// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MisthosEscrow} from "../src/MisthosEscrow.sol";

/// @dev USDC stand-in with the same 6 decimals as the Arc predeploy.
contract MockUSDC {
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

contract MisthosEscrowTest is Test {
    MisthosEscrow escrow;
    MockUSDC usdc;

    address constant ATTESTOR = address(0xA77E5);
    address constant PUBLISHER = address(0xB0B);
    address constant CONTRIBUTOR = address(0xC0FE);
    address constant REVIEWER = address(0x2E7);

    bytes32 constant ISSUE = keccak256("ISS-1001");

    uint256 constant FIX = 180_000_000; // 180.00 USDC at 6 decimals
    uint256 constant REVIEW_FEE = 36_000_000; // 36.00 USDC
    uint256 constant TOTAL = FIX + REVIEW_FEE;

    function setUp() public {
        usdc = new MockUSDC();
        escrow = new MisthosEscrow(ATTESTOR, address(usdc));

        usdc.mint(PUBLISHER, 1_000_000_000);
        vm.prank(PUBLISHER);
        usdc.approve(address(escrow), type(uint256).max);
    }

    function _commit() internal returns (uint64 deadline) {
        deadline = uint64(block.timestamp + 14 days);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, TOTAL, deadline);
    }

    // ----------------------------------------------------------------- commit

    function test_commit_holds_funds_and_records_the_commitment() public {
        uint64 deadline = _commit();

        assertTrue(escrow.isHeld(ISSUE));
        assertEq(usdc.balanceOf(address(escrow)), TOTAL);

        (address pub, uint96 amount, uint64 d, MisthosEscrow.Status status) =
            escrow.commitments(ISSUE);
        assertEq(pub, PUBLISHER);
        assertEq(amount, TOTAL);
        assertEq(d, deadline);
        assertEq(uint256(status), uint256(MisthosEscrow.Status.Held));
    }

    function test_commit_twice_for_the_same_issue_is_rejected() public {
        _commit();
        vm.expectRevert(MisthosEscrow.AlreadyExists.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, TOTAL, uint64(block.timestamp + 1 days));
    }

    function test_commit_with_a_zero_amount_is_rejected() public {
        vm.expectRevert(MisthosEscrow.ZeroAmount.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, 0, uint64(block.timestamp + 1 days));
    }

    function test_commit_with_a_past_deadline_is_rejected() public {
        vm.expectRevert(MisthosEscrow.DeadlinePassed.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, TOTAL, uint64(block.timestamp - 1));
    }

    function test_ceiling_caps_what_an_agent_can_commit() public {
        escrow.setCeiling(ISSUE, 100_000_000);
        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.ExceedsCeiling.selector, TOTAL, 100_000_000)
        );
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, TOTAL, uint64(block.timestamp + 1 days));
    }

    // ---------------------------------------------------------------- release

    function test_release_splits_the_commitment_between_contributor_and_reviewer() public {
        _commit();

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);

        assertEq(usdc.balanceOf(CONTRIBUTOR), FIX);
        assertEq(usdc.balanceOf(REVIEWER), REVIEW_FEE);
        assertEq(usdc.balanceOf(address(escrow)), 0);
        assertEq(uint256(escrow.statusOf(ISSUE)), uint256(MisthosEscrow.Status.Released));
    }

    function test_only_the_attestor_can_release() public {
        _commit();
        vm.expectRevert(MisthosEscrow.NotAttestor.selector);
        vm.prank(PUBLISHER);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);
    }

    function test_the_contributor_cannot_release_their_own_payment() public {
        _commit();
        vm.expectRevert(MisthosEscrow.NotAttestor.selector);
        vm.prank(CONTRIBUTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);
    }

    function test_release_after_the_deadline_is_refused() public {
        uint64 deadline = _commit();
        vm.warp(deadline + 1);
        vm.expectRevert(MisthosEscrow.DeadlinePassed.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);
    }

    function test_release_cannot_pay_out_more_than_was_committed() public {
        _commit();
        vm.expectRevert(MisthosEscrow.AmountMismatch.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX * 2, REVIEWER, REVIEW_FEE);
    }

    function test_release_twice_is_rejected() public {
        _commit();
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);

        vm.expectRevert(MisthosEscrow.NotHeld.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);
    }

    // ----------------------------------------------------------------- refund

    function test_refund_returns_funds_after_the_deadline() public {
        uint64 deadline = _commit();
        uint256 before = usdc.balanceOf(PUBLISHER);

        vm.warp(deadline + 1);
        escrow.refund(ISSUE);

        assertEq(usdc.balanceOf(PUBLISHER) - before, TOTAL);
        assertEq(uint256(escrow.statusOf(ISSUE)), uint256(MisthosEscrow.Status.Refunded));
    }

    function test_refund_before_the_deadline_is_refused() public {
        _commit();
        vm.expectRevert(MisthosEscrow.DeadlineNotReached.selector);
        escrow.refund(ISSUE);
    }

    function test_refund_of_an_unknown_issue_is_refused() public {
        vm.expectRevert(MisthosEscrow.NotHeld.selector);
        escrow.refund(keccak256("nope"));
    }

    function test_a_released_issue_cannot_be_refunded() public {
        _commit();
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);

        vm.warp(block.timestamp + 30 days);
        vm.expectRevert(MisthosEscrow.NotHeld.selector);
        escrow.refund(ISSUE);
    }

    // ------------------------------------------------------------------ admin

    function test_only_the_owner_can_rotate_the_attestor() public {
        vm.expectRevert(MisthosEscrow.NotOwner.selector);
        vm.prank(PUBLISHER);
        escrow.setAttestor(address(0xA11CE));
    }

    function test_rotating_the_attestor_moves_the_authority() public {
        address next = address(0xA11CE);
        escrow.setAttestor(next);
        _commit();

        vm.prank(ATTESTOR);
        vm.expectRevert(MisthosEscrow.NotAttestor.selector);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);

        vm.prank(next);
        escrow.release(ISSUE, CONTRIBUTOR, FIX, REVIEWER, REVIEW_FEE);
        assertEq(usdc.balanceOf(CONTRIBUTOR), FIX);
    }

    function test_constructor_rejects_zero_addresses() public {
        vm.expectRevert(MisthosEscrow.ZeroAddress.selector);
        new MisthosEscrow(address(0), address(usdc));

        vm.expectRevert(MisthosEscrow.ZeroAddress.selector);
        new MisthosEscrow(ATTESTOR, address(0));
    }
}
