"""Live benchmark runner (Ethereum mainnet).

Cases: every pair seen in data/swaps.json at its historical p25 / median / max size (rounded to 2 significant
digits, sizes within 25% of each other merged). The swapper is the real SwapModule that did that pair last.

Per round and case:
  t0      quote all aggregators in parallel (aggbench.bench.quoters), read the module oracle bound, then simulate every
          quote on the SAME pinned block (aggbench.bench.simulate)
  t0+5m   re-simulate the SAME calldata (no re-quote) on a fresh block — models a delayed execution
  t0+10m  same again
Rounds start `--spacing` seconds apart and overlap; the case order is shuffled every round.

Output: data/<out>.jsonl, one line per event: {"kind": "quote", round, case, oracle_out, multiplier, quotes, sims}
        and {"kind": "resim", round, delay, pair, amountIn, sims}. Load with aggbench.results.load_rows().
Usage:  python3 -m aggbench.bench.run --rounds 6 --spacing 180 --delays 300,600 --out results.jsonl [--pairs A->B,..] [--aggs kyber,fynd,0x]
        smoke test: python3 -m aggbench.bench.run --max-cases 3 --delays "" --out smoke.jsonl
"""
import argparse
import json
import random
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from web3 import Web3

from aggbench.bench.quoters import QUOTERS
from aggbench.bench.simulate import module_reference, simulate
from aggbench.chain import latest_block
from aggbench.config import DATA, ENV

lock = threading.Lock()


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
            target = min(q["t"] for q in row["quotes"].values()) + d
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


def main():
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


if __name__ == "__main__":
    main()
