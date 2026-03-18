// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IRouter {
    function swapExactETHForTokensSupportingFeeOnTransferTokens(
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external payable;

    function swapExactTokensForETHSupportingFeeOnTransferTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external;
}

interface IERC20 {
    function balanceOf(address) external view returns (uint);
    function approve(address, uint) external returns (bool);
}

contract HoneypotChecker {
    receive() external payable {}

    /// @notice Simulate buy+sell in one call. Reverts if token is honeypot.
    /// @return amountBought Tokens received from buy
    /// @return ethReceived ETH received from sell
    function check(
        address router,
        address token,
        address weth
    ) external payable returns (uint256 amountBought, uint256 ethReceived) {
        require(msg.value > 0, "need ETH");

        address[] memory buyPath = new address[](2);
        buyPath[0] = weth;
        buyPath[1] = token;

        // Step 1: Buy tokens
        IRouter(router).swapExactETHForTokensSupportingFeeOnTransferTokens{value: msg.value}(
            0,
            buyPath,
            address(this),
            block.timestamp + 3600
        );

        amountBought = IERC20(token).balanceOf(address(this));
        require(amountBought > 0, "buy failed");

        // Step 2: Approve router
        IERC20(token).approve(router, type(uint256).max);

        // Step 3: Sell all tokens
        address[] memory sellPath = new address[](2);
        sellPath[0] = token;
        sellPath[1] = weth;

        uint256 ethBefore = address(this).balance;

        IRouter(router).swapExactTokensForETHSupportingFeeOnTransferTokens(
            amountBought,
            0,
            sellPath,
            address(this),
            block.timestamp + 3600
        );

        ethReceived = address(this).balance - ethBefore;
        require(ethReceived > 0, "sell failed");
    }
}
