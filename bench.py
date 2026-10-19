"""Step 3: live benchmark of DEX aggregators on our real SwapModule flow (Ethereum mainnet).

Cases: every pair seen in data/swaps.json at its historical p25 / median / max size (rounded to 2 significant
digits, sizes within 25% of each other merged). The swapper is the real SwapModule that did that pair last.

Per round and case:
  t0      quote all aggregators in parallel (wall-clock latency, calldata built for sender = recipient = module),
          read the module oracle bound, then simulate every quote on the SAME pinned block via eth_simulateV1:
          the module address gets the Sim contract (Sim.sol) as code and a tokenIn balance via a storage override,
          so the call path is exactly module -> approve -> router call -> balance delta (like SwapModule.swap).
  t0+5m   re-simulate the SAME calldata (no re-quote) on a fresh block — models a delayed execution
  t0+10m  same again
Each simulation stores: amount received, gas, revert reason, tokenOut transfers that leave the routers and every
address with a net tokenOut gain (this is how fee takers / surplus capture were found, see common.SURPLUS_SINK).

Router-level slippage is deliberately loose (2%) so we observe the real output instead of a revert; production
bots pass a tight module minAmountOut (~quote - 1 bps), see delay_cost.py.

Output: data/<out>.jsonl, one line per event: {"kind": "quote", round, case, oracle_out, multiplier, quotes, sims}
        and {"kind": "resim", round, delay, pair, amountIn, sims}. Load with common.load_rows().
Usage:  python3 bench.py --rounds 6 --spacing 180 --delays 300,600 --out results.jsonl [--pairs A->B,..] [--aggs kyber,fynd,0x]
        Smoke test: python3 bench.py --max-cases 3 --delays "" --out smoke.jsonl
"""
import argparse
import json
import random
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from eth_abi import decode, encode
from web3 import Web3

from common import DATA, ENV, HERE, KYBER_ROUTER, RPCS, latest_block, rpc

FYND_URL = "https://fynd-api.propellerheads.xyz/v1/ethereum/quote"  # hosted Fynd, raw key in Authorization
KYBER_URL = "https://aggregator-api.kyberswap.com/ethereum/api/v1"
KYBER_HEADERS = {"x-client-id": "mellow-bench"}
ZEROX_URL = "https://api.0x.org/swap/allowance-holder/quote"  # allowance-holder flow = plain approve + call, fits SwapModule
SLIPPAGE_BPS = 200
SIM_CODE = "0x" + open(HERE / "sim_runtime.hex").read().strip().removeprefix("0x")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RUN_SELECTOR = Web3.keccak(text="run(address,address,uint256,address,bytes)")[:4]

w3 = Web3(Web3.HTTPProvider(RPCS["ethereum"], request_kwargs={"timeout": 60}))
fynd_session, kyber_session, zerox_session = requests.Session(), requests.Session(), requests.Session()
lock = threading.Lock()


def eth_call(to, data, block="latest", overrides=None):
    params = [{"to": to, "data": data}, block]
    if overrides:
        params.append(overrides)
    return rpc("eth_call", params)


# ---------------------------------------------------------------- tokenIn balance override

_slot_cache = {}


def balance_slot(token, holder):
    """Storage slot holding balanceOf(holder) on `token` (works for proxies and ERC-7201 layouts).

    eth_createAccessList on balanceOf(holder) lists the slots it reads; each candidate is overridden with a probe
    value and the one that makes balanceOf return the probe wins. Overriding a proxy's implementation slot makes the
    call return garbage or empty data, hence the try/except."""
    key = (token.lower(), holder.lower())
    if key in _slot_cache:
        return _slot_cache[key]
    data = "0x70a08231" + encode(["address"], [holder]).hex()
    access_list = rpc("eth_createAccessList", [{"from": holder, "to": token, "data": data}, "latest"])
    probe = 0x1234567890ABCDEF1234567890
    for entry in access_list["accessList"]:
        if entry["address"].lower() != token.lower():
            continue
        for slot in entry["storageKeys"]:
            ov = {token: {"stateDiff": {slot: "0x" + probe.to_bytes(32, "big").hex()}}}
            try:
                got = int(eth_call(token, data, "latest", ov) or "0x0", 16)
            except Exception:
                continue
            if got == probe:
                _slot_cache[key] = slot
                return slot
    raise RuntimeError(f"balance slot not found for {token}")


# ---------------------------------------------------------------- module oracle bound

MODULE_ABI = [
    {"name": "evaluate", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"name": "customMultiplier", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "defaultMultiplier", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]


def module_reference(module, tin, tout, amount, block):
    """Fair output per the module's oracle (SwapModule.evaluate) and the slippage multiplier checkParams applies."""
    c = w3.eth.contract(address=Web3.to_checksum_address(module), abi=MODULE_ABI)
    try:
        fair = c.functions.evaluate(tin, tout, amount).call(block_identifier=block)
    except Exception:  # oracle has no price for one of the tokens
        fair = None
    mult = c.functions.customMultiplier(tin, tout).call(block_identifier=block) or c.functions.defaultMultiplier().call(block_identifier=block)
    return fair, mult


# ---------------------------------------------------------------- quoters
# Each returns {"agg", "t", latency fields, quoted_out, expected_out, to, data, value, ...} or {"error": ...}.
# expected_out = what the aggregator promises to deliver to the swapper (the base for "realized vs quote").

def quote_kyber(case):
    """GET /routes (quote) + POST /route/build (calldata). Latency is reported for both and in total."""
    t0 = time.time()
    p = {"tokenIn": case["tokenIn"], "tokenOut": case["tokenOut"], "amountIn": str(case["amountIn"]), "origin": case["sender"]}
    s = time.perf_counter()
    r = kyber_session.get(f"{KYBER_URL}/routes", params=p, headers=KYBER_HEADERS, timeout=30)
    lat_route = (time.perf_counter() - s) * 1000
    res = {"agg": "kyber", "t": t0, "lat_quote_ms": lat_route, "http": r.status_code}
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:300]}
    if r.status_code != 200 or not j.get("data"):
        res["error"] = str(j)[:300]
        return res
    rs = j["data"]["routeSummary"]
    res.update(quoted_out=int(rs["amountOut"]), gas_est=int(rs["gas"]), gas_usd=float(rs["gasUsd"]),
               sources=sorted({h["exchange"] for path in rs["route"] for h in path}), route_ts=rs.get("timestamp"))
    body = {"routeSummary": rs, "sender": case["sender"], "recipient": case["sender"], "origin": case["sender"],
            "slippageTolerance": SLIPPAGE_BPS, "source": "mellow-bench"}  # default deadline = +20 min
    s = time.perf_counter()
    b = kyber_session.post(f"{KYBER_URL}/route/build", json=body, headers=KYBER_HEADERS, timeout=30)
    res["lat_build_ms"] = (time.perf_counter() - s) * 1000
    res["lat_total_ms"] = lat_route + res["lat_build_ms"]
    try:
        bj = b.json()
    except Exception:
        bj = {"raw": b.text[:300]}
    if b.status_code != 200 or not bj.get("data"):
        res["error"] = "build: " + str(bj)[:300]
        return res
    d = bj["data"]
    res.update(built_out=int(d["amountOut"]), to=d["routerAddress"], data=d["data"],
               value=int(d.get("transactionValue") or 0), expected_out=int(d["amountOut"]))
    return res


def quote_fynd(case):
    """Single POST /quote with encoding_options -> quote + TychoRouter calldata. No deadline is encoded
    (only quotes with client fee params expire). expected_out = amount_out - router_fee (router fee is 0 today)."""
    t0 = time.time()
    body = {"orders": [{"token_in": case["tokenIn"], "token_out": case["tokenOut"], "amount": str(case["amountIn"]),
                        "side": "sell", "sender": case["sender"]}],
            "options": {"encoding_options": {"slippage": str(SLIPPAGE_BPS / 10000)}}}  # hosted API wants a string
    s = time.perf_counter()
    r = fynd_session.post(FYND_URL, json=body, headers={"Authorization": ENV["FYND_API_KEY"]}, timeout=30)
    lat = (time.perf_counter() - s) * 1000
    res = {"agg": "fynd", "t": t0, "lat_quote_ms": lat, "lat_total_ms": lat, "http": r.status_code}
    try:
        j = r.json()
    except Exception:  # gateway errors (401/429) are plain text
        res["error"] = r.text[:300]
        return res
    if r.status_code != 200:
        res["error"] = str(j)[:300]
        return res
    o = j["orders"][0]
    res.update(status=o.get("status"), solve_ms=j.get("solve_time_ms"), algorithm=o.get("algorithm"),
               quote_block=(o.get("block") or {}).get("number"))
    if o.get("status") != "success" or not o.get("transaction"):  # no_route_found / encoding_failed / ...
        res["error"] = f"status={o.get('status')}"
        return res
    fb = o.get("fee_breakdown") or {}
    out = int(o["amount_out"])
    res.update(quoted_out=out, gas_est=int(o["gas_estimate"]), router_fee=int(fb.get("router_fee", 0)),
               min_received=int(fb.get("min_amount_received", 0)), expected_out=out - int(fb.get("router_fee", 0)),
               sources=sorted({sw["protocol"] for sw in o["route"]["swaps"]}), price_impact_bps=o.get("price_impact_bps"),
               to=o["transaction"]["to"], data=o["transaction"]["data"], value=int(o["transaction"].get("value") or 0))
    return res


def quote_0x(case):
    """0x Swap API v2, allowance-holder quote. buyAmount is already net of the 0x volume fee (15 bps on most pairs
    on the Standard plan); the calldata carries a 300 s signature deadline (SignatureExpired afterwards)."""
    t0 = time.time()
    p = {"chainId": 1, "sellToken": case["tokenIn"], "buyToken": case["tokenOut"], "sellAmount": str(case["amountIn"]),
         "taker": case["sender"], "slippageBps": SLIPPAGE_BPS}
    s = time.perf_counter()
    r = zerox_session.get(ZEROX_URL, params=p, headers={"0x-api-key": ENV["ZERO_X_API_KEY"], "0x-version": "v2"}, timeout=30)
    lat = (time.perf_counter() - s) * 1000
    res = {"agg": "0x", "t": t0, "lat_quote_ms": lat, "lat_total_ms": lat, "http": r.status_code}
    try:
        j = r.json()
    except Exception:
        res["error"] = r.text[:300]
        return res
    if r.status_code != 200 or not j.get("liquidityAvailable") or not j.get("transaction"):
        res["error"] = str(j)[:300]
        return res
    fee = (j.get("fees") or {}).get("zeroExFee") or {}
    out = int(j["buyAmount"])
    res.update(quoted_out=out, expected_out=out, min_out=int(j["minBuyAmount"]), quote_block=int(j.get("blockNumber") or 0),
               fee_amount=int(fee.get("amount") or 0), fee_token=(fee.get("token") or "").lower(),
               gas_est=int(j["transaction"].get("gas") or 0), sources=sorted({f["source"] for f in j.get("route", {}).get("fills", [])}),
               to=j["transaction"]["to"], data=j["transaction"]["data"], value=int(j["transaction"].get("value") or 0))
    return res


QUOTERS = {"kyber": quote_kyber, "fynd": quote_fynd, "0x": quote_0x}


# ---------------------------------------------------------------- simulation

def simulate(case, q, block):
    """Execute q's calldata from the module at `block` (eth_simulateV1 builds block+1 on top of it)."""
    sender = Web3.to_checksum_address(case["sender"])
    slot = balance_slot(case["tokenIn"], sender)
    calldata = "0x" + (RUN_SELECTOR + encode(["address", "address", "uint256", "address", "bytes"],
                                              [case["tokenIn"], case["tokenOut"], case["amountIn"], Web3.to_checksum_address(q["to"]),
                                               bytes.fromhex(q["data"][2:])])).hex()
    overrides = {sender: {"code": SIM_CODE, "balance": hex(10**21)},  # module runs Sim; ETH for gas/value
                 Web3.to_checksum_address(case["tokenIn"]): {"stateDiff": {slot: "0x" + case["amountIn"].to_bytes(32, "big").hex()}}}
    params = [{"blockStateCalls": [{"stateOverrides": overrides,
                                    "calls": [{"from": sender, "to": sender, "data": calldata, "gas": hex(30_000_000)}]}],
               "traceTransfers": True, "validation": False}, hex(block)]
    t = time.time()
    call = rpc("eth_simulateV1", params)[0]["calls"][0]
    out = {"block": block, "t": t, "status": call.get("status")}
    if call.get("status") != "0x1" or not call.get("returnData") or call["returnData"] == "0x":
        out.update(ok=False, error=str(call.get("error"))[:300])
        return out
    ok, amount_out, gas_used, in_spent, ret = decode(["bool", "uint256", "uint256", "uint256", "bytes"], bytes.fromhex(call["returnData"][2:]))
    out.update(ok=ok, amount_out=amount_out, gas_used=gas_used, in_spent=in_spent)
    if not ok:  # router reverted: keep the reason (Error(string) decoded, custom errors as hex)
        out["error"] = "0x" + ret.hex()[:200]
        try:
            if ret[:4].hex() == "08c379a0":
                out["error"] = decode(["string"], ret[4:])[0]
        except Exception:
            pass
        return out
    # who else got tokenOut: transfers out of the routers to non-swapper addresses, and net tokenOut gainers
    tout, routers = case["tokenOut"].lower(), {q["to"].lower(), KYBER_ROUTER}
    leaks, net = [], defaultdict(int)
    for lg in call.get("logs", []):
        if lg["address"].lower() != tout or not lg["topics"] or lg["topics"][0] != TRANSFER_TOPIC or len(lg["topics"]) < 3:
            continue
        frm, to, amt = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:], int(lg["data"], 16)
        net[frm] -= amt
        net[to] += amt
        if frm in routers and to != sender.lower():
            leaks.append({"to": to, "amount": amt})
    out["router_leaks"] = leaks
    out["net_gainers"] = {a: v for a, v in net.items() if v > 0 and a != sender.lower() and a != "0x" + "0" * 40}
    return out


# ---------------------------------------------------------------- driver

def load_cases(max_cases=None):
    """(pair, size) cases from historical swaps; sender = the module that executed the pair most recently."""
    swaps = json.load(open(DATA / "swaps.json"))
    groups = defaultdict(list)
    for s in swaps:
        if s["chain"] == "ethereum":
            groups[(s["tokenIn"], s["tokenOut"])].append(s)
    cases = []
    for (tin, tout), ss in groups.items():
        ss.sort(key=lambda s: s["ts"])
        amts = sorted(int(s["amountIn"]) for s in ss)
        picks = [amts[len(amts) // 4], int(statistics.median(amts)), amts[-1]] if len(amts) >= 4 else [amts[-1]]
        dec = ss[0]["decIn"]
        sizes = []
        for sz in sorted({int(float(f"{p / 10**dec:.2g}") * 10**dec) for p in picks}):  # 2 significant digits
            if not sizes or sz > sizes[-1] * 1.25:  # drop near-duplicate sizes
                sizes.append(sz)
        for sz in sizes:
            cases.append({"pair": f"{ss[0]['symIn']}->{ss[0]['symOut']}", "tokenIn": Web3.to_checksum_address(tin),
                          "tokenOut": Web3.to_checksum_address(tout), "decIn": dec, "decOut": ss[0]["decOut"],
                          "amountIn": sz, "sizeHuman": sz / 10**dec, "sender": ss[-1]["module"], "nHist": len(ss)})
    return cases[:max_cases] if max_cases else cases


def record(path, row):
    with lock, open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def run_round(rnd, cases, quoters, delays, path):
    t_start = time.time()
    pending = []
    for case in random.sample(cases, len(cases)):  # shuffled so no pair is always quoted first
        with ThreadPoolExecutor(len(quoters)) as ex:
            futs = {name: ex.submit(fn, case) for name, fn in quoters.items()}
            quotes = {name: f.result() for name, f in futs.items()}
        blk = latest_block()  # one pinned block for all aggregators of this case
        fair, mult = module_reference(case["sender"], case["tokenIn"], case["tokenOut"], case["amountIn"], blk)
        row = {"round": rnd, "case": case, "oracle_out": fair, "multiplier": mult, "quotes": quotes, "sims": {}}
        for q in quotes.values():
            if "data" in q:
                try:
                    row["sims"].setdefault(q["agg"], {})["0"] = simulate(case, q, blk)
                except Exception as e:
                    row["sims"].setdefault(q["agg"], {})["0"] = {"ok": False, "error": f"sim: {e}"[:300]}
        record(path, {"kind": "quote", **row})
        pending.append(row)
        time.sleep(0.3)  # stay well below the API rate limits
    print(f"round {rnd}: quoted {len(cases)} cases in {time.time() - t_start:.0f}s", flush=True)
    for d in delays:
        for row in pending:  # same order as quoting, so each case is re-executed ~d seconds after its own quote
            target = row["quotes"]["kyber"]["t"] + d if "kyber" in row["quotes"] else t_start + d
            if target > time.time():
                time.sleep(target - time.time())
            blk = latest_block()
            sims = {}
            for agg, q in row["quotes"].items():
                if "data" in q:
                    try:
                        sims[agg] = simulate(row["case"], q, blk)
                    except Exception as e:
                        sims[agg] = {"ok": False, "error": f"sim: {e}"[:300]}
            record(path, {"kind": "resim", "round": rnd, "delay": d, "pair": row["case"]["pair"], "amountIn": row["case"]["amountIn"], "sims": sims})
        print(f"round {rnd}: resim +{d}s done", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--spacing", type=int, default=180, help="seconds between round starts (rounds overlap)")
    ap.add_argument("--delays", default="300,600", help="re-simulation delays in seconds, comma separated")
    ap.add_argument("--max-cases", type=int)
    ap.add_argument("--pairs", help="comma-separated pair names to keep, e.g. USDC->PYUSD")
    ap.add_argument("--aggs", default="kyber,fynd,0x", help="aggregators to quote; 0x is skipped without ZERO_X_API_KEY")
    ap.add_argument("--out", default="results.jsonl")
    a = ap.parse_args()
    cases = load_cases(a.max_cases)
    if a.pairs:
        cases = [c for c in cases if c["pair"] in a.pairs.split(",")]
    quoters = {n: QUOTERS[n] for n in a.aggs.split(",") if n != "0x" or ENV.get("ZERO_X_API_KEY")}
    delays = [int(x) for x in a.delays.split(",") if x]
    print(f"{len(cases)} cases, {a.rounds} rounds, delays {delays}, aggregators {list(quoters)}", flush=True)
    threads = []
    for r in range(a.rounds):
        th = threading.Thread(target=run_round, args=(r, cases, quoters, delays, DATA / a.out))
        th.start()
        threads.append(th)
        if r < a.rounds - 1:
            time.sleep(a.spacing)
    for th in threads:
        th.join()
    print("done", flush=True)
