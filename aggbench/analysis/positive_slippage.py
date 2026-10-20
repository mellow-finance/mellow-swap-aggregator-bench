"""Step 8: how much positive slippage Kyber takes from our vaults, extrapolated to $/yr and APR.

Two estimates:
  A. actual   on-chain capture rate since Kyber started capturing (data/hist.json from aggbench.collect.hist_kyber):
              captured $ / volume in that window, applied to (a) the average annual volume and (b) the recent run-rate
  B. simulated  Kyber's sink gain in the +5m/+10m re-simulations (production median quote->tx delay is ~8 min),
              size-aware (each historical swap -> nearest benchmarked size) and only for quotes that clear the module
              oracle bound (unusable quotes like USDe->USDC 10M would otherwise dominate)
APR = annual $ / current TVL (valuation.vault_tvls).

Usage: python3 -m aggbench.analysis.positive_slippage
"""
import json
import statistics
from collections import defaultdict

from aggbench.config import DATA, SURPLUS_SINK
from aggbench.results import load_runs, sane
from aggbench.valuation import annualize_factor, load_prices, nearest_size, sizes_by_pair, usd_in, vault_tvls

SINK = SURPLUS_SINK["kyber"]


def main():
    px = load_prices()
    hist = json.load(open(DATA / "hist.json"))
    tvl = vault_tvls()
    yr = annualize_factor(hist)
    usd_out = lambda s, amount: amount / 10 ** s["decOut"] * px.get(s["symOut"], 1)

    # ---- A. actual
    t_cap = min(s["ts"] for s in hist if s["captured"])
    since = [s for s in hist if s["ts"] >= t_cap]
    cap = sum(usd_out(s, s["captured"]) for s in since)
    vol_since = sum(usd_in(s, px) for s in since)
    yr_since = 365 * 86400 / (max(s["ts"] for s in hist) - t_cap)
    rate = cap / vol_since
    print(f"A. actual: {len(since)} swaps over {365 / yr_since:.0f} days, ${vol_since / 1e6:.0f}M, Kyber took ${cap:,.0f} = {rate * 1e4:.3f} bps of volume;"
          f" run-rate ${cap * yr_since:,.0f}/yr on recent volume")

    # ---- B. simulated, size-aware, usable quotes only
    capz = defaultdict(list)
    for _, rows in load_runs():
        for r in rows:
            if not sane(r, "kyber"):
                continue
            e = r["quotes"]["kyber"]["expected_out"]
            for d in ("300", "600"):
                s = r["sims"].get("kyber", {}).get(d)
                if s and s.get("ok"):
                    capz[(r["case"]["pair"], r["case"]["amountIn"])].append((s.get("net_gainers") or {}).get(SINK, 0) / e * 1e4)
    sizes = sizes_by_pair(capz)
    by_pair = defaultdict(list)
    for (p, _), v in capz.items():
        by_pair[p] += v
    print("B. simulated capture at +5/+10m, mean bps per pair:", {p: round(statistics.mean(v), 3) for p, v in sorted(by_pair.items())})

    print(f"\n{'vault':10} {'vol $M/yr':>10} | {'A: actual rate $/yr':>19} {'APR bps':>8} | {'B: simulated $/yr':>17} {'APR bps':>8}")
    tot = defaultdict(float)
    for v, t in tvl.items():
        ss = [s for s in hist if s["vault"] == v]
        vol = sum(usd_in(s, px) for s in ss) * yr
        a = vol * rate
        b = 0.0
        for s in ss:
            pair = f"{s['symIn']}->{s['symOut']}"
            amt = nearest_size(sizes, pair, int(s["amountIn"]))
            if amt is not None:
                b += usd_in(s, px) * statistics.mean(capz[(pair, amt)]) / 1e4
        b *= yr
        for k, x in (("vol", vol), ("a", a), ("b", b), ("tvl", t["usd"])):
            tot[k] += x
        print(f"{t['name']:10} {vol / 1e6:10.0f} | {a:19,.0f} {a / t['usd'] * 1e4:8.3f} | {b:17,.0f} {b / t['usd'] * 1e4:8.3f}")
    print(f"{'total':10} {tot['vol'] / 1e6:10.0f} | {tot['a']:19,.0f} {tot['a'] / tot['tvl'] * 1e4:8.3f} | {tot['b']:17,.0f} {tot['b'] / tot['tvl'] * 1e4:8.3f}")


if __name__ == "__main__":
    main()
