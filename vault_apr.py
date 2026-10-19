"""Step 7: APR uplift per vault from the router race (race.py), using each vault's OWN historical swaps.

APR uplift (bps) = annualized race gain on the vault's swaps / current vault TVL (common.vault_tvls, on-chain).
Turnover = annual swap volume / TVL — the gain scales linearly with it.

Usage: python3 vault_apr.py [results3.jsonl] [--refresh-tvl]
"""
import json
import sys

from common import DATA, annualize_factor, load_prices, load_rows, vault_tvls
from race import dollars, race_case_gains

STRATS = ("K+F", "K+F+0x", "K+F+0x(no fee)")


def main(path, refresh):
    case_gain = race_case_gains(load_rows(path))[0]
    hist = [s for s in json.load(open(DATA / "swaps.json")) if s["chain"] == "ethereum"]
    yr = annualize_factor(hist)
    _, _, per_vault = dollars(case_gain, STRATS, hist, load_prices())
    tvl = vault_tvls(refresh)
    print(f"{'vault':44} {'name':10} {'base':5} {'TVL $M':>8} {'swap vol $M/yr':>15} {'turnover':>9} | "
          f"{'race $/yr':>10} {'APR +bps':>8} | {'no-fee-0x $/yr':>14} {'APR +bps':>8}")
    tot_g = tot_t = 0.0
    for v, d in sorted(per_vault.items(), key=lambda kv: -kv[1]["vol"]):
        t = tvl[v]
        g, gnf = d.get("K+F+0x", 0) * yr, d.get("K+F+0x(no fee)", 0) * yr
        tot_g += g
        tot_t += t["usd"]
        print(f"{v:44} {t['name']:10} {t['base']:5} {t['usd'] / 1e6:8.1f} {d['vol'] * yr / 1e6:15.1f} {d['vol'] * yr / t['usd']:8.1f}x | "
              f"{g:10,.0f} {g / t['usd'] * 1e4:8.3f} | {gnf:14,.0f} {gnf / t['usd'] * 1e4:8.3f}")
    print(f"total TVL ${tot_t / 1e6:.1f}M, race K+F+0x ${tot_g:,.0f}/yr -> +{tot_g / tot_t * 1e4:.3f} bps APR")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "results3.jsonl", "--refresh-tvl" in sys.argv)
