# mellow-swap-aggregator-bench

Benchmark of DEX aggregators — **KyberSwap**, **Fynd** (Propeller Heads, hosted API) and **0x** (Swap API v2) — on the
real swap flow of Mellow flexible-vault `SwapModule`s on Ethereum.

What it measures:

- **Quote**: availability, price versus the module oracle and versus the other aggregators, quote latency.
- **Real execution** of the quote: right after it was quoted, and again after 5 and 10 minutes with the same calldata.
- **Positive slippage**: whether output above the quote reaches the vault or is kept by the aggregator.
- **What it is worth for the vaults** in $/yr and APR:
  - racing the routers (best-of-N);
  - keeping the positive slippage;
  - executing quotes within ~60 s instead of the current delay.

## Results snapshot (2026-10-19, Ethereum mainnet)

The run had 38 cases (18 pairs × historical sizes, $70k–$40M) × 6 rounds. Each quote was executed on the same block
immediately, then again at +5 and +10 min. There were two benchmark windows: Kyber and Fynd, then Kyber, Fynd and 0x.

| Metric | Kyber | Fynd | 0x |
|---|---|---|---|
| Quote returned | 100% | 77% | 92% |
| Usable quote (clears the module oracle bound and executes) | **89%** | 57% | 75% |
| Quote latency, p50 / p90 / p99, ms | 1731 / 4237 / 8812 | **141** / 726 / 1527 | 333 / **499 / 616** |
| Realized vs own quote on immediate execution, mean / p5, bps | −0.47 / −2.5 | −3.98 / −18.9 | **0.00 / 0.00** |
| Realized vs Kyber, same block, median / mean, bps | — | −0.09 / −0.27 | −14.9 / −11.4 (incl. fee); ≈0 without the fee |
| Still executable at +10 min (within quote − 1 bps) | 79% | 72% | 0% (calldata deadline 300 s) |
| Explicit fee | 0 | 0 | 15 bps on 12 of 17 pairs (0 on sUSDe/syrup pairs) |
| Positive slippage | taken: surplus above ~0.01 bps goes to the collector, the user gets quote + 1 wei | taken in full (`FeeCalculator`, exemption is per address) | collected on the Standard plan |

Where each aggregator stands out:

- **Fynd**
  - Wins only on USDC/USDT → USDe at $2–3M+, by +5–7 bps.
  - Returns `success` with −56…−99.97% quotes for sUSDe, rsETH and syrupUSDT, because Tycho does not index the native
    deposit/mint paths for them.
  - Has no route for syrupUSDC 1.5M or wstETH → aEthWETH.
- **Kyber**
  - Reverts on WETH → rsETH: the route goes through the Kelp deposit pool, which hits `DailyMintLimitExceeded`.
  - Its prop-AMM legs fail on wstETH → USDT.
- **0x**
  - Returns absurd syrupUSDT quotes.
  - Has no route for rsETH.

**Matcha** is 0x's consumer front end, not a separate engine:
- **API.** It has no trading API of its own ("Matcha uses the 0x API"). Matcha Auto runs on 0x Tx Relay.
- **Not usable from a SwapModule.** Standard mode is a user-wallet tx, and Auto mode needs a user signature, which a
  SwapModule cannot give (no EIP-1271).
- **Fees.** 0% on stablecoin ↔ stablecoin, 25 bps on other pairs. It "may capture a portion" of positive slippage.

Applying Matcha's fees to the 0x route (`aggbench.analysis.matcha`):
- **Stable ↔ stable:** median 0.00, mean **+0.42 bps** vs Kyber; better in 38 cases, worse in 10, ties in 35.
- **Other pairs:** −25 bps vs Kyber.
- **Takeaway:** without the API fee, the 0x route beats Kyber on stable pairs. Getting the 0x API fee waived on stables
  (as Matcha does for its users) would make 0x the best router for those pairs.

Value estimates for the three vaults with swaps (strETH, earnUSDe, earnUSDc; $265M TVL, $2.1B/yr swap volume):

| Scenario | $/yr | APR uplift |
|---|---|---|
| Race Kyber + Fynd + 0x, execute the best realized route (simulate off-chain, send the winner) | ~$8.3k | +0.31 bps |
| … with the 0x fee waived | ~$17.6k | +0.66 bps |
| Keep the positive slippage Kyber takes today | ~$1.0k (simulated) – $8.2k (actual rate) | +0.04 – 0.31 bps |
| Execute Kyber quotes within 60 s instead of the ~8 min median delay | ~$3.5k | +0.13 bps |
| Replace Kyber with Fynd, executed within 60 s | −$33k | −1.2 bps |

## Scope

- **Vaults.** These are listed in `aggbench/config.py` (`VAULTS`).
  - Ethereum: `0x277C…` (strETH), `0x6a37…`, `0x014e…`, `0xDF0f…` (earnUSDc), `0x0cC6…` (earnUSDe).
  - Plasma: `0x49DA…`, `0x841e…`.
  - Mantle: `0xF9DD…`.
  - Robinhood: `0xEe94…`.
- **Swap modules.** Modules were enumerated from the SwapModule factories: `0xE357…` on Ethereum and `0xC5a5…` on the
  L2s. A module belongs to a vault when `module.subvault().vault()` is in the list.
  - 11 modules matched: 9 on Ethereum, 1 on Plasma, 1 on Mantle.
  - `0x6a37…`, `0x014e…`, `0x841e…` and the Robinhood vault have no swap module.
- **History.** All 212 historical swaps were on Ethereum, between 2025-12 and 2026-10. All of them went through the Kyber
  router `0x6131B5fa…`.
  - The Plasma and Mantle modules have no `Swap` events.
  - Fynd does not support Plasma or Mantle anyway.
  - The benchmark is therefore Ethereum-only.
- **Pairs.** 18 pairs:
  - stables ↔ USDe / sUSDe / PYUSD / syrupUSDC / syrupUSDT;
  - WETH → wstETH / rsETH;
  - wstETH → aEthWETH / USDT;
  - USDe → WETH;
  - MORPHO → USDC.

## Methodology

### 1. Cases

Each pair is benchmarked at its historical p25, median and max input size. Sizes are rounded to 2 significant digits,
and sizes within 25% of each other are merged; pairs with fewer than 4 swaps use only their max size. This gives 38
cases. The swapper (sender and recipient) is the real SwapModule that executed the pair most recently, so calldata is
built for exactly the address that would execute it.

### 2. Quoting

All aggregators are quoted in parallel for each case. Client wall-clock latency is measured with keep-alive sessions.
Router-level slippage is 2%, set loose on purpose so that we observe the real output instead of a revert.

| Aggregator | Request | Fields used |
|---|---|---|
| Kyber | `GET /routes` (origin = module), then `POST /route/build` (sender = recipient = module, `slippageTolerance=200`, default deadline +20 min). Latency is route + build. | `expected_out` = build `amountOut`; route sources |
| Fynd | `POST /v1/ethereum/quote` with `encoding_options.slippage="0.02"` and default solver options. One call returns the TychoRouter calldata. | `expected_out` = `amount_out − router_fee` (router fee is 0); `solve_time_ms`, `price_impact_bps` |
| 0x | `GET /swap/allowance-holder/quote` (taker = module, `slippageBps=200`). Allowance-holder is a plain approve + call, which fits `SwapModule`. | `expected_out` = `buyAmount`, already net of `zeroExFee` |

### 3. Execution simulation

Every quote is executed with `eth_simulateV1` on top of one pinned block shared by all aggregators of the case, i.e. in
the next block.

- **Code at the module address.** The module address gets `Sim.sol`'s runtime code through a state override. `Sim.run`
  reproduces `SwapModule.swap`: it resets the approval and approves `amountIn` to the router, calls the router with
  the aggregator calldata, and measures the tokenOut balance delta and the gas used.
- **Funding.** The module receives `amountIn` of tokenIn by overriding the token's balance storage slot. The slot is
  found generically: `eth_createAccessList` on `balanceOf(module)` lists candidate slots, and each candidate is
  probe-written until `balanceOf` returns the probe. This works for proxies (USDC) and ERC-7201 layouts.
- **Sender.** `msg.sender` at the router is the module, as in production. Using a curator EOA as `tx.origin` instead of
  the module made no difference (`aggbench.tools.debug_route --origin-test`).
- **Transfers.** `traceTransfers` returns every token transfer. For tokenOut we record transfers that leave the routers
  to third parties, and every address with a **net tokenOut gain** other than the swapper. This is how the
  surplus/fee sinks were found:

| Aggregator | Where the surplus or fee goes |
|---|---|
| Kyber | Executor `0x8f10b4…` forwards the surplus to `0x1111110f…`, an EIP-7702 EOA fed almost only by that executor. The user receives quote + 1 wei. |
| Fynd | `TychoRouterV3` `0x1644d2…` credits it to `routerFeeReceiver` in its ERC-6909 vault, so it never shows up as a transfer out. `FeeCalculator` `0x2930cb…` has `getPositiveSlippageEnabled() = true` since 2026-09-04, with per-address exemptions resolved by `tx.origin`. |
| 0x | The volume-fee recipient `0xad01c2…`. |

### 4. Delayed execution

The **same calldata**, with no re-quote, is executed again at quote time + 300 s and + 600 s, each time on a fresh block.
This models the production bot, whose median quote → execution delay is ~8 min (p25 4.6 min, p75 14 min). The delay is
measured from the `Timestamp` in Kyber's `clientData` on historical txs.

Six rounds start 3 minutes apart and overlap. The case order is shuffled every round.

### 5. Reference price and usability

`SwapModule.checkParams` requires `minAmountOut ≥ evaluate(tokenIn, tokenOut, amountIn) × multiplier / 1e8`. The
default multiplier is 0.995.

- A quote **clears the oracle bound** if `expected_out` is at least that threshold; otherwise the module would reject
  the swap regardless of the router.
- A quote is **usable** if it clears the bound and its simulation at t0 succeeded.

Head-to-head comparisons only use cases where both quotes clear the bound, so absurd quotes do not dominate the means.

### 6. Metrics

- **Realized vs own quote**: `(amount_out / expected_out − 1) · 1e4` in bps. Negative values mean overquoting.
- **Realized vs Kyber**: the aggregator's amount out versus Kyber's, on the same block and with the same delay.
- **Executable within tolerance**: `realized ≥ quote − 1 bps`. The production bot passes a module `minAmountOut` of
  about quote − 1 bps (median; p10 −2 bps), measured from historical `Swap` events against Kyber's quoted
  `AmountOut` in `clientData`.
- **Surplus taken**: the aggregator sink's net tokenOut gain divided by `expected_out`. For 0x, its own fee is
  subtracted.
- **Cost of waiting** is `−E[min(drift, 0)]`, where `drift` = the change between fresh quotes for the same case a few
  minutes apart. The reasoning:
  - if the market moves up, the aggregator keeps the surplus;
  - if it moves down, the vault either executes inside the tolerance or reverts and re-quotes at the worse price;
  - so the vault bears only the downside.

  We deliberately do not use the stale-calldata loss with 2% slippage: the bot's tight `minAmountOut` turns those
  outcomes into reverts. That number is reported only as an upper bound.
- **Race**: per case, the best realized output among routes that executed and cleared the oracle bound, versus Kyber's
  realized output. The "by quote" variant instead picks the highest quote without simulation, which shows what
  overquotes cost.

### 7. Extrapolation to $/yr and APR

- Each historical swap is mapped to the nearest benchmarked size of its pair and gets that case's mean effect in bps.
- Results are summed over the 307-day history and annualized. USD values use current prices (Kyber route
  `amountInUsd`).
- **APR uplift** = annual $ / current vault TVL.
- TVL comes from `shareManager.totalShares() · 1e18 / oracle.getReport(baseAsset).priceD18`.

### 8. Historical (on-chain) checks

`aggbench.collect.hist_kyber` reads the receipt and calldata of every historical swap and extracts:
- tokenOut sent to Kyber's collector — the actual capture: 11 of 43 swaps since 2026-09-01, $331;
- the quote → execution delay;
- realized vs Kyber's quoted amount: mean −0.10 bps, worst −2.8;
- the bot's tolerance.

`aggbench.analysis.delay_cost` takes the curators' txs and computes the revert rate by delay. It is 0 of 23 below 60 s, 10–21%
between 1 and 20 min, and 27% above 20 min.

### Limitations

- **Short sample.** There were two windows of ~35 min each on a single day. Waiting cost and race gains scale with
  volatility and with the mix of large stable → USDe swaps.
- **Simulation.** The trade is simulated in the next block on top of the latest state. Tx ordering, MEV and
  builder-specific behaviour are not modelled. RFQ/prop-AMM legs behave as they would in the next block.
- **Noisy quotes.** Fynd's quote noise (~10 bps stdev between rounds) inflates its waiting cost. Means are sensitive to
  overquote tails, so medians are reported as well.
- **Fynd settings.** Fynd was used through the hosted API with default solver options. A self-hosted instance with a
  different algorithm or protocol set may behave differently.
- **0x fee.** The 0x fee and surplus policy correspond to the Standard plan.

## How to run

Run everything from the repo root as modules:

```bash
pip install -r requirements.txt
cp .env.example .env              # or: export BENCH_ENV_FILE=/path/to/existing/.env

# collect on-chain inputs
python3 -m aggbench.collect.discover_modules   # modules of our vaults           -> data/modules.json
python3 -m aggbench.collect.fetch_swap_logs    # historical Swap events          -> data/swaps.json
python3 -m aggbench.collect.hist_kyber         # Kyber capture, delay, tolerance -> data/hist.json

# live benchmark (~35 min)
python3 -m aggbench.bench.run --rounds 6 --spacing 180 --delays 300,600 --out results3.jsonl

# analyses
python3 -m aggbench.analysis.summary results3.jsonl   # availability, latency, head-to-head, reverts, fees
python3 -m aggbench.analysis.race                     # best-of-N race
python3 -m aggbench.analysis.vault_apr                # TVL and APR uplift of the race -> data/tvl.json
python3 -m aggbench.analysis.positive_slippage        # Kyber positive slippage, $/yr and APR
python3 -m aggbench.analysis.delay_cost               # value of executing within 60 s
python3 -m aggbench.analysis.fynd_vs_kyber            # Fynd vs Kyber by execution delay
python3 -m aggbench.analysis.matcha                   # Matcha-equivalent pricing from the 0x results

# debugging
python3 -m aggbench.tools.debug_route "WETH->rsETH" [--origin-test]   # trace a failing route
```

To smoke-test the benchmark, run `python3 -m aggbench.bench.run --max-cases 3 --delays "" --out smoke.jsonl`.

To reproduce the snapshot above from the committed data:

```bash
python3 -m aggbench.analysis.summary results.jsonl --override results_fix.jsonl
python3 -m aggbench.analysis.summary results3.jsonl
```

The first window (`results.jsonl`) had a balance-slot bug for 9 cases; those cases were re-run into
`results_fix.jsonl`.

## Layout

```
contracts/
  Sim.sol               swap simulator placed at the SwapModule address (rebuild command inside)
  Sim.runtime.hex       its runtime bytecode, used by the simulation
aggbench/
  config.py             paths, secrets (.env), vaults, factories, curator EOAs, surplus sinks
  chain.py              JSON-RPC and Etherscan v2 with retries
  stats.py              bps / percentile / formatting helpers
  results.py            loading benchmark results; quote accessors (oracle bound, 0x fee, captured surplus)
  valuation.py          prices, historical-volume mapping, annualization, vault TVL
  collect/              discover_modules, fetch_swap_logs, hist_kyber          (on-chain inputs -> data/)
  bench/                quoters (Kyber / Fynd / 0x API clients), simulate (eth_simulateV1), run (runner, CLI)
  analysis/             summary, race, vault_apr, positive_slippage, delay_cost, fynd_vs_kyber, matcha
  tools/                debug_route
data/                   inputs and raw results of the 2026-10-19 study
```
