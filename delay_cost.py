"""Step 9: value of executing a Kyber quote within ~60 s instead of the production delay (median ~8 min).

1. Curator txs (both curator EOAs, Etherscan txlist) carrying Kyber calldata: revert rate per quote->tx delay bucket
   (delay = block time - clientData Timestamp) and gas burned on reverts.
2. Upper bound: same stale calldata re-simulated at +5/+10 vs t0 with the bench's loose 2% slippage. NOT a real loss —
   the bot passes minAmountOut ≈ quote - 1 bps (see hist_kyber.py), so such executions revert instead.
3. Realistic cost of waiting = -E[min(drift, 0)] of fresh Kyber quotes for the same case between rounds:
   upside above the quote is taken by Kyber, downside is realized (within tolerance, or via revert + re-quote).
   Extrapolated with each historical swap's own delay bucket, size-aware, to $/yr and APR.

Usage: python3 delay_cost.py
"""
import json
import re
import statistics
from collections import defaultdict

from common import (CURATOR_EOAS, DATA, annualize_factor, bps, etherscan, load_prices, load_runs, nearest_size, pct, sane,
                    sizes_by_pair, usd_in, vault_tvls)

BUCKETS = (("3-6m", 390), ("6-12m", 750), ("12-20m", 1230))


def bucket(seconds):
    for name, upper in BUCKETS:
        if seconds < upper:
            return name
    return None


def curator_reverts():
    rows = []
    for eoa in CURATOR_EOAS:
        page = 1
        while True:
            txs = etherscan(1, module="account", action="txlist", address=eoa, page=page, offset=1000, sort="asc")
            for t in txs:
                m = re.findall(rb'"Timestamp":(\d+)', bytes.fromhex(t["input"][2:]) if len(t["input"]) > 2 else b"")
                if m:  # tx carries Kyber router calldata
                    rows.append({"ok": t["isError"] == "0", "delay": int(t["timeStamp"]) - int(m[0]),
                                 "gas_eth": int(t["gasUsed"]) * int(t["gasPrice"]) / 1e18})
            if len(txs) < 1000:
                break
            page += 1
    print(f"1) curator txs with Kyber calldata: {len(rows)}, reverted {sum(not r['ok'] for r in rows)}, "
          f"gas burned on reverts {sum(r['gas_eth'] for r in rows if not r['ok']):.4f} ETH")
    edges = [(60, "<60s"), (180, "1-3m"), (360, "3-6m"), (600, "6-10m"), (1200, "10-20m"), (10**12, ">20m")]
    b = defaultdict(lambda: [0, 0])
    for r in rows:
        k = next(name for upper, name in edges if r["delay"] < upper)
        b[k][0] += not r["ok"]
        b[k][1] += 1
    for _, k in edges:
        if k in b:
            print(f"   delay {k:6}: {b[k][1]:3} txs, reverted {b[k][0]} ({b[k][0] / b[k][1] * 100:.0f}%)")


def main():
    curator_reverts()
    runs = load_runs()

    # 2) upper bound: stale calldata, loose slippage
    stale = defaultdict(list)
    for _, rows in runs:
        for r in rows:
            s0 = r["sims"].get("kyber", {}).get("0")
            if not sane(r, "kyber") or not s0 or not s0.get("ok"):
                continue
            for d in ("300", "600"):
                s = r["sims"]["kyber"].get(d)
                if s and s.get("ok"):
                    stale[d].append(bps(s["amount_out"], s0["amount_out"]))
    for d, v in stale.items():
        print(f"2) stale calldata +{int(d) // 60}m vs t0 (upper bound, 2% slippage): mean {statistics.mean(v):.3f} median {statistics.median(v):.3f} p5 {pct(v, .05):.2f} bps")

    # 3) fresh-quote drift between rounds, per (pair, size)
    quotes = defaultdict(list)
    for path, rows in runs:
        for r in rows:
            if sane(r, "kyber"):
                q = r["quotes"]["kyber"]
                quotes[(path, r["case"]["pair"], r["case"]["amountIn"])].append((q["t"], q["expected_out"]))
    drift = defaultdict(lambda: defaultdict(list))
    for (_, p, a), qs in quotes.items():
        qs.sort()
        for i in range(len(qs)):
            for j in range(i + 1, len(qs)):
                b = bucket(qs[j][0] - qs[i][0])
                if b:
                    drift[(p, a)][b].append(bps(qs[j][1], qs[i][1]))
    for b, _ in BUCKETS:
        v = [x for k in drift for x in drift[k][b]]
        print(f"3) fresh-quote drift {b:6}: n={len(v)} stdev {statistics.pstdev(v):.2f}  cost -E[min(drift,0)] {-statistics.mean([min(x, 0) for x in v]):.3f} bps")

    px, tvl = load_prices(), vault_tvls()
    hist = json.load(open(DATA / "hist.json"))
    yr = annualize_factor(hist)
    sizes = sizes_by_pair(drift)
    print("\n   $/yr gained by executing within 60 s (each swap's own delay bucket), current TVL:")
    tot = defaultdict(float)
    for v, t in tvl.items():
        g = 0.0
        for s in hist:
            if s["vault"] != v or not s.get("quote_ts"):
                continue
            pair = f"{s['symIn']}->{s['symOut']}"
            amt = nearest_size(sizes, pair, int(s["amountIn"]))
            if amt is None:
                continue
            vv = drift[(pair, amt)][bucket(s["ts"] - s["quote_ts"]) or "12-20m"] or drift[(pair, amt)]["6-12m"]
            if vv:
                g += usd_in(s, px) * -statistics.mean([min(x, 0) for x in vv]) / 1e4
        g *= yr
        tot["g"] += g
        tot["tvl"] += t["usd"]
        print(f"   {t['name']:10} ${g:9,.0f}/yr  APR +{g / t['usd'] * 1e4:.3f} bps")
    print(f"   {'total':10} ${tot['g']:9,.0f}/yr  APR +{tot['g'] / tot['tvl'] * 1e4:.3f} bps")


if __name__ == "__main__":
    main()
