"""Loading benchmark results (data/*.jsonl written by aggbench.bench.run) and per-row accessors."""
import json

from aggbench.config import DATA, SURPLUS_SINK

# the two benchmark windows of the study; first = Kyber/Fynd only, second = Kyber/Fynd/0x
DEFAULT_RUNS = [("results.jsonl", "results_fix.jsonl"), ("results3.jsonl", None)]


def load_rows(path, override=None):
    """Load a results jsonl into quote rows with all re-simulations attached.

    Each row: {round, case, oracle_out, multiplier, quotes: {agg: quote}, sims: {agg: {"0"|"300"|"600": sim}}}.
    `override`: a second jsonl whose pairs replace the same pairs of `path` (used for the first run, where
    9 cases had a broken balance-slot override and were re-run into results_fix.jsonl).
    """
    def read(p):
        quotes, resims = [], []
        for line in open(DATA / p):
            r = json.loads(line)
            (quotes if r["kind"] == "quote" else resims).append(r)
        idx = {(q["round"], q["case"]["pair"], q["case"]["amountIn"]): q for q in quotes}
        for r in resims:
            q = idx.get((r["round"], r["pair"], r["amountIn"]))
            if q:
                for agg, s in r["sims"].items():
                    q["sims"].setdefault(agg, {})[str(r["delay"])] = s
        return quotes

    rows = read(path)
    if override and (DATA / override).exists():
        fix = read(override)
        fixed_pairs = {r["case"]["pair"] for r in fix}
        rows = [r for r in rows if r["case"]["pair"] not in fixed_pairs] + fix
    return rows


def load_runs(runs=DEFAULT_RUNS):
    """[(path, rows)] for every run file that exists."""
    return [(p, load_rows(p, o)) for p, o in runs if (DATA / p).exists()]


def aggs_of(rows):
    """Aggregators present in a result set, Kyber first."""
    names = {a for r in rows for a in r["quotes"]}
    return ["kyber"] + sorted(names - {"kyber"})


def exp_out(r, agg):
    """Quoted amount the aggregator promises to deliver (Kyber: build amountOut, Fynd: amount_out - router fee,
    0x: buyAmount, already net of the 0x fee). None if there is no executable quote."""
    q = r["quotes"].get(agg, {})
    return q.get("expected_out") if "data" in q else None


def oracle_bound(r):
    """SwapModule.checkParams: minAmountOut must be >= oracle evaluate() * multiplier / 1e8."""
    return r["oracle_out"] * r["multiplier"] / 1e8 if r["oracle_out"] else None


def sane(r, agg):
    """Quote clears the module's oracle bound, i.e. a swap with this quote could pass checkParams at all."""
    e, b = exp_out(r, agg), oracle_bound(r)
    return bool(e and b and e >= b)


def fee_bps_0x(r):
    """0x volume fee in bps; buyAmount is net of the fee, so it is added back to get the gross output."""
    q = r["quotes"].get("0x", {})
    if not q.get("fee_amount"):
        return 0.0
    if q["fee_token"] == r["case"]["tokenOut"].lower():
        return q["fee_amount"] / (q["expected_out"] + q["fee_amount"]) * 1e4
    if q["fee_token"] == r["case"]["tokenIn"].lower():
        return q["fee_amount"] / r["case"]["amountIn"] * 1e4
    return None


def captured(r, agg, sim):
    """tokenOut kept by the aggregator's surplus sink in one simulation (0x: minus its own volume fee)."""
    gain = (sim.get("net_gainers") or {}).get(SURPLUS_SINK.get(agg, ""), 0)
    if agg == "0x":
        q = r["quotes"]["0x"]
        if q.get("fee_token") == r["case"]["tokenOut"].lower():
            gain -= q.get("fee_amount", 0)
    return max(gain, 0)
