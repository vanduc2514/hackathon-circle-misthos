// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MisthosEscrow} from "../../src/MisthosEscrow.sol";
import {MockErc20} from "../support/MockErc20.sol";
import {MockUsdcToken} from "../support/MockUsdcToken.sol";

/**
 * @title EscrowHandler
 * @notice Drives the escrow's money paths in a random order so that the
 *         properties in MisthosEscrowInvariant.t.sol are checked from states a
 *         person would not have thought to write down.
 *
 * @dev Each driver computes its own preconditions and returns untouched when a
 *      step is impossible, because the campaign runs with `fail_on_revert` on:
 *      a revert inside the handler is a failure, not a rejected input. Paths
 *      that are *supposed* to revert say so with `vm.expectRevert`, so the
 *      refusal is proven rather than skipped.
 *
 *      The handler deploys the token and the escrow, so it holds the owner key
 *      and can set ceilings and rotate the attestor without a prank.
 */
contract EscrowHandler is Test {
    MisthosEscrow public immutable escrow;
    MockUsdcToken public immutable usdc;
    MockErc20 public immutable eurc;

    /// @dev Every token an issue can be denominated in. Both are 6-decimal ERC-20s,
    ///      which is the only kind of token the escrow will hold.
    address[] public tokens;

    /// @dev The handler is the escrow's owner: the only key that can set a
    ///      ceiling or rotate the attestor.
    address public immutable owner;

    uint256 public constant ISSUE_COUNT = 6;
    uint256 public constant PUBLISHER_COUNT = 3;
    uint256 public constant CONTRIBUTOR_COUNT = 3;

    /// @dev What each publisher is funded with. Large enough for several
    ///      commitments, small enough that a fuzzed amount is bounded by it.
    uint256 public constant GRANT = 1_000_000_000_000; // 1,000,000 USDC

    uint64 internal constant MIN_LEAD = 1 hours;
    uint64 internal constant MAX_LEAD = 90 days;
    uint64 internal constant MAX_STEP = 30 days;

    bytes32[] public issues;
    address[] public publishers;
    address[] public contributors;

    /// @dev Every attestor set, oldest first, with the first one at index 0.
    address[] public attestors;

    /// @dev What the handler observed, per issue. These are what let the
    ///      invariants talk about settlement counts and deadlines, which
    ///      balances alone cannot express.
    mapping(bytes32 => uint256) public committedAmount;
    mapping(bytes32 => uint256) public committedCeiling;
    mapping(bytes32 => uint64) public committedDeadline;
    mapping(bytes32 => uint256) public settlements;
    mapping(bytes32 => uint256) public releasedAt;
    mapping(bytes32 => uint256) public refundedAt;
    mapping(bytes32 => address) public releasedTo;

    /// @dev Totals the handler moved, so the invariants can reconcile the
    ///      escrow against the parties rather than only against itself.
    uint256 public releasedTotal;
    uint256 public refundedTotal;

    /// @dev The take rate's side of a settlement, per token, so the invariants can
    ///      hold the split against the balances rather than only the gross.
    mapping(address => uint256) public feeTotal;
    mapping(address => uint256) public contributorTotal;
    mapping(address => uint256) public releasedByToken;
    mapping(address => uint256) public refundedByToken;

    /// @dev Which token an issue is held in, as the escrow was told.
    mapping(bytes32 => address) public committedToken;

    /// @dev Where the take rate goes. A fixed pool of treasury addresses, so the
    ///      invariants can account for the platform as its own party: a treasury that
    ///      was also a publisher would be counted twice and money would look created.
    uint256 public constant TREASURY_COUNT = 4;
    address[] public treasuries;

    /// @dev Where new fees go now. Rotating it moves later fees only, so every
    ///      treasury in the pool has to be accounted for, not just this one.
    address public feeRecipient;

    constructor() {
        usdc = new MockUsdcToken();
        eurc = new MockErc20("Euro Coin", "EURC", 6);
        owner = address(this);

        tokens.push(address(usdc));
        tokens.push(address(eurc));

        address firstAttestor = address(0xA7705);
        escrow = new MisthosEscrow(firstAttestor, address(usdc));
        attestors.push(firstAttestor);

        for (uint256 i = 0; i < ISSUE_COUNT; i++) {
            issues.push(keccak256(abi.encodePacked("ISS-", i)));
        }

        for (uint256 i = 0; i < PUBLISHER_COUNT; i++) {
            address publisher = address(uint160(0xB0B0 + i));
            publishers.push(publisher);
            usdc.mint(publisher, GRANT);
            eurc.mint(publisher, GRANT);
            vm.prank(publisher);
            usdc.approve(address(escrow), type(uint256).max);
            vm.prank(publisher);
            eurc.approve(address(escrow), type(uint256).max);
        }

        for (uint256 i = 0; i < CONTRIBUTOR_COUNT; i++) {
            contributors.push(address(uint160(0xC0FE + i)));
        }

        // The treasury the take rate is paid to has to exist before any rate is set:
        // a rate with nowhere to send the money is refused at release, and the campaign
        // runs with fail_on_revert on. That refusal is proven in the example suite.
        for (uint256 i = 0; i < TREASURY_COUNT; i++) {
            treasuries.push(address(uint160(0xFEE0 + i)));
        }
        feeRecipient = treasuries[0];
        escrow.setFeeRecipient(feeRecipient);
    }

    function tokenCount() external view returns (uint256) {
        return tokens.length;
    }

    function tokenAt(uint256 index) external view returns (address) {
        return tokens[index];
    }

    function publisherAt(uint256 index) external view returns (address) {
        return publishers[index];
    }

    function contributorAt(uint256 index) external view returns (address) {
        return contributors[index];
    }

    function treasuryAt(uint256 index) external view returns (address) {
        return treasuries[index];
    }

    // ------------------------------------------------------------ drivers

    /**
     * @notice A publisher commits a price for one issue.
     * @dev A price above a ceiling that is in force must be refused. The
     *      campaign proves the refusal here rather than avoiding the input, so
     *      a contract that quietly accepted it fails the run.
     */
    function commit(
        uint256 issueSeed,
        uint256 publisherSeed,
        uint256 amountSeed,
        uint256 deadlineSeed
    ) external {
        bytes32 issueId = issues[issueSeed % ISSUE_COUNT];
        if (escrow.statusOf(issueId) != MisthosEscrow.Status.None) return;

        address publisher = publishers[publisherSeed % PUBLISHER_COUNT];
        // The balance that matters is the one in the issue's own token: bounding by USDC
        // and then pulling EURC would underflow inside the token, not revert politely.
        address funding = escrow.tokenOf(issueId);
        uint256 balance = MockErc20(funding).balanceOf(publisher);
        if (balance == 0) return;

        uint256 ceilingInForce = escrow.ceiling(issueId);
        uint256 amount = bound(amountSeed, 1, _min(type(uint96).max, balance));
        uint64 deadline =
            uint64(bound(deadlineSeed, block.timestamp + MIN_LEAD, block.timestamp + MAX_LEAD));

        if (ceilingInForce != 0 && amount > ceilingInForce) {
            vm.expectRevert(
                abi.encodeWithSelector(
                    MisthosEscrow.ExceedsCeiling.selector, amount, ceilingInForce
                )
            );
            vm.prank(publisher);
            escrow.commit(issueId, amount, deadline);
            return;
        }

        vm.prank(publisher);
        escrow.commit(issueId, amount, deadline);

        committedToken[issueId] = escrow.tokenOf(issueId);
        committedAmount[issueId] = amount;
        committedCeiling[issueId] = ceilingInForce;
        committedDeadline[issueId] = deadline;
    }

    /**
     * @notice The owner caps what an issue may commit, or lifts the cap with
     *         zero. The ceiling is the guardrail an agent cannot talk its way
     *         past, so the campaign has to move it around.
     */
    function setCeiling(uint256 issueSeed, uint256 ceilingSeed) external {
        bytes32 issueId = issues[issueSeed % ISSUE_COUNT];
        escrow.setCeiling(issueId, bound(ceilingSeed, 0, GRANT));
    }

    /// @notice The attestor accepts the work. A commitment that has already
    ///         settled, or whose deadline has passed, must be refused — the
    ///         campaign asks for those refusals rather than skipping them, so a
    ///         contract that allowed either one fails the run.
    function release(uint256 issueSeed, uint256 contributorSeed) external {
        bytes32 issueId = issues[issueSeed % ISSUE_COUNT];
        (uint256 amount, uint64 deadline, MisthosEscrow.Status status) = _commitment(issueId);

        address contributor = contributors[contributorSeed % CONTRIBUTOR_COUNT];
        address attestor = escrow.attestor();

        if (status != MisthosEscrow.Status.Held) {
            vm.expectRevert(MisthosEscrow.NotHeld.selector);
            vm.prank(attestor);
            escrow.release(issueId, contributor, amount);
            return;
        }

        if (block.timestamp > deadline) {
            vm.expectRevert(MisthosEscrow.DeadlinePassed.selector);
            vm.prank(attestor);
            escrow.release(issueId, contributor, amount);
            return;
        }

        vm.prank(attestor);
        escrow.release(issueId, contributor, amount);

        // The rate is the escrow's own, and since it can no longer move once the money
        // is in, the split the contract performed is arithmetic the handler can repeat.
        address token = escrow.tokenOf(issueId);
        uint256 fee = (amount * escrow.feeBps(issueId)) / 10_000;
        settlements[issueId] += 1;
        releasedAt[issueId] = block.timestamp;
        releasedTo[issueId] = contributor;
        releasedTotal += amount;
        releasedByToken[token] += amount;
        feeTotal[token] += fee;
        contributorTotal[token] += amount - fee;
    }

    /// @notice Anyone reclaims an expired commitment, and the money only ever
    ///         goes back to the publisher. A refund before the deadline, or of a
    ///         commitment that already settled, must be refused.
    function refund(uint256 issueSeed) external {
        bytes32 issueId = issues[issueSeed % ISSUE_COUNT];
        (uint256 amount, uint64 deadline, MisthosEscrow.Status status) = _commitment(issueId);

        if (status != MisthosEscrow.Status.Held) {
            vm.expectRevert(MisthosEscrow.NotHeld.selector);
            escrow.refund(issueId);
            return;
        }

        if (block.timestamp < deadline) {
            vm.expectRevert(MisthosEscrow.DeadlineNotReached.selector);
            escrow.refund(issueId);
            return;
        }

        escrow.refund(issueId);

        settlements[issueId] += 1;
        refundedAt[issueId] = block.timestamp;
        refundedTotal += amount;
        refundedByToken[escrow.tokenOf(issueId)] += amount;
    }

    /// @notice The owner rotates the attestor key, because rotation is the
    ///         operation most likely to strand money.
    /// @dev The retired key is challenged on the next rotation: a rotation that
    ///      left the old key working would be a second way to move money, so the
    ///      campaign proves it is refused while a commitment is still held.
    function rotateAttestor(uint256 seed) external {
        address previous = escrow.attestor();
        address next = address(uint160(bound(seed, 0x1000, 0xFFFFFF)));
        if (next == previous) return;

        escrow.setAttestor(next);
        attestors.push(next);

        bytes32 heldIssue = _firstHeldIssue();
        if (heldIssue == bytes32(0)) return;

        (uint256 amount,,) = _commitment(heldIssue);
        vm.expectRevert(MisthosEscrow.NotAttestor.selector);
        vm.prank(previous);
        escrow.release(heldIssue, contributors[0], amount);
    }

    /**
     * @notice The owner names the token an issue is denominated in, and is refused
     *         once the money is in: a swap after a commitment would move it into a
     *         different balance.
     */
    function setIssueToken(uint256 issueSeed, uint256 tokenSeed) external {
        bytes32 issueId = issues[issueSeed % ISSUE_COUNT];
        address token = tokens[tokenSeed % tokens.length];

        if (escrow.statusOf(issueId) != MisthosEscrow.Status.None) {
            vm.expectRevert(MisthosEscrow.CommitmentStarted.selector);
            escrow.setIssueToken(issueId, token);
            return;
        }
        escrow.setIssueToken(issueId, token);
    }

    /**
     * @notice The owner sets the rate an issue settles at. Bounded here, and refused
     *         once the money is in, because the publisher approved the price at this
     *         rate. Both refusals are asked for rather than skipped.
     */
    function setFee(uint256 issueSeed, uint256 bpsSeed) external {
        bytes32 issueId = issues[issueSeed % ISSUE_COUNT];
        uint256 bps = bound(bpsSeed, 0, type(uint32).max);
        uint256 max = escrow.MAX_FEE_BPS();

        if (escrow.statusOf(issueId) != MisthosEscrow.Status.None) {
            // A legal rate, so the refusal can only come from the commitment. Read the
            // ceiling into a local first: a call in the argument list is the next call,
            // and it would consume the expectation below.
            uint256 legal = bps % (max + 1);
            vm.expectRevert(MisthosEscrow.CommitmentStarted.selector);
            escrow.setFee(issueId, legal);
            return;
        }

        if (bps > max) {
            vm.expectRevert(abi.encodeWithSelector(MisthosEscrow.FeeTooHigh.selector, bps, max));
            escrow.setFee(issueId, bps);
            return;
        }
        escrow.setFee(issueId, bps);
    }

    /**
     * @notice The owner rotates where the take rate goes. Allowed while money is held:
     *         the contract's own NatSpec says rotating moves only later fees.
     */
    function setFeeRecipient(uint256 seed) external {
        address next = treasuries[seed % TREASURY_COUNT];
        if (next == feeRecipient) return;

        escrow.setFeeRecipient(next);
        feeRecipient = next;
    }

    /// @notice Time passes, which is the only way a deadline is reached.
    function advanceTime(uint256 seed) external {
        vm.warp(block.timestamp + bound(seed, 1 hours, MAX_STEP));
    }

    // -------------------------------------------------------------- views

    function attestorCount() external view returns (uint256) {
        return attestors.length;
    }

    /// @notice The attestor the owner set last, which is the one with authority.
    function currentAttestor() external view returns (address) {
        return attestors[attestors.length - 1];
    }

    // ----------------------------------------------------------- internals

    function _commitment(bytes32 issueId)
        private
        view
        returns (uint256 amount, uint64 deadline, MisthosEscrow.Status status)
    {
        (, uint96 committed, uint64 d, MisthosEscrow.Status s) = escrow.commitments(issueId);
        return (uint256(committed), d, s);
    }

    function _min(uint256 a, uint256 b) private pure returns (uint256) {
        return a < b ? a : b;
    }

    /// @dev The first issue in a held state, or zero when every commitment the
    ///      campaign made has already settled.
    function _firstHeldIssue() private view returns (bytes32) {
        for (uint256 i = 0; i < issues.length; i++) {
            (, , MisthosEscrow.Status status) = _commitment(issues[i]);
            if (status == MisthosEscrow.Status.Held) return issues[i];
        }
        return bytes32(0);
    }
}
