"""Shared config and helpers for the DEX-aggregator benchmark (KyberSwap vs Fynd vs 0x) on Mellow SwapModule flows.

Pipeline (run from anywhere, all files live in ./data next to this script):
  1. discover_modules.py   SwapModules created by the factories and owned by our vaults        -> data/modules.json
  2. fetch_swap_logs.py    historical SwapModule `Swap` events (pairs, sizes, router)            -> data/swaps.json
  3. bench.py              live benchmark: quote all aggregators, simulate now / +5m / +10m      -> data/results*.jsonl
  4. analyze.py            availability, latency, quote/realized head-to-head, reverts, fees
  5. hist_kyber.py         per historical swap: Kyber surplus capture, quote->execution delay,
                           realized vs Kyber's own quote                                          -> data/hist.json
  6. race.py               gain of picking the best realized route (best-of-N) vs Kyber-only
  7. vault_apr.py          TVL per vault (on-chain) and APR uplift of the race
  8. ps_extrapolate.py     Kyber positive-slippage capture extrapolated to $/yr and APR
  9. delay_cost.py         value of executing a quote within ~60s instead of the production delay
 10. fynd_vs_kyber_delay.py Fynd vs Kyber along the execution-delay axis
 debug_route.py           trace a failing route / compare tx.origin = module vs curator EOA

Requirements: python3 + requirements.txt. Secrets are read by name (common.ENV_KEYS) from ./.env, or the file in
BENCH_ENV_FILE, and from the process environment. Methodology and results: README.md.
"""
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
ENV_KEYS = ("ETH_RPC", "PLASMA_RPC", "MANTLE_RPC", "ROBIN_RPC", "ETHERSCAN_API_KEY", "FYND_API_KEY", "ZERO_X_API_KEY")


def _load_env():
    """Variables from the .env file (BENCH_ENV_FILE or ./.env, see .env.example); process env vars take precedence."""
    env = {}
    path = Path(os.environ.get("BENCH_ENV_FILE", HERE / ".env"))
    if path.exists():
        for line in open(path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    env.update({k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)})
    return env


ENV = _load_env()
RPCS = {"ethereum": ENV.get("ETH_RPC"), "plasma": ENV.get("PLASMA_RPC"), "mantle": ENV.get("MANTLE_RPC"), "robinhood": ENV.get("ROBIN_RPC")}
ETHERSCAN_CHAIN_ID = {"ethereum": 1, "plasma": 9745, "mantle": 5000}

# SwapModule factories: Ethereum uses 0xE357..., the L2 deployments use 0xC5a5... (see scripts/*/Constants.sol)
FACTORIES = {"ethereum": "0xE3575055a24d8642DFA3a51ec766Ef2db2671659", "plasma": "0xC5a52E4bB718Dfe86938e5cB967362EdA1E62698",
             "mantle": "0xC5a52E4bB718Dfe86938e5cB967362EdA1E62698", "robinhood": "0xC5a52E4bB718Dfe86938e5cB967362EdA1E62698"}
VAULTS = [  # vaults whose swap modules define the benchmarked pairs
    "0x277C6A642564A91ff78b008022D65683cEE5CCC5", "0x6a37725ca7f4CE81c004c955f7280d5C704a249e", "0x014e6DA8F283C4aF65B2AA0f201438680A004452",
    "0xDF0fb76Df2c21F79798949A4E886cd22D1C085d7", "0x0cC65147BF7F615A8dD9E78e2c53158F8E01754d",
    "0x49DAb986A4288bE616f44733d56397d3410fD331",  # plasma
    "0xF9DD401ff0f806b71dE62a936c34B930d0876022",  # mantle
    "0xEe94bb5a8487954312c470F2cb457fd7329B421F",  # robinhood
    "0x841e213864046111E43d237703d71FaBe91Ef9e0",  # plasma
]
CURATOR_EOAS = ["0xe49384e6Aca6A26c82Eaa161A9211fA60fEee3Ef", "0xE98Be1E5538FCbD716C506052eB1Fd5d6fC495A3"]  # tx.origin of past swaps

# Where each aggregator parks the surplus above its quote (found by tracing tokenOut transfers in simulations):
#  - Kyber: executor 0x8f10b4... forwards it to this EIP-7702 EOA, the user gets quote + 1 wei
#  - Fynd: TychoRouterV3 credits it to its own ERC-6909 vault (FeeCalculator positive-slippage capture)
#  - 0x: the volume-fee recipient (also receives the 15 bps fee itself)
SURPLUS_SINK = {"kyber": "0x1111110f0f73c0b2ef09ec012eae758b3e03a902",
                "fynd": "0x1644d2477f809cc2c71bccfd6dc9497e3f83210d",
                "0x": "0xad01c20d5886137e056775af56915de824c8fce5"}
KYBER_ROUTER = "0x6131b5fae19ea4f9d964eac0408e4408b66337b5"
STABLES = ("USDC", "USDT", "USDe", "PYUSD")

_session = requests.Session()


def rpc(method, params, url=None, retries=5):
    """JSON-RPC call with simple retry/backoff (drpc occasionally drops requests)."""
    url = url or RPCS["ethereum"]
    for attempt in range(retries):
        try:
            r = _session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60).json()
            if "error" in r:
                raise RuntimeError(r["error"])
            return r["result"]
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)


def latest_block():
    return int(rpc("eth_blockNumber", []), 16)


def etherscan(chain_id, **params):
    """Etherscan v2 multichain API; 'No records found' is a valid empty answer."""
    q = {"chainid": chain_id, "apikey": ENV["ETHERSCAN_API_KEY"], **params}
    for _ in range(5):
        r = requests.get("https://api.etherscan.io/v2/api", params=q, timeout=60).json()
        if r.get("status") == "1" or r.get("message") == "No records found":
            return r["result"]
        time.sleep(1)
    raise RuntimeError(r)


# ---------------------------------------------------------------- math / formatting

def bps(a, b):
    return (a / b - 1) * 1e4


def pct(xs, p):
    """Linear-interpolated percentile, None on empty input."""
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


# ---------------------------------------------------------------- benchmark results

def load_rows(path, override=None):
    """Load a bench.py jsonl file into quote rows with all re-simulations attached.

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


# the two benchmark windows used in the study; first = Kyber/Fynd only, second = Kyber/Fynd/0x
DEFAULT_RUNS = [("results.jsonl", "results_fix.jsonl"), ("results3.jsonl", None)]


def load_runs(runs=DEFAULT_RUNS):
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


# ---------------------------------------------------------------- prices / TVL

def fetch_prices():
    """Current USD prices of the non-stable tokens (via Kyber route summaries), cached to data/prices.json."""
    url, usdc = "https://aggregator-api.kyberswap.com/ethereum/api/v1/routes", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    px = {}
    for sym, token in [("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), ("wstETH", "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"),
                       ("MORPHO", "0x58D97B57BB95320F9a05dC918Aef65434969c2B2"), ("sUSDe", "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497")]:
        rs = requests.get(url, params={"tokenIn": token, "tokenOut": usdc, "amountIn": str(10**18)},
                          headers={"x-client-id": "mellow-bench"}, timeout=30).json()["data"]["routeSummary"]
        px[sym] = float(rs["amountInUsd"])
    json.dump(px, open(DATA / "prices.json", "w"))
    return px


def load_prices():
    """USD prices for valuing volumes; stables are taken at $1, ETH-likes fall back to WETH."""
    path = DATA / "prices.json"
    px = json.load(open(path)) if path.exists() else fetch_prices()
    px.update({s: 1.0 for s in STABLES})
    px.setdefault("sUSDe", 1.2)
    for s in ("aEthWETH", "rsETH"):
        px.setdefault(s, px["WETH"])
    return px


def usd_in(swap, px):
    """USD value of a historical swap's input amount."""
    return int(swap["amountIn"]) / 10 ** swap["decIn"] * px.get(swap["symIn"], 0)


def annualize_factor(hist):
    """Multiply period totals by this to get per-year numbers (period = first..last historical swap)."""
    return 365 * 86400 / (max(s["ts"] for s in hist) - min(s["ts"] for s in hist))


def nearest_size(sizes_by_pair, pair, amount):
    """Map a historical swap to the closest benchmarked size of the same pair (None if pair not benchmarked)."""
    if pair not in sizes_by_pair:
        return None
    return min(sizes_by_pair[pair], key=lambda a: abs(a - amount))


def sizes_by_pair(keys):
    out = defaultdict(list)
    for pair, amount in keys:
        out[pair].append(amount)
    return out


def vault_tvls(refresh=False):
    """{vault: {"name", "base", "assets", "usd"}} for vaults that have swaps, from on-chain state, cached in data/tvl.json.

    TVL = shareManager.totalShares() * 1e18 / oracle.getReport(baseAsset).priceD18 (vault accounting:
    shares = assets * priceD18 / 1e18), converted to USD with load_prices()."""
    path = DATA / "tvl.json"
    if path.exists() and not refresh:
        return json.load(open(path))
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(RPCS["ethereum"]))
    A = lambda ts: [{"type": t} for t in ts]
    abi = [{"name": "shareManager", "type": "function", "stateMutability": "view", "inputs": [], "outputs": A(["address"])},
           {"name": "oracle", "type": "function", "stateMutability": "view", "inputs": [], "outputs": A(["address"])},
           {"name": "totalShares", "type": "function", "stateMutability": "view", "inputs": [], "outputs": A(["uint256"])},
           {"name": "supportedAssetAt", "type": "function", "stateMutability": "view", "inputs": A(["uint256"]), "outputs": A(["address"])},
           {"name": "getReport", "type": "function", "stateMutability": "view", "inputs": A(["address"]),
            "outputs": [{"type": "tuple", "components": A(["uint224", "uint32", "bool"])}]},
           {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": A(["string"])},
           {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": A(["uint8"])}]
    c = lambda a: w3.eth.contract(address=Web3.to_checksum_address(a), abi=abi)
    px = load_prices()
    out = {}
    for vault in sorted({s["vault"] for s in json.load(open(DATA / "swaps.json")) if s["chain"] == "ethereum"}):
        v = c(vault)
        sm, orc = v.functions.shareManager().call(), v.functions.oracle().call()
        asset = c(orc).functions.supportedAssetAt(0).call()
        price_d18 = c(orc).functions.getReport(asset).call()[0]
        native = asset.lower() == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        base = "ETH" if native else c(asset).functions.symbol().call()
        dec = 18 if native else c(asset).functions.decimals().call()
        assets = c(sm).functions.totalShares().call() * 10**18 / price_d18 / 10**dec
        unit_usd = px["WETH"] if "ETH" in base.upper() else px.get(base, 1.0)
        out[vault] = {"name": c(sm).functions.symbol().call(), "base": base, "assets": assets, "usd": assets * unit_usd}
    json.dump(out, open(path, "w"), indent=1)
    return out
