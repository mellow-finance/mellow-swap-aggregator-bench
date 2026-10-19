"""Step 1: find the SwapModules that belong to our vaults.

For every chain, enumerate all entities of the SwapModule factory (Factory.entities/entityAt), read
module.subvault() -> subvault.vault() and keep modules whose vault is in common.VAULTS.
For matched modules also store the module oracle and the default slippage multiplier (1e8 = 100%).

Output: data/modules.json — one row per factory entity: {chain, module, subvault, vault, match, oracle?, defaultMultiplier?}
Usage:  python3 discover_modules.py [chain ...]      (default: all chains in common.FACTORIES)
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from web3 import Web3

from common import DATA, FACTORIES, RPCS, VAULTS

ABI = [{"name": n, "type": "function", "stateMutability": "view", "inputs": i, "outputs": [{"type": o}]} for n, i, o in [
    ("entities", [], "uint256"), ("entityAt", [{"type": "uint256"}], "address"), ("subvault", [], "address"),
    ("vault", [], "address"), ("oracle", [], "address"), ("defaultMultiplier", [], "uint256")]]
TARGET = {v.lower() for v in VAULTS}


def call(w3, addr, fn, *args):
    """View call that returns None instead of raising (non-module entities, EOAs, etc.)."""
    try:
        return w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ABI).functions[fn](*args).call()
    except Exception:
        return None


def scan(chain):
    w3 = Web3(Web3.HTTPProvider(RPCS[chain], request_kwargs={"timeout": 30}))
    factory = FACTORIES[chain]
    if len(w3.eth.get_code(Web3.to_checksum_address(factory))) == 0:
        print(f"{chain}: no factory code at {factory}")
        return []
    n = call(w3, factory, "entities") or 0

    def info(i):
        module = call(w3, factory, "entityAt", i)
        subvault = call(w3, module, "subvault")
        vault = call(w3, subvault, "vault") if subvault else None
        return {"chain": chain, "module": module, "subvault": subvault, "vault": vault}

    with ThreadPoolExecutor(16) as ex:
        rows = list(ex.map(info, range(n)))
    for r in rows:
        r["match"] = bool(r["vault"] and r["vault"].lower() in TARGET)
        if r["match"]:
            r["oracle"] = call(w3, r["module"], "oracle")
            r["defaultMultiplier"] = call(w3, r["module"], "defaultMultiplier")
    print(f"{chain}: {n} modules, {sum(r['match'] for r in rows)} belong to target vaults")
    return rows


if __name__ == "__main__":
    out = []
    for chain in sys.argv[1:] or list(FACTORIES):
        out += scan(chain)
    json.dump(out, open(DATA / "modules.json", "w"), indent=1)
    found = {r["vault"].lower() for r in out if r["match"]}
    print("vaults without a swap module:", [v for v in VAULTS if v.lower() not in found])
