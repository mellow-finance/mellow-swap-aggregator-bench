"""Step 10: Fynd vs Kyber along the execution-delay axis (both benchmark runs, cases where BOTH quotes clear the oracle bound).

1) share of executions within the bot tolerance (realized >= own quote - 1 bps) at t0 / +5m / +10m, same calldata
2) realized vs own quote at t0 (overquote tails)
3) cost of waiting = -E[min(fresh-quote drift, 0)] per delay bucket (both take 100% of the upside)
4) realized Fynd vs Kyber on the same block at t0 / +5m / +10m
5) $/yr and APR vs today's setup (Kyber at the production delay): Kyber executed <60s, Fynd at the current delay,
   Fynd executed <60s — on the historical volume of pairs/sizes where both routes are usable
6) the same per pair

Usage: python3 fynd_vs_kyber_delay.py
"""
import json
import statistics
from collections import defaultdict

from common import DATA, annualize_factor, bps, load_prices, load_runs, nearest_size, pct, sane, sizes_by_pair, usd_in, vault_tvls

TOL = -1.0  # bot's module minAmountOut ≈ quote - 1 bps (median, hist_kyber.py)
AG = ("kyber", "fynd")


def bucket(seconds):
    return "3-6m" if seconds < 390 else "6-12m" if seconds < 750 else "12-20m" if seconds < 1230 else None


def main():
    both = [(path, r) for path, rows in load_runs() for r in rows if sane(r, "kyber") and sane(r, "fynd")]
    print(f"cases where both quotes clear the oracle bound: {len(both)}")

    print("\n1) executable within tolerance (realized >= quote - 1 bps), same calldata")
    for d in ("0", "300", "600"):
        line = []
        for a in AG:
            sims = [(r, r["sims"].get(a, {}).get(d)) for _, r in both]
            sims = [(r, s) for r, s in sims if s is not None]
            ok = sum(bool(s.get("ok") and bps(s["amount_out"], r["quotes"][a]["expected_out"]) >= TOL) for r, s in sims)
            line.append(f"{a} {ok}/{len(sims)} ({ok / len(sims) * 100:.0f}%)")
        print(f"   +{int(d) // 60:>2}m: " + "   ".join(line))

    print("\n2) realized vs own quote at t0, bps")
    for a in AG:
        v = [bps(r["sims"][a]["0"]["amount_out"], r["quotes"][a]["expected_out"]) for _, r in both if r["sims"].get(a, {}).get("0", {}).get("ok")]
        print(f"   {a:5} mean {statistics.mean(v):+.3f}  p10 {pct(v, .1):+.2f}  p5 {pct(v, .05):+.2f}  worst {min(v):+.2f}  below -1bps: {sum(x < TOL for x in v)}/{len(v)}")

    print("\n3) cost of waiting = -E[min(fresh-quote drift, 0)], bps")
    q = defaultdict(list)
    for path, r in both:
        for a in AG:
            q[(path, a, r["case"]["pair"], r["case"]["amountIn"])].append((r["quotes"][a]["t"], r["quotes"][a]["expected_out"]))
    drift = defaultdict(lambda: defaultdict(list))  # (agg, pair, amount) -> bucket -> [bps]
    for (_, a, p, amt), qs in q.items():
        qs.sort()
        for i in range(len(qs)):
            for j in range(i + 1, len(qs)):
                b = bucket(qs[j][0] - qs[i][0])
                if b:
                    drift[(a, p, amt)][b].append(bps(qs[j][1], qs[i][1]))
    for b in ("3-6m", "6-12m", "12-20m"):
        out = []
        for a in AG:
            v = [x for (aa, _, _), dd in drift.items() if aa == a for x in dd[b]]
            out.append(f"{a} {-statistics.mean([min(x, 0) for x in v]):.3f} (stdev {statistics.pstdev(v):.2f}, n={len(v)})")
        print(f"   {b:6}: " + "   ".join(out))

    print("\n4) realized Fynd vs Kyber, same block, bps (+ = Fynd better)")
    for d in ("0", "300", "600"):
        v = [bps(r["sims"]["fynd"][d]["amount_out"], r["sims"]["kyber"][d]["amount_out"]) for _, r in both
             if r["sims"].get("fynd", {}).get(d, {}).get("ok") and r["sims"].get("kyber", {}).get(d, {}).get("ok")]
        print(f"   +{int(d) // 60:>2}m: mean {statistics.mean(v):+.3f} median {statistics.median(v):+.3f}  Fynd better {sum(x > .1 for x in v)} / Kyber better {sum(x < -.1 for x in v)} / tie {sum(abs(x) <= .1 for x in v)}")

    # ---- $ vs today's Kyber at the production delay
    px, tvl = load_prices(), vault_tvls()
    hist = json.load(open(DATA / "hist.json"))
    yr = annualize_factor(hist)
    fk = defaultdict(list)  # realized Fynd - Kyber at t0 per case
    for _, r in both:
        sk, sf = r["sims"]["kyber"].get("0", {}), r["sims"]["fynd"].get("0", {})
        if sk.get("ok") and sf.get("ok"):
            fk[(r["case"]["pair"], r["case"]["amountIn"])].append(bps(sf["amount_out"], sk["amount_out"]))
    sizes = sizes_by_pair(fk)

    def wait_cost(a, p, amt, delay):
        v = drift[(a, p, amt)][bucket(delay) or "12-20m"] or drift[(a, p, amt)]["6-12m"]
        return -statistics.mean([min(x, 0) for x in v]) if v else 0.0

    print("\n5) $/yr vs today's setup (Kyber, current delay); APR bps in brackets")
    print(f"{'vault':10} {'vol $M/yr':>9} | {'Kyber <60s':>16} | {'Fynd now':>16} | {'Fynd <60s':>16}")
    T, per_pair = defaultdict(float), defaultdict(lambda: defaultdict(float))
    for v, t in tvl.items():
        acc = defaultdict(float)
        for s in hist:
            if s["vault"] != v or not s.get("quote_ts"):
                continue
            pair = f"{s['symIn']}->{s['symOut']}"
            amt = nearest_size(sizes, pair, int(s["amountIn"]))
            if amt is None:
                continue
            usd, delay = usd_in(s, px), s["ts"] - s["quote_ts"]
            d_fk = statistics.mean(fk[(pair, amt)])
            ck, cf = wait_cost("kyber", pair, amt, delay), wait_cost("fynd", pair, amt, delay)
            acc["vol"] += usd
            acc["k_fast"] += usd * ck / 1e4               # Kyber stops paying its waiting cost
            acc["f_now"] += usd * (d_fk - cf + ck) / 1e4  # Fynd's edge, minus Fynd's waiting cost, plus Kyber's avoided
            acc["f_fast"] += usd * (d_fk + ck) / 1e4      # Fynd's edge, no waiting cost for anyone
            per_pair[pair]["vol"] += usd * yr
            per_pair[pair]["f_fast"] += usd * (d_fk + ck) / 1e4 * yr
            per_pair[pair]["fk"] = statistics.mean([x for (pp, _), c in fk.items() if pp == pair for x in c])
        for k in acc:
            acc[k] *= yr
            T[k] += acc[k]
        T["tvl"] += t["usd"]
        cell = lambda k: f"{acc[k]:+9,.0f} ({acc[k] / t['usd'] * 1e4:+.2f})"
        print(f"{t['name']:10} {acc['vol'] / 1e6:9.0f} | {cell('k_fast'):>16} | {cell('f_now'):>16} | {cell('f_fast'):>16}")
    cell = lambda k: f"{T[k]:+9,.0f} ({T[k] / T['tvl'] * 1e4:+.2f})"
    print(f"{'total':10} {T['vol'] / 1e6:9.0f} | {cell('k_fast'):>16} | {cell('f_now'):>16} | {cell('f_fast'):>16}")

    print("\n6) per pair, Fynd executed <60s vs today's Kyber")
    for p, d in sorted(per_pair.items(), key=lambda kv: -kv[1]["f_fast"]):
        print(f"   {p:16} vol ${d['vol'] / 1e6:6.0f}M/yr  Fynd-Kyber t0 mean {d['fk']:+7.2f} bps  ->  {d['f_fast']:+9,.0f} $/yr")


if __name__ == "__main__":
    main()
