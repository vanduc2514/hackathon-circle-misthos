// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MisthosEscrow} from "../../src/MisthosEscrow.sol";
import {MockErc20} from "../support/MockErc20.sol";
import {MockUsdcToken} from "../support/MockUsdcToken.sol";
import {EscrowHandler} from "./EscrowHandler.sol";

/**
 * @title MisthosEscrowInvariant
 * @notice The properties that must hold after *any* sequence of the money
 *         paths. The example-based suite next door proves one input each; this
 *         one fuzzes the order and the amounts, because a bug in this contract
 *         is unrecoverable and the platform has no other way to move the money.
 *
 * @dev One campaign per property, driven by EscrowHandler. Each property is
 *      named for what it protects rather than for the function it calls.
 */
contract MisthosEscrowInvariant is Test {
    EscrowHandler internal handler;
    MisthosEscrow internal escrow;
    MockUsdcToken internal usdc;

    function setUp() public {
        handler = new EscrowHandler();
        escrow = handler.escrow();
        usdc = handler.usdc();

        targetContract(address(handler));
    }

    /// The escrow holds exactly the commitments that are still held, and nothing
    /// else: no settled issue leaves a residue behind, and a commitment is never paid
    /// out of another issue's money. Per token, because an issue is held in one of
    /// them and adding the two balances would count money that is not there.
    function invariant_the_escrow_holds_exactly_the_unsettled_commitments() public view {
        for (uint256 t = 0; t < handler.tokenCount(); t++) {
            address token = handler.tokenAt(t);
            uint256 expected;
            for (uint256 i = 0; i < handler.ISSUE_COUNT(); i++) {
                bytes32 issueId = handler.issues(i);
                (uint96 amount, MisthosEscrow.Status status) = _amountAndStatus(issueId);
                if (status == MisthosEscrow.Status.Held && escrow.tokenOf(issueId) == token) {
                    expected += amount;
                }
            }

            assertEq(
                MockErc20(token).balanceOf(address(escrow)),
                expected,
                "the escrow balance is not the sum of the held commitments"
            );
        }
    }

    /// Money is conserved: every base unit is in the escrow, with a publisher, with a
    /// contributor or with the platform's take-rate wallet. Nothing is minted to the
    /// contract, skimmed on the way through, or stranded where no one can claim it.
    /// The fee recipient is a party here for exactly that reason: it is where a take
    /// rate goes, and leaving it out would make the fee look like money that vanished.
    function invariant_no_token_is_created_or_lost() public view {
        for (uint256 t = 0; t < handler.tokenCount(); t++) {
            MockErc20 token = MockErc20(handler.tokenAt(t));
            uint256 accounted = token.balanceOf(address(escrow));

            // Every treasury, not only the one new fees go to: rotating the recipient
            // leaves earlier fees where they were paid.
            for (uint256 i = 0; i < handler.TREASURY_COUNT(); i++) {
                accounted += token.balanceOf(handler.treasuryAt(i));
            }

            for (uint256 i = 0; i < handler.PUBLISHER_COUNT(); i++) {
                accounted += token.balanceOf(handler.publisherAt(i));
            }
            for (uint256 i = 0; i < handler.CONTRIBUTOR_COUNT(); i++) {
                accounted += token.balanceOf(handler.contributorAt(i));
            }

            assertEq(
                accounted,
                handler.GRANT() * handler.PUBLISHER_COUNT(),
                "a token was created or lost"
            );
        }
    }

    /// A release pays what the escrow was told to pay: the contributor receives the
    /// commitment less the rate the escrow holds for that issue, and the rate goes to
    /// the recipient. The balances are held against the split the handler observed, so
    /// a contract that took a different cut, or paid the wrong wallet, fails here.
    function invariant_a_release_splits_the_commitment_at_the_rate() public view {
        for (uint256 t = 0; t < handler.tokenCount(); t++) {
            address token = handler.tokenAt(t);
            uint256 toContributors;
            for (uint256 i = 0; i < handler.CONTRIBUTOR_COUNT(); i++) {
                toContributors += MockErc20(token).balanceOf(handler.contributorAt(i));
            }

            assertEq(
                toContributors,
                handler.contributorTotal(token),
                "contributors were paid other than the commitment less the rate"
            );
            uint256 toTreasuries;
            for (uint256 i = 0; i < handler.TREASURY_COUNT(); i++) {
                toTreasuries += MockErc20(token).balanceOf(handler.treasuryAt(i));
            }
            assertEq(
                toTreasuries,
                handler.feeTotal(token),
                "the take rate paid is not the rate the escrow holds"
            );
        }
    }

    /// A commitment settles at most once. The handler attempts every settlement
    /// it observes, so a second release or a refund after a release would show up
    /// as two settlements, two timestamps, or a status that disagrees with what
    /// the handler watched happen.
    function invariant_a_commitment_settles_at_most_once() public view {
        for (uint256 i = 0; i < handler.ISSUE_COUNT(); i++) {
            bytes32 issueId = handler.issues(i);
            (uint96 amount, MisthosEscrow.Status status) = _amountAndStatus(issueId);

            assertLe(handler.settlements(issueId), 1, "an issue settled twice");
            assertTrue(
                handler.releasedAt(issueId) == 0 || handler.refundedAt(issueId) == 0,
                "a commitment was both released and refunded"
            );

            if (handler.releasedAt(issueId) != 0) {
                assertEq(uint256(status), uint256(MisthosEscrow.Status.Released));
            }
            if (handler.refundedAt(issueId) != 0) {
                assertEq(uint256(status), uint256(MisthosEscrow.Status.Refunded));
            }
            if (status == MisthosEscrow.Status.Held) {
                assertEq(handler.settlements(issueId), 0, "a held commitment was settled");
                assertEq(uint256(amount), handler.committedAmount(issueId));
            }
        }
    }

    /// The deadlines are release conditions, not advice: a release never lands
    /// after the deadline, and a refund never happens before it, so a
    /// contributor's window is never cut short and a publisher always gets the
    /// money back once the window closes.
    function invariant_settlement_respects_the_deadline() public view {
        for (uint256 i = 0; i < handler.ISSUE_COUNT(); i++) {
            bytes32 issueId = handler.issues(i);
            uint64 deadline = handler.committedDeadline(issueId);

            uint256 released = handler.releasedAt(issueId);
            if (released != 0) {
                assertLe(released, deadline, "released after the deadline");
            }

            uint256 refunded = handler.refundedAt(issueId);
            if (refunded != 0) {
                assertGe(refunded, deadline, "refunded before the deadline");
            }
        }
    }

    /// No commitment is ever above the ceiling that was in force when it was
    /// made. The ceiling is the one guardrail that survives a compromised agent
    /// key, so it has to hold for every amount the campaign tries, not just the
    /// amount the caller meant to send.
    function invariant_a_commitment_fits_its_ceiling() public view {
        for (uint256 i = 0; i < handler.ISSUE_COUNT(); i++) {
            bytes32 issueId = handler.issues(i);
            uint256 committed = handler.committedAmount(issueId);
            uint256 ceiling = handler.committedCeiling(issueId);

            if (committed == 0) continue;
            // Every commitment had an approved price behind it: an unset ceiling
            // refuses the commitment rather than leaving it uncapped.
            assertGt(ceiling, 0, "committed without a ceiling");
            assertLe(committed, ceiling, "committed above the ceiling in force");
        }
    }

    /// The attestor always has exactly one live key: it is never the zero
    /// address, it is always the last key the owner set, and the owner never
    /// changes. The handler challenges each retired key on every rotation, so a
    /// rotation that left a second key able to move money would fail the run
    /// before this property is even checked.
    function invariant_the_attestor_keeps_its_authority() public view {
        assertGt(handler.attestorCount(), 0, "no attestor was ever set");

        address current = escrow.attestor();
        assertTrue(current != address(0), "the attestor was rotated to zero");
        assertEq(current, handler.currentAttestor(), "the attestor is not the last key set");
        assertEq(escrow.owner(), handler.owner(), "the owner changed");
    }

    /// What went in is what is still held plus what came out, per token: a settlement
    /// moves exactly the commitment, so no release can pay less than the publisher
    /// committed (a skim) or more than the escrow holds (a hole). The contributor's
    /// share and the fee are both inside the amount that left, which is why this sums
    /// the gross rather than the split.
    function invariant_the_money_that_left_matches_the_money_that_came_in() public view {
        for (uint256 t = 0; t < handler.tokenCount(); t++) {
            address token = handler.tokenAt(t);
            uint256 committed;
            for (uint256 i = 0; i < handler.ISSUE_COUNT(); i++) {
                bytes32 issueId = handler.issues(i);
                if (handler.committedToken(issueId) == token) {
                    committed += handler.committedAmount(issueId);
                }
            }

            assertEq(
                MockErc20(token).balanceOf(address(escrow)) + handler.releasedByToken(token)
                    + handler.refundedByToken(token),
                committed,
                "the money that left the escrow is not the money that entered it"
            );
        }
    }

    // ----------------------------------------------------------- internals

    function _amountAndStatus(bytes32 issueId)
        private
        view
        returns (uint96 amount, MisthosEscrow.Status status)
    {
        (, amount,, status) = escrow.commitments(issueId);
    }
}
