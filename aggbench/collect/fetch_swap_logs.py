"""Step 2: download every historical `Swap` event of the matched SwapModules.

event Swap(Params params, address router, uint256 amountOut) with
Params = (tokenIn, tokenOut, amountIn, minAmountOut, deadline) — all non-indexed, so the data is 7 static words.
Logs come from the Etherscan v2 API (one call per module, paged by 1000) because public RPCs reject
full-range eth_getLogs. Token symbol/decimals are read from the chain.

Input:  data/modules.json (aggbench.collect.discover_modules)
Output: data/swaps.json — one row per swap: chain, module, vault, block, ts, tx, tokens/symbols/decimals,
        amountIn, minAmountOut, router, amountOut (amounts as decimal strings)
"""
import json
from collections import Counter

from web3 import Web3

from aggbench.chain import etherscan
from aggbench.config import DATA, ETHERSCAN_CHAIN_ID, RPCS

TOPIC = "0x" + Web3.keccak(text="Swap((address,address,uint256,uint256,uint256),address,uint256)").hex().removeprefix("0x")
ERC20 = [{"name": n, "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": t}]}
         for n, t in [("symbol", "string"), ("decimals", "uint8")]]
NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


def main():
    modules = [m for m in json.load(open(DATA / "modules.json")) if m["match"]]
    swaps = []
    for m in modules:
        cid, page = ETHERSCAN_CHAIN_ID[m["chain"]], 1
        while True:
            logs = etherscan(cid, module="logs", action="getLogs", fromBlock=0, toBlock="latest", address=m["module"],
                             topic0=TOPIC, page=page, offset=1000)
            for lg in logs:
                d = bytes.fromhex(lg["data"][2:])
                w = [d[i:i + 32] for i in range(0, len(d), 32)]
                swaps.append({"chain": m["chain"], "module": m["module"], "vault": m["vault"],
                              "block": int(lg["blockNumber"], 16), "ts": int(lg["timeStamp"], 16), "tx": lg["transactionHash"],
                              "tokenIn": Web3.to_checksum_address(w[0][-20:]), "tokenOut": Web3.to_checksum_address(w[1][-20:]),
                              "amountIn": int.from_bytes(w[2], "big"), "minAmountOut": int.from_bytes(w[3], "big"),
                              "router": Web3.to_checksum_address(w[5][-20:]), "amountOut": int.from_bytes(w[6], "big")})
            if len(logs) < 1000:
                break
            page += 1
        print(f"{m['chain']} {m['module']}: {sum(s['module'] == m['module'] for s in swaps)} swaps")

    # token metadata, per chain
    meta = {}
    for chain in {s["chain"] for s in swaps}:
        w3 = Web3(Web3.HTTPProvider(RPCS[chain]))
        for t in {s[k] for s in swaps if s["chain"] == chain for k in ("tokenIn", "tokenOut")}:
            if t.lower() == NATIVE:
                meta[(chain, t)] = ("ETH", 18)
                continue
            c = w3.eth.contract(address=t, abi=ERC20)
            meta[(chain, t)] = (c.functions.symbol().call(), c.functions.decimals().call())
    for s in swaps:
        s["symIn"], s["decIn"] = meta[(s["chain"], s["tokenIn"])]
        s["symOut"], s["decOut"] = meta[(s["chain"], s["tokenOut"])]
        for k in ("amountIn", "minAmountOut", "amountOut"):
            s[k] = str(s[k])
    json.dump(swaps, open(DATA / "swaps.json", "w"), indent=1)
    print(f"total {len(swaps)} swaps; routers: {dict(Counter(s['router'] for s in swaps))}")
    for (a, b), n in Counter((s["symIn"], s["symOut"]) for s in swaps).most_common():
        print(f"  {a}->{b}: {n}")


if __name__ == "__main__":
    main()
