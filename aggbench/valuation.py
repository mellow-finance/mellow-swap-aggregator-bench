"""Turning bps effects into $/yr and APR: prices, historical-volume mapping, vault TVL."""
import json
from collections import defaultdict

import requests

from aggbench.config import DATA, RPCS, STABLES


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


def sizes_by_pair(keys):
    """{pair: [benchmarked amountIn, ...]} from (pair, amountIn) keys."""
    out = defaultdict(list)
    for pair, amount in keys:
        out[pair].append(amount)
    return out


def nearest_size(sizes, pair, amount):
    """Map a historical swap to the closest benchmarked size of the same pair (None if the pair was not benchmarked)."""
    if pair not in sizes:
        return None
    return min(sizes[pair], key=lambda a: abs(a - amount))


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
