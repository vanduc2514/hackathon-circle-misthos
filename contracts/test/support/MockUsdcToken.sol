// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title MockUsdcToken
 * @notice The 6-decimal ERC-20 view of USDC, standing in for the predeploy at
 *         0x3600...0000 so the money paths can be fuzzed without a network.
 *
 * @dev Arc has two views of one balance: native gas at 18 decimals and this
 *      ERC-20 at 6. The escrow only ever touches this one, which is why the
 *      amounts here are the amounts the contract reasons about.
 */
contract MockUsdcToken {
    string public constant name = "USD Coin";
    string public constant symbol = "USDC";
    uint8 public constant decimals = 6;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}
