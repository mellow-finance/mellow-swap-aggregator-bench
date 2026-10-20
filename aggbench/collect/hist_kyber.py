"""Step 5: what actually happened on our historical Kyber swaps.

For every swap in data/swaps.json:
  * captured   tokenOut sent to Kyber's surplus collector (config.SURPLUS_SINK["kyber"]) in the same tx receipt
  * quote_ts   Kyber's route timestamp and `quoted` = Kyber's quoted AmountOut, both parsed from the router
               calldata's clientData JSON ({"Source":..,"AmountOut":"..","RouteID":..,"Timestamp":..}); in batched txs
               the clientData whose AmountOut is closest to the realized amountOut is used
  * delay      block timestamp - quote_ts (quote -> execution latency of the curator bot)
  * from       tx.origin (curator EOA)

Output: data/hist.json (swaps.json rows + the fields above)
Prints: surplus captured per pair and since Kyber started capturing, delay distribution,
        realized vs Kyber's own quote (both directions), the bot's minAmountOut tolerance vs the quote.
"""
import datetime
import json
import re
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from aggbench.chain import rpc
from aggbench.config import DATA, SURPLUS_SINK
from aggbench.stats import pct
from aggbench.valuation import load_prices

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
COLLECTOR = SURPLUS_SINK["kyber"]
CLIENT_DATA = re.compile(rb'"AmountOut":"(\d+)"[^}]*?"Timestamp":(\d+)')


def enrich(s):
    receipt = rpc("eth_getTransactionReceipt", [s["tx"]])
    captured = 0
    for lg in receipt["logs"]:
        if (lg["topics"] and lg["topics"][0] == TRANSFER and len(lg["topics"]) == 3
                and lg["topics"][2][-40:] == COLLECTOR[2:] and lg["address"].lower() == s["tokenOut"].lower()):
            captured += int(lg["data"], 16)
    tx = rpc("eth_getTransactionByHash", [s["tx"]])
    got = int(s["amountOut"])
    pairs = [(int(a), int(t)) for a, t in CLIENT_DATA.findall(bytes.fromhex(tx["input"][2:]))]
    quoted, quote_ts = min(pairs, key=lambda p: abs(p[0] - got)) if pairs else (None, None)
    if quoted and abs(got / quoted - 1) > 0.05:  # clientData of another swap in the batch
        quoted = quote_ts = None
    return {**s, "captured": captured, "quoted": quoted, "quote_ts": quote_ts, "from": tx["from"]}


def main():
    swaps = json.load(open(DATA / "swaps.json"))
    with ThreadPoolExecutor(8) as ex:
        hist = list(ex.map(enrich, swaps))
    json.dump(hist, open(DATA / "hist.json", "w"), indent=1)
    px = load_prices()
    usd_out = lambda s, amount: amount / 10 ** s["decOut"] * px.get(s["symOut"], 1)

    print("## Kyber positive-slippage capture on our swaps")
    by = defaultdict(lambda: [0, 0, 0.0])
    for s in hist:
        b = by[f"{s['symIn']}->{s['symOut']}"]
        b[0] += 1
        if s["captured"]:
            b[1] += 1
            b[2] += usd_out(s, s["captured"])
    for p, (n, nc, usd) in sorted(by.items(), key=lambda kv: -kv[1][2]):
        if nc:
            print(f"  {p:18} swaps {n:3}  with capture {nc:2}  ~${usd:,.0f}")
    t_cap = min(s["ts"] for s in hist if s["captured"])
    since = [s for s in hist if s["ts"] >= t_cap]
    cap = sum(usd_out(s, s["captured"]) for s in since)
    vol = sum(int(s["amountIn"]) / 10 ** s["decIn"] * px.get(s["symIn"], 0) for s in since)
    print(f"  first capture {datetime.datetime.fromtimestamp(t_cap, datetime.UTC):%Y-%m-%d}; since then {len(since)} swaps, "
          f"{sum(1 for s in since if s['captured'])} captured, ${cap:,.0f} on ${vol / 1e6:,.0f}M = {cap / vol * 1e4:.3f} bps of volume")

    print("\n## Quote -> execution delay (s)")
    d = sorted(s["ts"] - s["quote_ts"] for s in hist if s["quote_ts"])
    print(f"  n={len(d)} min {d[0]} p25 {pct(d, .25):.0f} median {statistics.median(d):.0f} p75 {pct(d, .75):.0f} p90 {pct(d, .9):.0f} max {d[-1]}")

    print("\n## Realized vs Kyber's own quote")
    q = [s for s in hist if s["quoted"]]
    sl = [(int(s["amountOut"]) / s["quoted"] - 1) * 1e4 for s in q]
    neg = sum(usd_out(s, int(s["amountOut"]) - s["quoted"]) for s in q if int(s["amountOut"]) < s["quoted"])
    pos = sum(usd_out(s, int(s["amountOut"]) - s["quoted"]) for s in q if int(s["amountOut"]) > s["quoted"] + 1)
    print(f"  n={len(q)} bps: median {statistics.median(sl):.3f} mean {statistics.mean(sl):.3f} p10 {pct(sl, .1):.2f} p5 {pct(sl, .05):.2f} min {min(sl):.2f} max {max(sl):.2f}")
    print(f"  below quote {sum(x < -0.01 for x in sl)} swaps = -${-neg:,.0f}; above quote kept by the vault {sum(x > 0.01 for x in sl)} = +${pos:,.0f}")
    print(f"  exactly quote + 1 wei (surplus went to Kyber): {sum(0 <= int(s['amountOut']) - s['quoted'] <= 1 for s in q)}")

    print("\n## Bot tolerance: module minAmountOut vs Kyber quote, bps")
    tol = [(int(s["minAmountOut"]) / s["quoted"] - 1) * 1e4 for s in q]
    print(f"  median {statistics.median(tol):.2f} p10 {pct(tol, .1):.2f} p90 {pct(tol, .9):.2f} min {min(tol):.2f}")


if __name__ == "__main__":
    main()
