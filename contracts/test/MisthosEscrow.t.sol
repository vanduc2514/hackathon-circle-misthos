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

/// @dev A stand-in for any other ERC-20: EURC, which uses the same 6 decimals as
/// USDC, and an 18-decimal token, which is what the escrow must never hold.
contract MockToken {
    string public symbol;
    uint8 public decimals;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory symbol_, uint8 decimals_) {
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

contract MisthosEscrowTest is Test {
    MisthosEscrow escrow;
    MockUSDC usdc;
    MockToken eurc;
    MockToken nativeView;

    address constant ATTESTOR = address(0xA77E5);
    address constant PUBLISHER = address(0xB0B);
    address constant CONTRIBUTOR = address(0xC0FE);
    address constant FEE_RECIPIENT = address(0xFEE);

    bytes32 constant ISSUE = keccak256("ISS-1001");

    uint256 constant FIX = 180_000_000; // 180.00 USDC at 6 decimals
    uint256 constant EUR_FIX = 170_000_000; // 170.00 EURC at 6 decimals

    function setUp() public {
        usdc = new MockUSDC();
        eurc = new MockToken("EURC", 6);
        nativeView = new MockToken("WAD", 18);
        escrow = new MisthosEscrow(ATTESTOR, address(usdc));

        usdc.mint(PUBLISHER, 1_000_000_000);
        vm.prank(PUBLISHER);
        usdc.approve(address(escrow), type(uint256).max);

        eurc.mint(PUBLISHER, 1_000_000_000);
        vm.prank(PUBLISHER);
        eurc.approve(address(escrow), type(uint256).max);
    }

    address constant STRANGER = address(0xBAD);

    /// @dev The approval checkpoint as the owner records it: the price, the publisher
    ///      who may commit it, and the latest deadline they may commit it to.
    function _approve(uint256 cap) internal {
        escrow.setCeiling(ISSUE, cap, PUBLISHER, _latest());
    }

    function _latest() internal view returns (uint64) {
        return uint64(block.timestamp + 14 days);
    }

    function _commit() internal returns (uint64 deadline) {
        _approve(FIX);
        deadline = uint64(block.timestamp + 14 days);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, deadline);
    }

    // ----------------------------------------------------------------- commit

    function test_commit_holds_funds_and_records_the_commitment() public {
        uint64 deadline = _commit();

        assertTrue(escrow.isHeld(ISSUE));
        assertEq(usdc.balanceOf(address(escrow)), FIX);

        (address pub, uint96 amount, uint64 d, MisthosEscrow.Status status) =
            escrow.commitments(ISSUE);
        assertEq(pub, PUBLISHER);
        assertEq(amount, FIX);
        assertEq(d, deadline);
        assertEq(uint256(status), uint256(MisthosEscrow.Status.Held));
    }

    function test_commit_twice_for_the_same_issue_is_rejected() public {
        _commit();
        vm.expectRevert(MisthosEscrow.AlreadyExists.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, uint64(block.timestamp + 1 days));
    }

    function test_commit_with_a_zero_amount_is_rejected() public {
        _approve(FIX);
        vm.expectRevert(MisthosEscrow.ZeroAmount.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, 0, uint64(block.timestamp + 1 days));
    }

    function test_commit_with_a_past_deadline_is_rejected() public {
        _approve(FIX);
        vm.expectRevert(MisthosEscrow.DeadlinePassed.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, uint64(block.timestamp - 1));
    }

    function test_ceiling_caps_what_an_agent_can_commit() public {
        _approve(100_000_000);
        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.ExceedsCeiling.selector, FIX, 100_000_000)
        );
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, uint64(block.timestamp + 1 days));
    }

    // An issue nobody approved a price for cannot take money at all. Without
    // this, a ceiling only binds if someone remembered to set it first.
    function test_an_issue_without_an_approved_ceiling_cannot_be_funded() public {
        vm.expectRevert(MisthosEscrow.NoCeiling.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, uint64(block.timestamp + 1 days));
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    // The approved price itself must be committable; the ceiling is inclusive.
    function test_committing_exactly_the_approved_price_succeeds() public {
        _approve(FIX);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, uint64(block.timestamp + 1 days));
        assertTrue(escrow.isHeld(ISSUE));
    }

    // Withdrawing an approval puts the issue back to unfundable, not uncapped.
    function test_clearing_the_ceiling_makes_the_issue_unfundable_again() public {
        _approve(FIX);
        _approve(0);
        vm.expectRevert(MisthosEscrow.NoCeiling.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, uint64(block.timestamp + 1 days));
    }

    // The agent holds the publisher's key, so the publisher must not be able
    // to raise the cap a human approved.
    function test_the_publisher_cannot_raise_its_own_ceiling() public {
        vm.expectRevert(MisthosEscrow.NotOwner.selector);
        vm.prank(PUBLISHER);
        _approve(type(uint256).max);
    }

    // ------------------------------------------------- who may commit, until when

    // The issue id is the keccak of a public platform id. Before the approval named
    // its publisher, a stranger could commit one base unit first with a deadline in
    // 2100, and the issue could then never be funded, booked or refunded (#122).
    function test_a_stranger_cannot_squat_an_issue_with_dust() public {
        _approve(FIX);
        usdc.mint(STRANGER, 1);
        vm.startPrank(STRANGER);
        usdc.approve(address(escrow), 1);
        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.NotApprovedPublisher.selector, STRANGER)
        );
        escrow.commit(ISSUE, 1, 4_102_444_800);
        vm.stopPrank();

        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, _latest());
        (address publisher, uint96 amount,,) = escrow.commitments(ISSUE);
        assertEq(publisher, PUBLISHER);
        assertEq(amount, FIX);
    }

    // A deadline the sender chooses would push the refund wherever they like, past
    // the platform's own deadline sweep. The approval bounds it.
    function test_a_deadline_past_the_approved_one_is_refused() public {
        _approve(FIX);
        uint64 latest = _latest();
        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.DeadlineTooLate.selector, latest + 1, latest)
        );
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, latest + 1);
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    // The approved deadline itself is committable; the bound is inclusive.
    function test_the_approved_deadline_itself_is_committable() public {
        _approve(FIX);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, _latest());
        (,, uint64 deadline,) = escrow.commitments(ISSUE);
        assertEq(deadline, _latest());
    }

    // A ceiling with nobody allowed to commit it would let nobody fund the issue
    // while looking approved; one with no time left could never be committed.
    function test_a_ceiling_names_its_publisher_and_leaves_time_to_commit() public {
        vm.expectRevert(MisthosEscrow.ZeroAddress.selector);
        escrow.setCeiling(ISSUE, FIX, address(0), _latest());

        vm.expectRevert(MisthosEscrow.DeadlinePassed.selector);
        escrow.setCeiling(ISSUE, FIX, PUBLISHER, uint64(block.timestamp));
    }

    // Clearing the approval forgets who could commit and until when, so a later
    // approval for someone else starts clean.
    function test_clearing_the_ceiling_forgets_the_publisher_and_the_deadline() public {
        _approve(FIX);
        escrow.setCeiling(ISSUE, 0, PUBLISHER, _latest());
        assertEq(escrow.ceiling(ISSUE), 0);
        assertEq(escrow.approvedPublisher(ISSUE), address(0));
        assertEq(escrow.latestDeadline(ISSUE), 0);
    }

    // The approval is readable on chain and announced, so a publisher can check who
    // the platform named before sending money.
    function test_an_approval_reports_who_may_commit_and_until_when() public {
        uint64 latest = _latest();
        vm.expectEmit(true, true, false, true, address(escrow));
        emit MisthosEscrow.CeilingUpdated(ISSUE, FIX, PUBLISHER, latest);
        _approve(FIX);
        assertEq(escrow.approvedPublisher(ISSUE), PUBLISHER);
        assertEq(escrow.latestDeadline(ISSUE), latest);
    }

    // Naming the publisher is the owner's decision, like the ceiling it comes with.
    function test_only_the_owner_can_name_who_may_commit() public {
        vm.expectRevert(MisthosEscrow.NotOwner.selector);
        vm.prank(STRANGER);
        escrow.setCeiling(ISSUE, FIX, STRANGER, _latest());
    }

    // For any approved price and any attempted amount, the contract takes the
    // money exactly when the amount is within the approval.
    function testFuzz_a_commitment_is_taken_only_within_the_ceiling(
        uint96 cap,
        uint96 amount
    ) public {
        cap = uint96(bound(cap, 1, 1_000_000_000));
        amount = uint96(bound(amount, 1, 1_000_000_000));
        _approve(cap);

        if (amount > cap) {
            vm.expectRevert(
                abi.encodeWithSelector(MisthosEscrow.ExceedsCeiling.selector, amount, cap)
            );
        }
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, amount, uint64(block.timestamp + 1 days));

        assertEq(usdc.balanceOf(address(escrow)), amount > cap ? 0 : amount);
    }

    // ---------------------------------------------------------------- release

    /// With no take rate configured the commitment is the contributor's whole
    /// payment, which is the only configuration where one transfer settles it.
    function test_release_without_a_take_rate_pays_the_whole_commitment() public {
        _commit();

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);

        assertEq(usdc.balanceOf(CONTRIBUTOR), FIX);
        assertEq(usdc.balanceOf(address(escrow)), 0);
        assertEq(uint256(escrow.statusOf(ISSUE)), uint256(MisthosEscrow.Status.Released));
    }

    function test_only_the_attestor_can_release() public {
        _commit();
        vm.expectRevert(MisthosEscrow.NotAttestor.selector);
        vm.prank(PUBLISHER);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);
    }

    function test_the_contributor_cannot_release_their_own_payment() public {
        _commit();
        vm.expectRevert(MisthosEscrow.NotAttestor.selector);
        vm.prank(CONTRIBUTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);
    }

    function test_release_after_the_deadline_is_refused() public {
        uint64 deadline = _commit();
        vm.warp(deadline + 1);
        vm.expectRevert(MisthosEscrow.DeadlinePassed.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);
    }

    function test_release_cannot_pay_out_more_than_was_committed() public {
        _commit();
        vm.expectRevert(MisthosEscrow.AmountMismatch.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX * 2);
    }

    function test_release_to_the_zero_address_is_rejected() public {
        _commit();
        vm.expectRevert(MisthosEscrow.ZeroAddress.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, address(0), FIX);
    }

    function test_release_twice_is_rejected() public {
        _commit();
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);

        vm.expectRevert(MisthosEscrow.NotHeld.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);
    }

    // -------------------------------------------------------------- take rate

    /// The platform's revenue is the tier's rate on the fix price, carved out of
    /// the commitment rather than added to it, and it lands in the same call.
    function test_a_release_carves_the_tier_take_rate_out_of_the_commitment() public {
        // 1200 basis points is the Open tier's 12 percent.
        escrow.setFeeRecipient(FEE_RECIPIENT);
        escrow.setFee(ISSUE, 1200);
        _commit();

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);

        uint256 fee = (FIX * 1200) / 10_000;
        assertEq(usdc.balanceOf(FEE_RECIPIENT), fee);
        assertEq(usdc.balanceOf(CONTRIBUTOR), FIX - fee);
        assertEq(usdc.balanceOf(FEE_RECIPIENT) + usdc.balanceOf(CONTRIBUTOR), FIX);
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    /// Rounding an odd commitment goes the contributor's way: the fee is floored,
    /// so the platform never takes a base unit the rate did not earn.
    function test_the_take_rate_rounds_down_in_the_contributors_favour() public {
        uint256 odd = 55_555_555; // 55.555555 USDC
        escrow.setFeeRecipient(FEE_RECIPIENT);
        escrow.setFee(ISSUE, 1200);
        _approve(odd);

        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, odd, uint64(block.timestamp + 14 days));
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, odd);

        uint256 fee = (odd * 1200) / 10_000; // 6.6666666 floors to 6.666666
        assertEq(usdc.balanceOf(FEE_RECIPIENT), fee);
        assertEq(usdc.balanceOf(CONTRIBUTOR), odd - fee);
    }

    /// The rate is fixed with the money, like the currency. The publisher approves the
    /// price at this rate, so moving it afterwards would charge a rate they never saw.
    function test_the_take_rate_is_fixed_once_a_commitment_exists() public {
        _commit();

        vm.expectRevert(MisthosEscrow.CommitmentStarted.selector);
        escrow.setFee(ISSUE, 1200);
    }

    /// A zero rate is a legal configuration and leaves the contributor whole.
    function test_a_zero_take_rate_leaves_the_contributor_whole() public {
        escrow.setFeeRecipient(FEE_RECIPIENT);
        escrow.setFee(ISSUE, 0);
        _commit();

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);

        assertEq(usdc.balanceOf(FEE_RECIPIENT), 0);
        assertEq(usdc.balanceOf(CONTRIBUTOR), FIX);
    }

    /// Above 15 percent the tier table says the rate is renegotiated, so the
    /// contract refuses to carry one even for the owner.
    function test_the_take_rate_cannot_exceed_the_published_ceiling() public {
        assertEq(escrow.MAX_FEE_BPS(), 1500);
        vm.expectRevert(abi.encodeWithSelector(MisthosEscrow.FeeTooHigh.selector, 1501, 1500));
        escrow.setFee(ISSUE, 1501);
    }

    /// A rate with nowhere to send the money would strand it in the contract,
    /// where no one has a claim on it.
    function test_a_release_with_a_fee_and_no_recipient_is_refused() public {
        escrow.setFee(ISSUE, 1200);
        _commit();

        vm.expectRevert(MisthosEscrow.FeeRecipientNotSet.selector);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);
    }

    /// The rate and its recipient are the owner's to set, like the ceiling.
    function test_only_the_owner_can_set_the_take_rate_and_its_recipient() public {
        vm.expectRevert(MisthosEscrow.NotOwner.selector);
        vm.prank(PUBLISHER);
        escrow.setFee(ISSUE, 1200);

        vm.expectRevert(MisthosEscrow.NotOwner.selector);
        vm.prank(PUBLISHER);
        escrow.setFeeRecipient(FEE_RECIPIENT);
    }

    /// A refund returns the publisher's money untouched: no fee is earned on work
    /// that was never accepted.
    function test_a_refund_never_pays_a_fee() public {
        escrow.setFeeRecipient(FEE_RECIPIENT);
        escrow.setFee(ISSUE, 1200);
        uint64 deadline = _commit();
        uint256 before = usdc.balanceOf(PUBLISHER);

        vm.warp(deadline + 1);
        escrow.refund(ISSUE);

        assertEq(usdc.balanceOf(PUBLISHER) - before, FIX);
        assertEq(usdc.balanceOf(FEE_RECIPIENT), 0);
    }

    /// Rotating the treasury moves where later fees land, not fees already paid.
    function test_rotating_the_fee_recipient_moves_only_later_fees() public {
        escrow.setFeeRecipient(FEE_RECIPIENT);
        escrow.setFee(ISSUE, 1200);
        _commit();

        address next = address(0xFEE2);
        escrow.setFeeRecipient(next);

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);

        assertEq(usdc.balanceOf(FEE_RECIPIENT), 0);
        assertEq(usdc.balanceOf(next), (FIX * 1200) / 10_000);
    }

    /// `Released` reports what the contributor received, not the gross
    /// commitment, so an indexer cannot read the fee back as a contributor payout.
    function test_released_reports_the_contributor_payout_not_the_gross() public {
        escrow.setFeeRecipient(FEE_RECIPIENT);
        escrow.setFee(ISSUE, 1200);
        _commit();
        uint256 fee = (FIX * 1200) / 10_000;

        vm.expectEmit(true, true, false, true, address(escrow));
        emit MisthosEscrow.Released(ISSUE, CONTRIBUTOR, FIX - fee);
        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);
    }

    // ----------------------------------------------------------------- refund

    function test_refund_returns_funds_after_the_deadline() public {
        uint64 deadline = _commit();
        uint256 before = usdc.balanceOf(PUBLISHER);

        vm.warp(deadline + 1);
        escrow.refund(ISSUE);

        assertEq(usdc.balanceOf(PUBLISHER) - before, FIX);
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
        escrow.release(ISSUE, CONTRIBUTOR, FIX);

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
        escrow.release(ISSUE, CONTRIBUTOR, FIX);

        vm.prank(next);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);
        assertEq(usdc.balanceOf(CONTRIBUTOR), FIX);
    }

    function test_constructor_rejects_zero_addresses() public {
        vm.expectRevert(MisthosEscrow.ZeroAddress.selector);
        new MisthosEscrow(address(0), address(usdc));

        vm.expectRevert(MisthosEscrow.ZeroAddress.selector);
        new MisthosEscrow(ATTESTOR, address(0));
    }

    // ------------------------------------------------------------ currencies

    /// USDC is what an issue is denominated in unless the owner names another
    /// token, so every issue funded before EURC existed still works unchanged.
    function test_usdc_is_the_default_currency_for_an_issue() public {
        assertEq(escrow.tokenOf(ISSUE), address(usdc));

        _commit();

        assertEq(escrow.tokenOf(ISSUE), address(usdc));
        assertEq(usdc.balanceOf(address(escrow)), FIX);
    }

    /// The reason EURC exists here: a European publisher's issue is funded and
    /// paid in EURC, and the USDC balance is untouched at both ends.
    function test_a_european_issue_round_trips_through_eurc() public {
        escrow.setIssueToken(ISSUE, address(eurc));

        uint64 deadline = uint64(block.timestamp + 14 days);
        _approve(EUR_FIX);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, EUR_FIX, deadline);

        assertEq(eurc.balanceOf(address(escrow)), EUR_FIX);
        assertEq(usdc.balanceOf(address(escrow)), 0);

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, EUR_FIX);

        assertEq(eurc.balanceOf(CONTRIBUTOR), EUR_FIX);
        assertEq(eurc.balanceOf(address(escrow)), 0);
        assertEq(usdc.balanceOf(CONTRIBUTOR), 0);
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    /// Both features in one call: an issue held in EURC that also pays the platform's
    /// take rate settles in EURC, split, and never touches the USDC balance. The two
    /// were written as separate changes, so this is the case neither one covered.
    function test_a_european_issue_pays_the_take_rate_in_eurc() public {
        escrow.setIssueToken(ISSUE, address(eurc));
        escrow.setFeeRecipient(FEE_RECIPIENT);
        escrow.setFee(ISSUE, 1200);

        uint64 deadline = uint64(block.timestamp + 14 days);
        _approve(EUR_FIX);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, EUR_FIX, deadline);

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, EUR_FIX);

        uint256 fee = (EUR_FIX * 1200) / 10_000;
        assertEq(eurc.balanceOf(CONTRIBUTOR), EUR_FIX - fee);
        assertEq(eurc.balanceOf(FEE_RECIPIENT), fee);
        assertEq(eurc.balanceOf(address(escrow)), 0);
        assertEq(usdc.balanceOf(CONTRIBUTOR), 0);
        assertEq(usdc.balanceOf(FEE_RECIPIENT), 0);
    }

    /// The other half of the pair. A USDC round trip may not move EURC: they are
    /// two balances, and adding them would double-count money.
    function test_a_usdc_issue_round_trips_without_touching_eurc() public {
        _commit();

        vm.prank(ATTESTOR);
        escrow.release(ISSUE, CONTRIBUTOR, FIX);

        assertEq(usdc.balanceOf(CONTRIBUTOR), FIX);
        assertEq(eurc.balanceOf(address(escrow)), 0);
        assertEq(eurc.balanceOf(CONTRIBUTOR), 0);
    }

    /// A refund is paid in the currency that was committed, not converted into
    /// USDC on the way back.
    function test_a_refund_returns_eurc_to_the_publisher() public {
        escrow.setIssueToken(ISSUE, address(eurc));

        uint64 deadline = uint64(block.timestamp + 14 days);
        _approve(EUR_FIX);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, EUR_FIX, deadline);
        uint256 before = eurc.balanceOf(PUBLISHER);

        vm.warp(deadline + 1);
        escrow.refund(ISSUE);

        assertEq(eurc.balanceOf(PUBLISHER) - before, EUR_FIX);
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    /// The ceiling is quoted in the issue's own 6-decimal base units, so an
    /// over-ceiling EURC commitment is refused exactly as a USDC one is.
    function test_the_ceiling_applies_to_the_issue_currency() public {
        escrow.setIssueToken(ISSUE, address(eurc));
        _approve(100_000_000);

        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.ExceedsCeiling.selector, EUR_FIX, 100_000_000)
        );
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, EUR_FIX, uint64(block.timestamp + 1 days));
    }

    /// An 18-decimal token is the view gas is accounted in, and letting it in is
    /// the way to lose funds. It is refused when it is named, not when the money
    /// has already moved.
    function test_a_token_with_eighteen_decimals_cannot_be_named() public {
        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.WrongDecimals.selector, address(nativeView), 18)
        );
        escrow.setIssueToken(ISSUE, address(nativeView));

        assertEq(escrow.tokenOf(ISSUE), address(usdc));
    }

    /// Arc's native sentinel has no code at all, and `decimals()` on it reverts
    /// rather than answering. The escrow must not install what it cannot read.
    function test_an_address_that_is_not_a_token_cannot_be_named() public {
        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.NotAToken.selector, address(0xD15EA5E))
        );
        escrow.setIssueToken(ISSUE, address(0xD15EA5E));
    }

    /// The zero address is not a token either, and the error should say so.
    function test_the_zero_address_cannot_be_named_as_an_issue_currency() public {
        vm.expectRevert(MisthosEscrow.ZeroAddress.selector);
        escrow.setIssueToken(ISSUE, address(0));
    }

    /// Naming a currency is an admin decision, like the ceiling.
    function test_only_the_owner_can_name_the_issue_currency() public {
        vm.expectRevert(MisthosEscrow.NotOwner.selector);
        vm.prank(PUBLISHER);
        escrow.setIssueToken(ISSUE, address(eurc));
    }

    /// Once the money is committed the currency is historical fact: changing it
    /// afterwards would strand the commitment in a token nobody paid.
    function test_the_issue_currency_is_fixed_once_the_money_is_committed() public {
        _commit();

        vm.expectRevert(MisthosEscrow.CommitmentStarted.selector);
        escrow.setIssueToken(ISSUE, address(eurc));
    }
}
