"""Matcha-equivalent pricing, derived from the 0x results.

Matcha is 0x's consumer front end: it has no trading API of its own ("Matcha uses the 0x API", Matcha help center) and
routes through the same 0x engine (Matcha Auto = 0x Tx Relay / gasless). It cannot be called from a SwapModule —
Standard mode is a user-wallet tx, Auto mode needs a user signature (SwapModule has no EIP-1271) — so the only thing
that differs for us is the FEE SCHEDULE. Matcha's published fees (help center, 2026): 0% for stablecoin<->stablecoin
swaps, 0.25% for other pairs, and it "may capture a portion" of positive slippage. The 0x API Standard plan instead
charges 15 bps on most pairs, stables included.

Matcha-equivalent output = 0x realized output + 0x API fee (gross route output) - Matcha fee, compared with Kyber's
realized output on the same block, on cases where both quotes clear the module oracle bound.
Stable<->stable = both tokens in config.STABLES (USDC, USDT, USDe, PYUSD); sUSDe/syrup are treated as non-stable.

Usage: python3 -m aggbench.analysis.matcha [results3.jsonl]
"""
import statistics
import sys
from collections import defaultdict

from aggbench.config import STABLES
from aggbench.results import fee_bps_0x, load_rows, sane
from aggbench.stats import bps

MATCHA_FEE_BPS = {"stable": 0.0, "other": 25.0}


def main(path):
    res = defaultdict(list)
    for r in load_rows(path):
        if not (sane(r, "kyber") and sane(r, "0x")):
            continue
        sk, sx = r["sims"]["kyber"].get("0", {}), r["sims"]["0x"].get("0", {})
        fee = fee_bps_0x(r)
        if not (sk.get("ok") and sx.get("ok")) or fee is None:
            continue
        a, b = r["case"]["pair"].split("->")
        cat = "stable" if a in STABLES and b in STABLES else "other"
        api = bps(sx["amount_out"], sk["amount_out"])  # what the 0x API delivered (net of its fee)
        gross = api + fee                              # the 0x route itself, no fee
        res[cat].append((api, gross, gross - MATCHA_FEE_BPS[cat], r["case"]["pair"]))
    for cat, v in res.items():
        api, gross, matcha, pairs = zip(*v)
        print(f"{cat} pairs (Matcha fee {MATCHA_FEE_BPS[cat]:g} bps): n={len(v)} {sorted(set(pairs))}")
        for name, x in (("0x API (Standard plan fee)", api), ("0x route, no fee", gross), ("Matcha-equivalent", matcha)):
            print(f"   {name:27} vs Kyber realized t0: median {statistics.median(x):+.2f} mean {statistics.mean(x):+.2f} bps"
                  f" | better {sum(y > .1 for y in x)} / worse {sum(y < -.1 for y in x)} / tie {sum(abs(y) <= .1 for y in x)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results3.jsonl")
