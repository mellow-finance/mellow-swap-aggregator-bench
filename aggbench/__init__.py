"""DEX-aggregator benchmark (KyberSwap vs Fynd vs 0x) on Mellow SwapModule flows. Methodology: README.md.

Package layout (run every entry point from the repo root with `python3 -m aggbench.<sub>.<module>`):
  config, chain, stats, results, valuation   shared building blocks
  collect/   on-chain inputs: swap modules, historical swaps, Kyber history            -> data/
  bench/     live benchmark: aggregator API clients, SwapModule-equivalent simulator, runner
  analysis/  everything computed from the benchmark results and the on-chain history
  tools/     debugging helpers
"""
