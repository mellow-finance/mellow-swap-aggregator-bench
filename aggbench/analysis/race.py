"""Step 6: potential gain of racing the routers — execute whichever route REALIZES the most — vs Kyber-only.

All aggregators of a case were simulated on the same block, so "best on-chain" = max realized output among routes
that (a) executed and (b) cleared the SwapModule oracle bound (a swap below it would revert in checkParams).
Baseline = Kyber's realized output. Strategies:
  K+F, K+0x, K+F+0x     race of those routers
  K+F+0x(no fee)        same, 0x valued without its 15 bps volume fee (custom plan)
  K+F by quote          pick the highest QUOTE and execute it (no simulation) — shows the cost of overquotes;
                        a revert of the pick counts as no gain (retry with Kyber)
"rescued" = Kyber's route failed but another one worked (reliability win, not counted in bps).

$ estimate: every historical swap (data/swaps.json) is mapped to the nearest benchmarked size of its pair and gets
that case's mean gain; summed over the 307-day history and annualized. Also prints the gas a one-tx on-chain race
(try every route, keep the best) would cost, and the K+F race on stale calldata (+5m/+10m; 0x calldata expires at 300s).

Usage: python3 -m aggbench.analysis.race [results3.jsonl]
"""
import json
import statistics
import sys
from collections import defaultdict

from aggbench.config import DATA
from aggbench.results import exp_out, load_rows, oracle_bound
from aggbench.stats import bps, fmt, pct
from aggbench.valuation import annualize_factor, load_prices, nearest_size, sizes_by_pair, usd_in

GAS_PRICE_GWEI = 0.037  # observed in Kyber/Fynd quotes during the benchmark window

STRATS = {"K+F": (("kyber", "fynd"), False), "K+0x": (("kyber", "0x"), False),
          "K+F+0x": (("kyber", "fynd", "0x"), False), "K+F+0x(no fee)": (("kyber", "fynd", "0x"), True)}


def realized(r, a, d="0", nofee=False):
    """Realized output of aggregator `a` at delay `d`, None if it reverted or would fail the module oracle bound."""
    s = r["sims"].get(a, {}).get(d)
    if not s or not s.get("ok") or not r["oracle_out"]:
        return None
    out = s["amount_out"]
    if nofee and a == "0x":
        q = r["quotes"]["0x"]
        if q.get("fee_token") == r["case"]["tokenOut"].lower():
            out += q.get("fee_amount", 0)
    return out if out >= oracle_bound(r) else None


def race_case_gains(rows):
    """{(pair, amountIn): {strategy: [gain bps per round]}} plus diagnostics."""
    case_gain = defaultdict(lambda: defaultdict(list))
    rescued, winners, by_quote_reverts, race_gas = defaultdict(int), defaultdict(lambda: defaultdict(int)), defaultdict(int), []
    for r in rows:
        key = (r["case"]["pair"], r["case"]["amountIn"])
        base = realized(r, "kyber")
        for name, (aggs, nofee) in STRATS.items():
            vals = {a: v for a in aggs if a in r["quotes"] and (v := realized(r, a, nofee=nofee))}
            if not vals:
                continue
            best = max(vals, key=vals.get)
            if base is None:
                rescued[name] += 1
                continue
            g = bps(vals[best], base)
            case_gain[key][name].append(g)
            winners[name][best if g > 0.01 else "kyber"] += 1
        # pick by quote among Kyber/Fynd quotes that clear the oracle bound
        cands = {a: exp_out(r, a) for a in ("kyber", "fynd") if exp_out(r, a) and exp_out(r, a) >= (oracle_bound(r) or 0)}
        if cands and base is not None:
            pick = max(cands, key=cands.get)
            got = realized(r, pick)
            if got is None:
                by_quote_reverts[pick] += 1
            case_gain[key]["K+F by quote"].append(bps(got, base) if got else 0.0)
        # one-tx race = every route is tried (simulated in-tx) before the best one is executed
        race_gas.append(sum((r["sims"].get(a, {}).get("0") or {}).get("gas_used") or 0 for a in r["quotes"]))
    return case_gain, rescued, winners, by_quote_reverts, race_gas


def dollars(case_gain, names, hist, px):
    """Map each historical swap to its nearest benchmarked case; returns {name: $ over period}, per-pair and per-vault splits."""
    sizes = sizes_by_pair(case_gain)
    tot, per_pair, per_vault = defaultdict(float), defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float))
    for s in hist:
        pair = f"{s['symIn']}->{s['symOut']}"
        usd = usd_in(s, px)
        per_pair[pair]["vol"] += usd
        per_vault[s["vault"]]["vol"] += usd
        amt = nearest_size(sizes, pair, int(s["amountIn"]))
        if amt is None:
            continue
        for name in names:
            g = case_gain[(pair, amt)].get(name)
            if g:
                v = usd * statistics.mean(g) / 1e4
                tot[name] += v
                per_pair[pair][name] += v
                per_vault[s["vault"]][name] += v
    return tot, per_pair, per_vault


def main(path):
    rows = load_rows(path)
    case_gain, rescued, winners, by_quote_reverts, race_gas = race_case_gains(rows)
    names = list(STRATS) + ["K+F by quote"]

    print("## Race at t0 vs Kyber-only (routes must execute AND clear the oracle bound)")
    for name in names:
        g = [x for c in case_gain.values() for x in c.get(name, [])]
        if not g:
            continue
        print(f"{name:16} n={len(g)} mean {fmt(statistics.mean(g), 3)} bps  median {fmt(pct(g, .5), 3)}  p90 {fmt(pct(g, .9), 2)}  max {fmt(max(g), 2)}"
              f"  gain>0.1bps {sum(x > .1 for x in g) / len(g) * 100:.0f}%  | rescued Kyber failures {rescued[name]}"
              + (f"  | winners {dict(winners[name])}" if name in winners else ""))
    print("by-quote picks that reverted:", dict(by_quote_reverts))

    hist = [s for s in json.load(open(DATA / "swaps.json")) if s["chain"] == "ethereum"]
    px = load_prices()
    yr = annualize_factor(hist)
    tot, per_pair, _ = dollars(case_gain, names, hist, px)
    vol = sum(usd_in(s, px) for s in hist)
    print(f"\n## $ on historical volume ({len(hist)} swaps, ${vol / 1e6:,.0f}M, {365 / yr:.0f} days; current prices)")
    for name in names:
        print(f"{name:16} ${tot[name]:9,.0f} over the period  (${tot[name] * yr:,.0f}/yr, {tot[name] / vol * 1e4:.3f} bps of volume)")
    print("per pair:")
    for pair, d in sorted(per_pair.items(), key=lambda kv: -kv[1].get("K+F+0x", 0)):
        print(f"  {pair:18} vol ${d['vol'] / 1e6:7.1f}M  K+F+0x ${d.get('K+F+0x', 0):8,.0f}  K+F ${d.get('K+F', 0):8,.0f}"
              f"  no-fee-0x ${d.get('K+F+0x(no fee)', 0):8,.0f}  by-quote ${d.get('K+F by quote', 0):9,.0f}")

    g = statistics.median(race_gas)
    print(f"\n## One-tx on-chain race gas (sum of all routes, median) {g / 1e6:.2f}M ≈ ${g * GAS_PRICE_GWEI * 1e-9 * px['WETH']:.2f} at "
          f"{GAS_PRICE_GWEI} gwei, ${g * 10e-9 * px['WETH']:.0f} at 10 gwei — simulating off-chain and sending the winner costs nothing extra")

    for d in ("300", "600"):  # stale calldata: only Kyber/Fynd (0x signature expired)
        gs, resc = [], 0
        for r in rows:
            b, f = realized(r, "kyber", d), realized(r, "fynd", d)
            if b is None:
                resc += f is not None
                continue
            gs.append(bps(max(b, f or 0), b))
        print(f"K+F race at +{int(d) // 60}m: mean {fmt(statistics.mean(gs), 3)} bps, gain>0.1bps in {sum(x > .1 for x in gs)}/{len(gs)}, rescued {resc}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results3.jsonl")
