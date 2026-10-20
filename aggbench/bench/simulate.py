"""Executing aggregator calldata exactly as a SwapModule would, without sending anything on-chain.

eth_simulateV1 on top of a pinned block (i.e. the trade lands in the next block) with state overrides:
  * the SwapModule address gets contracts/Sim.sol's runtime code: approve router -> call router -> balance delta,
    the same sequence as SwapModule.swap, so msg.sender at the router is the module as in production
  * the module gets `amountIn` of tokenIn by overriding the token's balance storage slot (found generically)
traceTransfers returns all token transfers, used to see who besides the swapper ended up with tokenOut.
"""
import time
from collections import defaultdict

from eth_abi import decode, encode
from web3 import Web3

from aggbench.chain import rpc
from aggbench.config import CONTRACTS, KYBER_ROUTER, RPCS

SIM_CODE = "0x" + open(CONTRACTS / "Sim.runtime.hex").read().strip().removeprefix("0x")
RUN_SELECTOR = Web3.keccak(text="run(address,address,uint256,address,bytes)")[:4]
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

w3 = Web3(Web3.HTTPProvider(RPCS["ethereum"], request_kwargs={"timeout": 60}))


def eth_call(to, data, block="latest", overrides=None):
    params = [{"to": to, "data": data}, block]
    if overrides:
        params.append(overrides)
    return rpc("eth_call", params)


# ---------------------------------------------------------------- tokenIn balance override

_slot_cache = {}


def balance_slot(token, holder):
    """Storage slot holding balanceOf(holder) on `token` (works for proxies and ERC-7201 layouts).

    eth_createAccessList on balanceOf(holder) lists the slots it reads; each candidate is overridden with a probe
    value and the one that makes balanceOf return the probe wins. Overriding a proxy's implementation slot makes the
    call return garbage or empty data, hence the try/except."""
    key = (token.lower(), holder.lower())
    if key in _slot_cache:
        return _slot_cache[key]
    data = "0x70a08231" + encode(["address"], [holder]).hex()
    access_list = rpc("eth_createAccessList", [{"from": holder, "to": token, "data": data}, "latest"])
    probe = 0x1234567890ABCDEF1234567890
    for entry in access_list["accessList"]:
        if entry["address"].lower() != token.lower():
            continue
        for slot in entry["storageKeys"]:
            ov = {token: {"stateDiff": {slot: "0x" + probe.to_bytes(32, "big").hex()}}}
            try:
                got = int(eth_call(token, data, "latest", ov) or "0x0", 16)
            except Exception:
                continue
            if got == probe:
                _slot_cache[key] = slot
                return slot
    raise RuntimeError(f"balance slot not found for {token}")


def run_calldata(case, q):
    """Calldata of Sim.run(tokenIn, tokenOut, amountIn, router, aggregatorCalldata)."""
    return "0x" + (RUN_SELECTOR + encode(["address", "address", "uint256", "address", "bytes"],
                                         [case["tokenIn"], case["tokenOut"], case["amountIn"], Web3.to_checksum_address(q["to"]),
                                          bytes.fromhex(q["data"][2:])])).hex()


def overrides(case, origin=None):
    """State overrides: Sim code at the module, ETH for gas at the tx origin, tokenIn balance for the module."""
    sender = Web3.to_checksum_address(case["sender"])
    ov = {origin or sender: {"balance": hex(10**21)}}
    ov.setdefault(sender, {})["code"] = SIM_CODE  # merged: origin may be the module itself
    slot = balance_slot(case["tokenIn"], sender)
    ov[Web3.to_checksum_address(case["tokenIn"])] = {"stateDiff": {slot: "0x" + case["amountIn"].to_bytes(32, "big").hex()}}
    return ov


# ---------------------------------------------------------------- module oracle bound

MODULE_ABI = [
    {"name": "evaluate", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"name": "customMultiplier", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "defaultMultiplier", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]


def module_reference(module, tin, tout, amount, block):
    """Fair output per the module's oracle (SwapModule.evaluate) and the slippage multiplier checkParams applies."""
    c = w3.eth.contract(address=Web3.to_checksum_address(module), abi=MODULE_ABI)
    try:
        fair = c.functions.evaluate(tin, tout, amount).call(block_identifier=block)
    except Exception:  # oracle has no price for one of the tokens
        fair = None
    mult = c.functions.customMultiplier(tin, tout).call(block_identifier=block) or c.functions.defaultMultiplier().call(block_identifier=block)
    return fair, mult


# ---------------------------------------------------------------- simulation

def simulate(case, q, block):
    """Execute q's calldata from the module at `block` (eth_simulateV1 builds block+1 on top of it).

    Returns {block, t, status, ok, amount_out, gas_used, in_spent, router_leaks, net_gainers} or {ok: False, error}."""
    sender = Web3.to_checksum_address(case["sender"])
    params = [{"blockStateCalls": [{"stateOverrides": overrides(case),
                                    "calls": [{"from": sender, "to": sender, "data": run_calldata(case, q), "gas": hex(30_000_000)}]}],
               "traceTransfers": True, "validation": False}, hex(block)]
    t = time.time()
    call = rpc("eth_simulateV1", params)[0]["calls"][0]
    out = {"block": block, "t": t, "status": call.get("status")}
    if call.get("status") != "0x1" or not call.get("returnData") or call["returnData"] == "0x":
        out.update(ok=False, error=str(call.get("error"))[:300])
        return out
    ok, amount_out, gas_used, in_spent, ret = decode(["bool", "uint256", "uint256", "uint256", "bytes"], bytes.fromhex(call["returnData"][2:]))
    out.update(ok=ok, amount_out=amount_out, gas_used=gas_used, in_spent=in_spent)
    if not ok:  # router reverted: keep the reason (Error(string) decoded, custom errors as hex)
        out["error"] = "0x" + ret.hex()[:200]
        try:
            if ret[:4].hex() == "08c379a0":
                out["error"] = decode(["string"], ret[4:])[0]
        except Exception:
            pass
        return out
    # who else got tokenOut: transfers out of the routers to non-swapper addresses, and net tokenOut gainers
    tout, routers = case["tokenOut"].lower(), {q["to"].lower(), KYBER_ROUTER}
    leaks, net = [], defaultdict(int)
    for lg in call.get("logs", []):
        if lg["address"].lower() != tout or not lg["topics"] or lg["topics"][0] != TRANSFER_TOPIC or len(lg["topics"]) < 3:
            continue
        frm, to, amt = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:], int(lg["data"], 16)
        net[frm] -= amt
        net[to] += amt
        if frm in routers and to != sender.lower():
            leaks.append({"to": to, "amount": amt})
    out["router_leaks"] = leaks
    out["net_gainers"] = {a: v for a, v in net.items() if v > 0 and a != sender.lower() and a != "0x" + "0" * 40}
    return out
