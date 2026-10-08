// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title MockErc20
 * @notice Any 6-decimal ERC-20 an issue can be denominated in, standing in for the
 *         EURC predeploy so the money paths can be driven across two balances
 *         without a network.
 *
 * @dev The escrow refuses a token whose `decimals()` is not 6, so the fuzz campaign
 *      needs a second legal token to prove the per-issue accounting is really per
 *      token, and this one is small enough to reason about.
 */
contract MockErc20 {
    string public name;
    string public symbol;
    uint8 public decimals;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory name_, string memory symbol_, uint8 decimals_) {
        name = name_;
        symbol = symbol_;
        decimals = decimals_;
    }

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
