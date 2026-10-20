"""Aggregator API clients. Each quoter takes a case and returns one dict:

  {"agg", "t" (unix time of the request), "lat_*_ms", "quoted_out", "expected_out", "to", "data", "value", "sources", ...}
  or {"agg", "t", "lat_*_ms", "error": ...} when there is no executable quote.

expected_out = what the aggregator promises to deliver to the swapper; it is the base for "realized vs quote".
Calldata is always built for sender = recipient = the SwapModule (case["sender"]).
Router-level slippage is deliberately loose (SLIPPAGE_BPS = 2%) so the simulation shows the real output instead of
reverting; production bots pass a tight module minAmountOut (~quote - 1 bps).
"""
import time

import requests

from aggbench.config import ENV

SLIPPAGE_BPS = 200
FYND_URL = "https://fynd-api.propellerheads.xyz/v1/ethereum/quote"  # hosted Fynd, raw key in Authorization
KYBER_URL = "https://aggregator-api.kyberswap.com/ethereum/api/v1"
KYBER_HEADERS = {"x-client-id": "mellow-bench"}
ZEROX_URL = "https://api.0x.org/swap/allowance-holder/quote"  # allowance-holder flow = plain approve + call, fits SwapModule

fynd_session, kyber_session, zerox_session = requests.Session(), requests.Session(), requests.Session()


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
