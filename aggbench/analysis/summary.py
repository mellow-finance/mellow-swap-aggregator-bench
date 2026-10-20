"""Step 4: summary of a benchmark run (aggbench.bench.run) — works for any set of aggregators found in the file (Kyber is the baseline).

Sections:
  availability     quote returned / clears the module oracle bound / "usable" (clears the bound AND executes at t0)
  latency          client wall clock per aggregator (Kyber = routes + build, Fynd/0x = one call)
  0x fee           zeroExFee in bps of the gross output, per pair (only if 0x is in the file)
  head-to-head     every aggregator vs Kyber on cases where BOTH quotes clear the oracle bound:
                   quote, realized at t0 / +5m / +10m (same block per case), reverts
  execution        realized vs own quote, revert share, how often the swapper got more than the quote, and how
                   much surplus the aggregator's sink (config.SURPLUS_SINK) took
  per pair         medians per (pair, size): Kyber vs oracle, realized deltas vs Kyber, 0x fee, failures

Usage: python3 -m aggbench.analysis.summary results3.jsonl
       python3 -m aggbench.analysis.summary results.jsonl --override results_fix.jsonl    (first run with the re-run of broken cases)
"""
import argparse
import statistics
from collections import Counter, defaultdict

from aggbench.results import aggs_of, captured, exp_out, fee_bps_0x, load_rows, sane
from aggbench.stats import bps, fmt, pct

DELAYS = ("0", "300", "600")



def main(rows):
    aggs = aggs_of(rows)
    print(f"rows {len(rows)}, rounds {sorted({r['round'] for r in rows})}, aggregators {aggs}")

    print("\n## Availability")
    for a in aggs:
        n = len(rows)
        ok = sum(1 for r in rows if exp_out(r, a))
        sn = sum(1 for r in rows if sane(r, a))
        us = sum(1 for r in rows if sane(r, a) and r["sims"].get(a, {}).get("0", {}).get("ok"))
        errs = Counter((r["quotes"][a].get("error") or "")[:70] for r in rows if not exp_out(r, a))
        print(f"{a:5} quoted {ok}/{n}  passes oracle {sn}/{n}  usable {us}/{n}  errors {dict(errs.most_common(4))}")

    print("\n## Latency ms (p50 / p90 / p99)")
    for a in aggs:
        v = [r["quotes"][a]["lat_total_ms"] for r in rows if "lat_total_ms" in r["quotes"][a]]
        extra = ""
        if a == "fynd":
            s = [r["quotes"][a]["solve_ms"] for r in rows if r["quotes"][a].get("solve_ms") is not None]
            extra = f"   server solve_time_ms p50 {fmt(pct(s, .5), 0)} p90 {fmt(pct(s, .9), 0)}"
        print(f"{a:5} {fmt(pct(v, .5), 0)} / {fmt(pct(v, .9), 0)} / {fmt(pct(v, .99), 0)}  n={len(v)}{extra}")

    if "0x" in aggs:
        print("\n## 0x volume fee, bps")
        fb = [x for x in (fee_bps_0x(r) for r in rows if exp_out(r, "0x")) if x is not None]
        print(f"median {fmt(pct(fb, .5))}  min {fmt(min(fb))}  max {fmt(max(fb))}  zero-fee quotes {sum(x < 0.01 for x in fb)}/{len(fb)}")
        byp = defaultdict(list)
        for r in rows:
            if exp_out(r, "0x"):
                byp[r["case"]["pair"]].append(fee_bps_0x(r))
        print("  per pair:", {k: round(statistics.median(v), 2) for k, v in sorted(byp.items())})

    print("\n## Head-to-head vs Kyber, cases where both quotes clear the oracle bound (+ = challenger better), bps")
    for a in aggs[1:]:
        both = [r for r in rows if sane(r, "kyber") and sane(r, a)]
        print(f"-- {a} vs kyber: {len(both)} cases")
        q = [bps(exp_out(r, a), exp_out(r, "kyber")) for r in both]
        print(f"   quote     median {fmt(pct(q, .5))} p10 {fmt(pct(q, .1))} p90 {fmt(pct(q, .9))}  {a} better {sum(x > .1 for x in q)} / kyber better {sum(x < -.1 for x in q)} / tie {sum(abs(x) <= .1 for x in q)}")
        if a == "0x":  # what 0x would be without its fee (custom plan)
            qp = [bps(exp_out(r, a), exp_out(r, "kyber")) + f for r in both if (f := fee_bps_0x(r)) is not None]
            print(f"   quote w/o 0x fee  median {fmt(pct(qp, .5))} p10 {fmt(pct(qp, .1))} p90 {fmt(pct(qp, .9))}  0x better {sum(x > .1 for x in qp)} / kyber better {sum(x < -.1 for x in qp)} / tie {sum(abs(x) <= .1 for x in qp)}")
        for d in DELAYS:
            v, rk, ra = [], 0, 0
            for r in both:
                sk, sa = r["sims"].get("kyber", {}).get(d), r["sims"].get(a, {}).get(d)
                if sk is None or sa is None:  # re-simulation not run (yet)
                    continue
                rk += not sk.get("ok")
                ra += not sa.get("ok")
                if sk.get("ok") and sa.get("ok"):
                    v.append(bps(sa["amount_out"], sk["amount_out"]))
            print(f"   real +{int(d) // 60:>2}m median {fmt(pct(v, .5))} mean {fmt(statistics.mean(v) if v else None)} p10 {fmt(pct(v, .1))} p90 {fmt(pct(v, .9))}"
                  f"  {a} better {sum(x > .1 for x in v)} / kyber better {sum(x < -.1 for x in v)} / tie {sum(abs(x) <= .1 for x in v)}  reverts kyber {rk} {a} {ra}")

    print("\n## Execution vs own quote, on each aggregator's own oracle-clearing quotes")
    for a in aggs:
        rs = [r for r in rows if sane(r, a)]
        for d in DELAYS:
            ex, rev, cap, above, reasons = [], 0, [], 0, Counter()
            for r in rs:
                s = r["sims"].get(a, {}).get(d)
                if s is None:
                    continue
                if not s.get("ok"):
                    rev += 1
                    reasons[str(s.get("error", "?"))[:30]] += 1
                    continue
                e = exp_out(r, a)
                ex.append(bps(s["amount_out"], e))
                above += s["amount_out"] > e * (1 + 1e-6)  # more than quote by > 0.01 bps
                cap.append(captured(r, a, s) / e * 1e4)
            n = len(ex) + rev
            print(f"{a:5} +{int(d) // 60:>2}m n={n} revert {rev} ({rev / max(n, 1) * 100:.0f}%)  median {fmt(pct(ex, .5))} p10 {fmt(pct(ex, .1))} p5 {fmt(pct(ex, .05))}"
                  f"  | swapper got > quote: {above}  | surplus taken in {sum(c > 0 for c in cap)} sims, max {fmt(max(cap, default=None), 3)} bps"
                  + (f"  | top revert reasons {dict(reasons.most_common(3))}" if reasons else ""))

    print("\n## Per pair (median over rounds): Kyber vs oracle; realized at t0 vs Kyber, bps; failures")
    others = aggs[1:]
    print("pair | size | K vs orc | " + " | ".join(f"{a}-K t0" for a in others) + (" | 0x w/o fee | 0x fee" if "0x" in aggs else "")
          + " | no quote " + "/".join(aggs) + " | t0 revert " + "/".join(aggs))
    byc = defaultdict(list)
    for r in rows:
        byc[(r["case"]["pair"], r["case"]["sizeHuman"])].append(r)
    for (pair, size), rs in sorted(byc.items()):
        def med(a):
            v = [bps(sa["amount_out"], sk["amount_out"]) for r in rs
                 if (sk := r["sims"].get("kyber", {}).get("0", {})).get("ok") and (sa := r["sims"].get(a, {}).get("0", {})).get("ok")]
            return statistics.median(v) if v else None
        korc = [bps(exp_out(r, "kyber"), r["oracle_out"]) for r in rs if exp_out(r, "kyber") and r["oracle_out"]]
        cols = [pair, f"{size:g}", fmt(statistics.median(korc) if korc else None)] + [fmt(med(a)) for a in others]
        if "0x" in aggs:
            fees = [f for r in rs if exp_out(r, "0x") and (f := fee_bps_0x(r)) is not None]
            m0 = med("0x")
            cols += [fmt(m0 + statistics.median(fees) if m0 is not None and fees else None), fmt(statistics.median(fees) if fees else None)]
        cols.append("/".join(str(sum(1 for r in rs if not exp_out(r, a))) for a in aggs))
        cols.append("/".join(str(sum(1 for r in rs if exp_out(r, a) and not r["sims"].get(a, {}).get("0", {}).get("ok"))) for a in aggs))
        print(" | ".join(cols))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="?", default="results3.jsonl", help="file in ./data")
    ap.add_argument("--override", help="jsonl in ./data whose pairs replace the same pairs of `results`")
    a = ap.parse_args()
    main(load_rows(a.results, a.override))
