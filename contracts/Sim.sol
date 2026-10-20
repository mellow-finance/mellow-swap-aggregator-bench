// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
}

/// @notice Swap simulator used by aggbench/bench/simulate.py. It is never deployed: its runtime bytecode
///         (Sim.runtime.hex) is put at the SwapModule address via an eth_simulateV1 state override, so the aggregator
///         calldata runs exactly as in production (msg.sender = the module, approve -> router call -> balance delta,
///         same as SwapModule.swap).
/// @dev Rebuild Sim.runtime.hex after editing (from the repo root):
///      forge build --root . --contracts contracts --out /tmp/sim-out --use 0.8.25 --evm-version cancun
///      jq -r .deployedBytecode.object /tmp/sim-out/Sim.sol/Sim.json > contracts/Sim.runtime.hex
contract Sim {
    address constant ETH = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE;

    /// @return ok       router call succeeded
    /// @return amountOut tokenOut received by this contract (the swapper)
    /// @return gasUsed  gas spent inside the router call
    /// @return inSpent  tokenIn actually pulled by the router
    /// @return ret      raw router return / revert data (decoded by simulate.py on failure)
    function run(address tokenIn, address tokenOut, uint256 amountIn, address router, bytes calldata data)
        external
        payable
        returns (bool ok, uint256 amountOut, uint256 gasUsed, uint256 inSpent, bytes memory ret)
    {
        uint256 value;
        if (tokenIn == ETH) {
            value = amountIn;
        } else {
            // reset first: USDT-style tokens reject changing a non-zero allowance
            _approve(tokenIn, router, 0);
            _approve(tokenIn, router, amountIn);
        }
        uint256 outBefore = _bal(tokenOut);
        uint256 inBefore = _bal(tokenIn);
        uint256 g = gasleft();
        (ok, ret) = router.call{value: value}(data);
        gasUsed = g - gasleft();
        if (ok) {
            amountOut = _bal(tokenOut) - outBefore;
            inSpent = inBefore - _bal(tokenIn);
        }
    }

    function _bal(address t) internal view returns (uint256) {
        return t == ETH ? address(this).balance : IERC20(t).balanceOf(address(this));
    }

    function _approve(address t, address s, uint256 a) internal {
        // low-level call: USDT's approve returns nothing
        (bool ok,) = t.call(abi.encodeWithSelector(0x095ea7b3, s, a));
        require(ok, "approve");
    }

    receive() external payable {}
}
