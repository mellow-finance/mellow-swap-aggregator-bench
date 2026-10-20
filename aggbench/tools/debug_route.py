"""Debug helper: re-quote one benchmark case on Kyber now and show why its calldata fails (or that it works).

  * callTracer trace (debug_traceCall with the same state overrides as aggbench.bench.simulate.simulate) — prints the top of the
    call tree and every reverting frame with its revert data; decode selectors with `cast 4byte <selector>`
    (e.g. Kelp's DailyMintLimitExceeded, 1inch/prop-AMM legs failing)
  * --origin-test: execute once with tx.origin = the module and once with tx.origin = a curator EOA (production),
    to rule out tx.origin-gated RFQ / prop-AMM liquidity

Usage: python3 -m aggbench.tools.debug_route "wstETH->USDT" [--origin-test]
"""
import argparse

import requests
from eth_abi import decode
from web3 import Web3

from aggbench.bench.quoters import KYBER_HEADERS, KYBER_URL, quote_kyber
from aggbench.bench.run import load_cases
from aggbench.bench.simulate import overrides, run_calldata
from aggbench.chain import latest_block, rpc
from aggbench.config import CURATOR_EOAS


def trace(case):
    q = quote_kyber(case)
    print("sources", q.get("sources"), "error", q.get("error"))
    if "data" not in q:
        return
    sender = Web3.to_checksum_address(case["sender"])
    tr = rpc("debug_traceCall", [{"from": sender, "to": sender, "data": run_calldata(case, q), "gas": hex(30_000_000)},
                                 hex(latest_block()), {"tracer": "callTracer", "stateOverrides": overrides(case, sender)}])

    def walk(c, depth=0):
        if c.get("error") or depth < 3:  # top of the tree + every failing frame
            print("  " * depth, c["type"], c["to"], c.get("input", "")[:10], "err=", c.get("error"),
                  (c.get("revertReason") or "")[:80], (c.get("output") or "")[:140] if c.get("error") else "")
        for sub in c.get("calls", []):
            walk(sub, depth + 1)
    walk(tr)


def origin_test(case):
    sender = Web3.to_checksum_address(case["sender"])
    for origin in (sender, Web3.to_checksum_address(CURATOR_EOAS[0])):
        # Kyber routes can depend on `origin` (exclusive RFQ liquidity), so rebuild with the same origin
        p = {"tokenIn": case["tokenIn"], "tokenOut": case["tokenOut"], "amountIn": str(case["amountIn"]), "origin": origin}
        rs = requests.get(f"{KYBER_URL}/routes", params=p, headers=KYBER_HEADERS, timeout=30).json()["data"]["routeSummary"]
        b = requests.post(f"{KYBER_URL}/route/build", headers=KYBER_HEADERS, timeout=30,
                          json={"routeSummary": rs, "sender": sender, "recipient": sender, "origin": origin, "slippageTolerance": 200}).json()["data"]
        q = {"to": b["routerAddress"], "data": b["data"], "expected_out": int(b["amountOut"])}
        res = rpc("eth_simulateV1", [{"blockStateCalls": [{"stateOverrides": overrides(case, origin),
                                                           "calls": [{"from": origin, "to": sender, "data": run_calldata(case, q), "gas": hex(30_000_000)}]}],
                                      "validation": False}, hex(latest_block())])[0]["calls"][0]
        label = "module" if origin == sender else "curator EOA"
        if res.get("status") != "0x1":
            print(f"tx.origin={label}: top-level failure {res.get('error')}")
            continue
        ok, out, *_ = decode(["bool", "uint256", "uint256", "uint256", "bytes"], bytes.fromhex(res["returnData"][2:]))
        print(f"tx.origin={label}: ok={ok}" + (f" realized vs quote {(out / q['expected_out'] - 1) * 1e4:+.2f} bps" if ok else ""),
              sorted({h["exchange"] for path in rs["route"] for h in path}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pair")
    ap.add_argument("--origin-test", action="store_true")
    a = ap.parse_args()
    case = next(c for c in load_cases() if c["pair"] == a.pair)
    print(f"case {case['pair']} {case['sizeHuman']:g} from module {case['sender']}")
    origin_test(case) if a.origin_test else trace(case)
