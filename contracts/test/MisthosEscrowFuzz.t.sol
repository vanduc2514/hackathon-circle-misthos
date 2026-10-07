// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MisthosEscrow} from "../src/MisthosEscrow.sol";
import {MockUsdcToken} from "./support/MockUsdcToken.sol";

/**
 * @title MisthosEscrowFuzz
 * @notice The money paths, fuzzed. The example-based suite proves one input
 *         each; these prove the space around it — every amount the contract can
 *         hold, every deadline it can be given, and every key that can try to
 *         move the money.
 *
 * @dev Each test states the property it protects. Uses 6-decimal base units
 *      throughout, the only USDC view this contract touches.
 */
contract MisthosEscrowFuzz is Test {
    MisthosEscrow internal escrow;
    MockUsdcToken internal usdc;

    address internal constant ATTESTOR = address(0xA7705);
    address internal constant PUBLISHER = address(0xB0B);
    address internal constant CONTRIBUTOR = address(0xC0FE);

    bytes32 internal constant ISSUE = keccak256("ISS-1001");

    /// @dev Enough for any single commitment the fuzzer can pick.
    uint256 internal constant GRANT = type(uint96).max;

    function setUp() public {
        usdc = new MockUsdcToken();
        escrow = new MisthosEscrow(ATTESTOR, address(usdc));

        usdc.mint(PUBLISHER, GRANT);
        vm.prank(PUBLISHER);
        usdc.approve(address(escrow), type(uint256).max);
    }

    // ------------------------------------------------------------- amounts

    /// A commitment is held in full: the escrow's balance is exactly what the
    /// publisher committed, so nothing is rounded down, skimmed or added on the
    /// way in.
    function testFuzz_commit_holds_exactly_the_claimed_amount(uint96 amountSeed) public {
        uint256 amount = _amount(amountSeed);
        uint256 publisherBefore = usdc.balanceOf(PUBLISHER);

        _commit(amount, uint64(block.timestamp + 14 days));

        assertEq(usdc.balanceOf(address(escrow)), amount);
        assertEq(usdc.balanceOf(PUBLISHER), publisherBefore - amount);
        assertTrue(escrow.isHeld(ISSUE));
    }

    /// Above the ceiling, a commitment is refused whatever the amount is. This
    /// is the guardrail that holds when the agent's own key is compromised, so
    /// it may not depend on the size of the overrun.
    function testFuzz_a_commitment_above_the_ceiling_reverts(
        uint96 ceilingSeed,
        uint256 amountSeed
    ) public {
        uint256 ceiling = bound(ceilingSeed, 1, type(uint96).max - 1);
        escrow.setCeiling(ISSUE, ceiling);
        uint256 amount = bound(amountSeed, ceiling + 1, type(uint96).max);

        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.ExceedsCeiling.selector, amount, ceiling)
        );
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, amount, uint64(block.timestamp + 14 days));
    }

    /// At or below the ceiling, a commitment goes through: the guardrail caps a
    /// budget without blocking a price the publisher can actually afford.
    function testFuzz_a_commitment_within_the_ceiling_is_held(
        uint96 ceilingSeed,
        uint256 amountSeed
    ) public {
        uint256 ceiling = bound(ceilingSeed, 1, type(uint96).max);
        escrow.setCeiling(ISSUE, ceiling);
        uint256 amount = bound(amountSeed, 1, ceiling);

        _commit(amount, uint64(block.timestamp + 14 days));

        assertEq(usdc.balanceOf(address(escrow)), amount);
    }

    /// A release pays the contributor the whole commitment and empties the
    /// escrow: the platform never keeps a balance, so a settled issue cannot
    /// fund the next one.
    function testFuzz_release_pays_the_commitment_and_empties_the_escrow(
        uint96 amountSeed
    ) public {
        uint256 amount = _amount(amountSeed);
        _commit(amount, uint64(block.timestamp + 14 days));

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, amount);

        assertEq(usdc.balanceOf(CONTRIBUTOR), amount);
        assertEq(usdc.balanceOf(address(escrow)), 0);
        assertEq(uint256(escrow.statusOf(ISSUE)), uint256(MisthosEscrow.Status.Released));
    }

    /// A release can pay nothing except the commitment: the attestor records an
    /// acceptance, it does not choose an amount.
    function testFuzz_a_release_of_any_other_amount_reverts(
        uint96 amountSeed,
        uint256 attemptSeed
    ) public {
        uint256 amount = _amount(amountSeed);
        _commit(amount, uint64(block.timestamp + 14 days));

        uint256 attempt = bound(attemptSeed, 0, type(uint96).max);
        vm.assume(attempt != amount);

        vm.expectRevert(MisthosEscrow.AmountMismatch.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, attempt);
    }

    /// A refund returns exactly the commitment to the publisher: an expired
    /// commitment is the publisher's money back, not a fee.
    function testFuzz_a_refund_returns_the_commitment_to_the_publisher(
        uint96 amountSeed
    ) public {
        uint256 amount = _amount(amountSeed);
        uint64 deadline = _deadline(7);
        _commit(amount, deadline);
        uint256 publisherBefore = usdc.balanceOf(PUBLISHER);

        vm.warp(deadline);
        escrow.refund(ISSUE);

        assertEq(usdc.balanceOf(PUBLISHER) - publisherBefore, amount);
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    // ----------------------------------------------------------- deadlines

    /// Past the deadline a release cannot land, whatever the amount: once the
    /// window closes the publisher's reclaim always wins, which is what makes
    /// the deadline a release condition rather than an advisory date.
    function testFuzz_a_release_after_the_deadline_reverts(
        uint96 amountSeed,
        uint256 driftSeed
    ) public {
        uint256 amount = _amount(amountSeed);
        uint64 deadline = _deadline(3);
        _commit(amount, deadline);

        vm.warp(deadline + bound(driftSeed, 1, 365 days));

        vm.expectRevert(MisthosEscrow.DeadlinePassed.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, amount);
    }

    /// Before the deadline a refund cannot happen: the publisher cannot pull the
    /// money out from under a contributor who still has time to deliver.
    function testFuzz_a_refund_before_the_deadline_reverts(
        uint96 amountSeed,
        uint256 keepSeed
    ) public {
        uint256 amount = _amount(amountSeed);
        uint64 deadline = _deadline(5);
        _commit(amount, deadline);

        vm.warp(bound(keepSeed, block.timestamp, deadline - 1));

        vm.expectRevert(MisthosEscrow.DeadlineNotReached.selector);
        escrow.refund(ISSUE);
    }

    // ------------------------------------------------- double settlement

    /// A commitment settles once. The second release is refused, so a second
    /// contributor cannot be paid out of the escrow's other commitments.
    function testFuzz_a_second_release_reverts(uint96 amountSeed) public {
        uint256 amount = _amount(amountSeed);
        _commit(amount, uint64(block.timestamp + 14 days));

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, amount);

        vm.expectRevert(MisthosEscrow.NotHeld.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, amount);
    }

    /// A released commitment cannot be refunded as well: acceptance and the
    /// deadline are mutually exclusive endings, so the publisher cannot be paid
    /// twice for one commitment.
    function testFuzz_a_released_commitment_cannot_be_refunded(uint96 amountSeed) public {
        uint256 amount = _amount(amountSeed);
        uint64 deadline = _deadline(2);
        _commit(amount, deadline);

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, amount);

        vm.warp(deadline + 1);
        vm.expectRevert(MisthosEscrow.NotHeld.selector);
        escrow.refund(ISSUE);
    }

    /// A refunded commitment cannot be released afterwards: the money has left
    /// the escrow, so there is nothing to attest.
    function testFuzz_a_refunded_commitment_cannot_be_released(uint96 amountSeed) public {
        uint256 amount = _amount(amountSeed);
        uint64 deadline = _deadline(2);
        _commit(amount, deadline);

        vm.warp(deadline);
        escrow.refund(ISSUE);

        vm.expectRevert(MisthosEscrow.NotHeld.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, amount);
    }

    // ------------------------------------------------- attestor rotation

    /// Only the attestor can release, whoever else asks. The authority is the
    /// key, not a role a caller can claim, so no contributor can pay themselves.
    function testFuzz_only_the_attestor_can_release(uint96 amountSeed, address caller) public {
        uint256 amount = _amount(amountSeed);
        _commit(amount, uint64(block.timestamp + 14 days));

        vm.assume(caller != ATTESTOR);

        vm.expectRevert(MisthosEscrow.NotAttestor.selector);
        vm.prank(caller);
        escrow.release(ISSUE, CONTRIBUTOR, amount);
    }

    /// Rotation moves the authority and leaves the money reachable: the retired
    /// key cannot release, the new one can, and the contributor is paid in full.
    /// A rotation that bricked the authority would strand every held commitment.
    function testFuzz_rotating_the_attestor_keeps_the_money_reachable(
        uint96 amountSeed,
        address next
    ) public {
        uint256 amount = _amount(amountSeed);
        vm.assume(next != address(0) && next != ATTESTOR);

        _commit(amount, uint64(block.timestamp + 14 days));
        escrow.setAttestor(next);

        vm.expectRevert(MisthosEscrow.NotAttestor.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, amount);

        vm.prank(next);
        escrow.release(ISSUE, CONTRIBUTOR, amount);

        assertEq(usdc.balanceOf(CONTRIBUTOR), amount);
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    // ----------------------------------------------------------- internals

    /// A commitment between one base unit and the largest amount the struct can
    /// carry. Zero and above `uint96` are refused by the contract itself.
    function _amount(uint256 seed) private pure returns (uint256) {
        return bound(seed, 1, type(uint96).max);
    }

    function _deadline(uint256 seed) private view returns (uint64) {
        return uint64(bound(seed, block.timestamp + 1, block.timestamp + 365 days));
    }

    function _commit(uint256 amount, uint64 deadline) private {
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, amount, deadline);
    }
}
