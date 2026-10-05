// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);

    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/**
 * @title MisthosEscrow
 * @notice Holds the committed price for a funded issue on Arc, and releases it
 *         when the work is accepted or refunds it when the deadline passes.
 *
 * The whole point of this contract is that the platform is never a custodian.
 * A contributor can read the commitment on chain before writing any code, and
 * the platform has no way to move the money except by producing the attestation
 * this contract expects. That removes the failure mode that ended the previous
 * attempt in this category.
 *
 * Arc notes:
 *  - USDC is the native gas token AND an ERC-20 at a fixed predeploy address.
 *    The two views are the same balance. This contract only ever touches the
 *    ERC-20 view, which uses 6 decimals. Gas is accounted by the protocol in
 *    18 decimals and is never handled here.
 *  - Finality is deterministic and sub-second, so there is no confirmation
 *    window to wait out and no reorg handling.
 *  - The USDC blocklist is enforced at runtime. A transfer to or from a
 *    blocklisted address reverts, and the revert still consumes gas.
 */
contract MisthosEscrow {
    // ----------------------------------------------------------------- errors

    error NotOwner();
    error NotAttestor();
    error AlreadyExists();
    error NotHeld();
    error DeadlineNotReached();
    error DeadlinePassed();
    error ZeroAddress();
    error ZeroAmount();
    error AmountMismatch();
    error ExceedsCeiling(uint256 amount, uint256 ceiling);
    error TransferFailed();

    // ------------------------------------------------------------------ types

    enum Status {
        None,
        Held,
        Released,
        Refunded
    }

    struct Commitment {
        address publisher;
        uint96 amount; // 6-decimal USDC
        uint64 deadline;
        Status status;
    }

    // ----------------------------------------------------------------- events

    event Committed(
        bytes32 indexed issueId, address indexed publisher, uint256 amount, uint64 deadline
    );
    event Released(
        bytes32 indexed issueId,
        address indexed contributor,
        address indexed reviewer,
        uint256 fixAmount,
        uint256 reviewFee
    );
    event Refunded(bytes32 indexed issueId, address indexed publisher, uint256 amount);
    event CeilingUpdated(bytes32 indexed issueId, uint256 ceiling);
    event AttestorUpdated(address indexed previousAttestor, address indexed newAttestor);

    // ------------------------------------------------------------------ state

    /// @notice USDC ERC-20 on the target Arc network. 6 decimals.
    /// @dev Immutable rather than constant so tests can point it at a mock.
    address public immutable usdc;

    /// @notice The only address that can attest acceptance. Lives in a managed
    ///         secret store off chain, and is never held by the agent.
    address public attestor;

    /// @notice Able to rotate the attestor and set ceilings.
    address public immutable owner;

    mapping(bytes32 => Commitment) public commitments;

    /// @notice Optional per-issue ceiling. Zero means uncapped. A budget an agent
    ///         cannot exceed is a contract, not a prompt.
    mapping(bytes32 => uint256) public ceiling;

    // ------------------------------------------------------------- modifiers

    modifier onlyAttestor() {
        if (msg.sender != attestor) revert NotAttestor();
        _;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address initialAttestor, address usdc_) {
        if (initialAttestor == address(0) || usdc_ == address(0)) revert ZeroAddress();
        owner = msg.sender;
        attestor = initialAttestor;
        usdc = usdc_;
    }

    // --------------------------------------------------------- publisher side

    /**
     * @notice Commit the price for an issue. One commitment per issue id.
     * @param issueId  keccak256 of the platform issue identifier.
     * @param amount   Total committed in 6-decimal USDC, covering the fix and
     *                 the review fee.
     * @param deadline Unix seconds after which the publisher can reclaim.
     */
    function commit(bytes32 issueId, uint256 amount, uint64 deadline) external {
        if (commitments[issueId].status != Status.None) revert AlreadyExists();
        if (amount == 0) revert ZeroAmount();
        if (deadline <= block.timestamp) revert DeadlinePassed();
        if (amount > type(uint96).max) revert AmountMismatch();

        uint256 cap = ceiling[issueId];
        if (cap != 0 && amount > cap) revert ExceedsCeiling(amount, cap);

        commitments[issueId] = Commitment({
            publisher: msg.sender,
            amount: uint96(amount),
            deadline: deadline,
            status: Status.Held
        });

        emit Committed(issueId, msg.sender, amount, deadline);

        if (!IERC20(usdc).transferFrom(msg.sender, address(this), amount)) {
            revert TransferFailed();
        }
    }

    /**
     * @notice Reclaim the commitment once the deadline has passed and no
     *         acceptable work arrived. Anyone may call it; the money only ever
     *         goes back to the publisher.
     */
    function refund(bytes32 issueId) external {
        Commitment storage c = commitments[issueId];
        if (c.status != Status.Held) revert NotHeld();
        if (block.timestamp < c.deadline) revert DeadlineNotReached();

        c.status = Status.Refunded;
        emit Refunded(issueId, c.publisher, c.amount);

        if (!IERC20(usdc).transfer(c.publisher, c.amount)) revert TransferFailed();
    }

    // ------------------------------------------------------------ attestation

    /**
     * @notice Release the commitment on acceptance.
     *
     * Splits the commitment between the contributor and the reviewer in one
     * transaction. Paying the reviewer is the reason maintainers will accept
     * funded issues at all: their complaint about bounty platforms is the
     * unpaid review queue, not the money.
     */
    function release(
        bytes32 issueId,
        address contributor,
        uint256 fixAmount,
        address reviewer,
        uint256 reviewFee
    ) external onlyAttestor {
        Commitment storage c = commitments[issueId];
        if (c.status != Status.Held) revert NotHeld();
        if (block.timestamp > c.deadline) revert DeadlinePassed();
        if (contributor == address(0)) revert ZeroAddress();
        if (fixAmount + reviewFee != c.amount) revert AmountMismatch();

        c.status = Status.Released;
        emit Released(issueId, contributor, reviewer, fixAmount, reviewFee);

        if (!IERC20(usdc).transfer(contributor, fixAmount)) revert TransferFailed();

        if (reviewFee > 0) {
            if (reviewer == address(0)) revert ZeroAddress();
            if (!IERC20(usdc).transfer(reviewer, reviewFee)) revert TransferFailed();
        }
    }

    // ------------------------------------------------------------------ admin

    /// @notice Cap what may be committed for an issue. Zero means no cap.
    function setCeiling(bytes32 issueId, uint256 cap) external onlyOwner {
        ceiling[issueId] = cap;
        emit CeilingUpdated(issueId, cap);
    }

    function setAttestor(address next) external onlyOwner {
        if (next == address(0)) revert ZeroAddress();
        emit AttestorUpdated(attestor, next);
        attestor = next;
    }

    // ------------------------------------------------------------------ views

    function statusOf(bytes32 issueId) external view returns (Status) {
        return commitments[issueId].status;
    }

    function isHeld(bytes32 issueId) external view returns (bool) {
        return commitments[issueId].status == Status.Held;
    }
}
