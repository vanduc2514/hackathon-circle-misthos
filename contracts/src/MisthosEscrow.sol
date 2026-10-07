// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);

    function transferFrom(address from, address to, uint256 amount) external returns (bool);

    function decimals() external view returns (uint8);
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
 *  - An issue is denominated in one ERC-20: USDC unless the owner names another
 *    6-decimal token, which is how a European publisher funds in EURC. A token
 *    whose `decimals()` is not 6 is refused when it is named, so the 18-decimal
 *    view of USDC can never be committed against this contract.
 *  - Finality is deterministic and sub-second, so there is no confirmation
 *    window to wait out and no reorg handling.
 *  - The USDC blocklist is enforced at runtime. A transfer to or from a
 *    blocklisted address reverts, and the revert still consumes gas.
 */
contract MisthosEscrow {
    // ----------------------------------------------------------------- errors

    error NotAToken(address token);
    error WrongDecimals(address token, uint256 decimals);
    error CommitmentStarted();
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
        uint256 fixAmount
    );
    event Refunded(bytes32 indexed issueId, address indexed publisher, uint256 amount);
    event CeilingUpdated(bytes32 indexed issueId, uint256 ceiling);
    event IssueTokenSet(bytes32 indexed issueId, address indexed token);
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
    /// @dev In base units of the issue's token, which is always 6 decimals.
    mapping(bytes32 => uint256) public ceiling;

    /// @notice The ERC-20 an issue is denominated in. Unset means USDC, which is
    ///         what every issue was before EURC existed. Named by the owner before
    ///         the commitment, and refused unless it has 6 decimals: the escrow
    ///         holds ERC-20 amounts only, never the 18-decimal native view.
    mapping(bytes32 => address) public issueToken;

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
     * @param amount   Total committed in 6-decimal base units of the issue's
     *                 token: USDC, or EURC when the owner named it. The publisher
     *                 pays the fix price and nothing else: the platform reviews
     *                 the submission, so there is no reviewer fee to fund
     *                 alongside it.
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

        if (!IERC20(tokenOf(issueId)).transferFrom(msg.sender, address(this), amount)) {
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

        if (!IERC20(tokenOf(issueId)).transfer(c.publisher, c.amount)) revert TransferFailed();
    }

    // ------------------------------------------------------------ attestation

    /**
     * @notice Release the commitment on acceptance.
     *
     * Pays the contributor in one transfer, in the issue's token. The platform
     * performs the review, so there is no second party holding a verdict and
     * nothing to split: the whole commitment is the fix price. The attestor
     * decides nothing about quality; it only records that acceptance happened,
     * whether that was the publisher's merge or the silent-publisher grace
     * period expiring.
     */
    function release(bytes32 issueId, address contributor, uint256 fixAmount)
        external
        onlyAttestor
    {
        Commitment storage c = commitments[issueId];
        if (c.status != Status.Held) revert NotHeld();
        if (block.timestamp > c.deadline) revert DeadlinePassed();
        if (contributor == address(0)) revert ZeroAddress();
        if (fixAmount != c.amount) revert AmountMismatch();

        c.status = Status.Released;
        emit Released(issueId, contributor, fixAmount);

        if (!IERC20(tokenOf(issueId)).transfer(contributor, fixAmount)) revert TransferFailed();
    }

    // ------------------------------------------------------------------ admin

    /// @notice Cap what may be committed for an issue. Zero means no cap.
    function setCeiling(bytes32 issueId, uint256 cap) external onlyOwner {
        ceiling[issueId] = cap;
        emit CeilingUpdated(issueId, cap);
    }

    /// @notice Name the ERC-20 an issue is denominated in, before it is funded.
    ///         USDC is the default. EURC is the European publisher's option, and
    ///         it is the same 6 decimals, which is the only kind of token this
    ///         contract can hold: an 18-decimal token, including the native view
    ///         of USDC, is refused here rather than reverting mid-release.
    function setIssueToken(bytes32 issueId, address token) external onlyOwner {
        if (commitments[issueId].status != Status.None) revert CommitmentStarted();
        if (token == address(0)) revert ZeroAddress();
        (bool ok, bytes memory data) = token.staticcall(abi.encodeCall(IERC20.decimals, ()));
        if (!ok || data.length < 32) revert NotAToken(token);
        uint256 tokenDecimals = abi.decode(data, (uint256));
        if (tokenDecimals != 6) revert WrongDecimals(token, tokenDecimals);
        issueToken[issueId] = token;
        emit IssueTokenSet(issueId, token);
    }

    function setAttestor(address next) external onlyOwner {
        if (next == address(0)) revert ZeroAddress();
        emit AttestorUpdated(attestor, next);
        attestor = next;
    }

    // ------------------------------------------------------------------ views

    /// @notice The ERC-20 an issue's commitment is denominated in: the token the
    ///         owner named, or USDC when nobody named one.
    function tokenOf(bytes32 issueId) public view returns (address) {
        address token = issueToken[issueId];
        return token == address(0) ? usdc : token;
    }

    function statusOf(bytes32 issueId) external view returns (Status) {
        return commitments[issueId].status;
    }

    function isHeld(bytes32 issueId) external view returns (bool) {
        return commitments[issueId].status == Status.Held;
    }
}
