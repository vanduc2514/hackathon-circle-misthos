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

    function _commit() internal returns (uint64 deadline) {
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
        vm.expectRevert(MisthosEscrow.ZeroAmount.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, 0, uint64(block.timestamp + 1 days));
    }

    function test_commit_with_a_past_deadline_is_rejected() public {
        vm.expectRevert(MisthosEscrow.DeadlinePassed.selector);
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, uint64(block.timestamp - 1));
    }

    function test_ceiling_caps_what_an_agent_can_commit() public {
        escrow.setCeiling(ISSUE, 100_000_000);
        vm.expectRevert(
            abi.encodeWithSelector(MisthosEscrow.ExceedsCeiling.selector, FIX, 100_000_000)
        );
        vm.prank(PUBLISHER);
        escrow.commit(ISSUE, FIX, uint64(block.timestamp + 1 days));
    }

    // ---------------------------------------------------------------- release

    function test_release_pays_the_contributor_in_one_transfer() public {
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
        escrow.setCeiling(ISSUE, 100_000_000);

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
