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
 * The one thing the platform is paid from this contract is its take rate, and it
 * is carved out inside the same `release` call rather than accumulated here. The
 * rate and its recipient are set by the owner and bounded on chain, so a fee can
 * never be raised above the published ceiling (docs/misthos/06) or sent somewhere
 * the owner did not name.
 *
 * An issue's id is the keccak of a public platform id, so the approval that lets
 * money in names who may send it and for how long: only the publisher whose price
 * was approved can commit, and only to a deadline no later than the one approved.
 * Without that, anyone could commit dust first with a deadline decades away, and
 * the issue could never be funded or refunded.
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
    error NoCeiling();
    error ExceedsCeiling(uint256 amount, uint256 ceiling);
    error NotApprovedPublisher(address sender);
    error DeadlineTooLate(uint64 deadline, uint64 latest);
    error FeeTooHigh(uint256 bps, uint256 max);
    error FeeRecipientNotSet();
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
    /// @param contributorAmount What the contributor actually received: the
    ///        commitment less the take rate. The fee is reported separately.
    event Released(
        bytes32 indexed issueId,
        address indexed contributor,
        uint256 contributorAmount
    );
    event PlatformFeePaid(bytes32 indexed issueId, address indexed recipient, uint256 amount);
    event Refunded(bytes32 indexed issueId, address indexed publisher, uint256 amount);
    event CeilingUpdated(
        bytes32 indexed issueId, uint256 ceiling, address indexed publisher, uint64 latestDeadline
    );
    event IssueTokenSet(bytes32 indexed issueId, address indexed token);
    event FeeUpdated(bytes32 indexed issueId, uint256 bps);
    event FeeRecipientUpdated(address indexed previousRecipient, address indexed newRecipient);
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

    /// @notice Per-issue ceiling: the most that may be committed for the issue.
    ///         It is the price a human approved, recorded here at the approval
    ///         checkpoint. An issue without one cannot be funded, so an agent
    ///         holding the publisher's key can never commit more than a person
    ///         agreed to. A budget an agent cannot exceed is a contract, not a
    ///         prompt, and it holds on testnet, where Circle's spending policies
    ///         do not exist.
    /// @dev In base units of the issue's token, which is always 6 decimals.
    mapping(bytes32 => uint256) public ceiling;

    /// @notice The only wallet that may commit to an issue: the publisher whose price
    ///         was approved, named by the owner together with the ceiling.
    mapping(bytes32 => address) public approvedPublisher;

    /// @notice The latest deadline a commitment to the issue may carry, named with the
    ///         ceiling. It keeps the refund near: a commitment the platform cannot book
    ///         goes back to the publisher at a deadline the approval bounded, not one
    ///         the sender chose.
    mapping(bytes32 => uint64) public latestDeadline;

    /// @notice The ERC-20 an issue is denominated in. Unset means USDC, which is
    ///         what every issue was before EURC existed. Named by the owner before
    ///         the commitment, and refused unless it has 6 decimals: the escrow
    ///         holds ERC-20 amounts only, never the 18-decimal native view.
    mapping(bytes32 => address) public issueToken;

    /// @notice The platform's take rate for an issue, in basis points, set from
    ///         the publisher's tier before the work is accepted. Zero means the
    ///         contributor receives the whole commitment.
    mapping(bytes32 => uint256) public feeBps;

    /// @notice Where the take rate goes on release. A platform treasury address,
    ///         never a balance this contract accumulates.
    address public feeRecipient;

    /// @notice The published ceiling on the take rate, in basis points. Above 15
    ///         percent the tier table in docs/misthos/06 says the rate is
    ///         renegotiated, so the contract refuses to carry one.
    uint256 public constant MAX_FEE_BPS = 1500;

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
     * @notice Commit the price for an issue. One commitment per issue id, and only
     *         from the publisher the owner named for it.
     * @param issueId  keccak256 of the platform issue identifier.
     * @param amount   Total committed in 6-decimal base units of the issue's
     *                 token (USDC, or EURC when the owner named it), at most the
     *                 issue's ceiling. The publisher pays the fix price and nothing
     *                 else: the platform reviews the submission, so there is no
     *                 reviewer fee to fund alongside it.
     * @param deadline Unix seconds after which the publisher can reclaim. No later
     *                 than the latest deadline the owner named for the issue.
     */
    function commit(bytes32 issueId, uint256 amount, uint64 deadline) external {
        if (commitments[issueId].status != Status.None) revert AlreadyExists();
        if (amount == 0) revert ZeroAmount();
        if (deadline <= block.timestamp) revert DeadlinePassed();
        if (amount > type(uint96).max) revert AmountMismatch();

        uint256 cap = ceiling[issueId];
        if (cap == 0) revert NoCeiling();
        // The issue id is public. A commitment from anyone else would take the issue's
        // one slot, and the publisher's own would then be refused for good.
        if (msg.sender != approvedPublisher[issueId]) revert NotApprovedPublisher(msg.sender);
        if (amount > cap) revert ExceedsCeiling(amount, cap);
        uint64 latest = latestDeadline[issueId];
        if (deadline > latest) revert DeadlineTooLate(deadline, latest);

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
     * @notice Release the commitment on acceptance, carving out the platform's
     *         take rate.
     *
     * Pays the contributor in the issue's own token, less the platform's take rate.
     * The publisher paid the fix price and nothing else, so the fee is taken out of
     * the commitment rather than added to it: the contributor receives the remainder
     * and the platform's wallet receives the rate, from the same balance. The fee is
     * integer division on basis points and rounds down, which leaves the extra base
     * unit with the contributor.
     *
     * The attestor decides nothing about quality; it only records that acceptance
     * happened, whether that was the publisher's merge or the silent-publisher grace
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

        uint256 fee = (fixAmount * feeBps[issueId]) / 10_000;
        address recipient = feeRecipient;
        // A rate with nowhere to send the money would strand it in this contract,
        // where nobody has a claim on it. Refuse instead.
        if (fee != 0 && recipient == address(0)) revert FeeRecipientNotSet();

        uint256 contributorAmount = fixAmount - fee;
        c.status = Status.Released;
        emit Released(issueId, contributor, contributorAmount);
        if (fee != 0) emit PlatformFeePaid(issueId, recipient, fee);

        // The issue's own token: an issue held in EURC settles in EURC (#30), and the
        // fee comes out of the same balance.
        IERC20 token = IERC20(tokenOf(issueId));
        if (!token.transfer(contributor, contributorAmount)) revert TransferFailed();
        if (fee != 0 && !token.transfer(recipient, fee)) revert TransferFailed();
    }

    // ------------------------------------------------------------------ admin

    /// @notice Record the approved price as the most that may be committed for an
    ///         issue, the publisher who may commit it, and the latest deadline they may
    ///         commit it to. Zero removes all three, which makes the issue unfundable
    ///         again.
    /// @dev Only a cap: it can stop money entering, never move money already held.
    function setCeiling(bytes32 issueId, uint256 cap, address publisher, uint64 latest)
        external
        onlyOwner
    {
        if (cap == 0) {
            publisher = address(0);
            latest = 0;
        } else {
            if (publisher == address(0)) revert ZeroAddress();
            if (latest <= block.timestamp) revert DeadlinePassed();
        }
        ceiling[issueId] = cap;
        approvedPublisher[issueId] = publisher;
        latestDeadline[issueId] = latest;
        emit CeilingUpdated(issueId, cap, publisher, latest);
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

    /// @notice Set the take rate for an issue from the publisher's tier. Bounded
    ///         on chain, so a compromised owner key still cannot release at a rate
    ///         the published tier table forbids.
    /// @dev Refused once the money is in, like the currency, and for the same reason:
    ///      the publisher approved the price at this rate. Moving it afterwards would
    ///      charge a rate they never saw, and the settlement record — which reads the
    ///      rate back from here rather than from the publisher's plan — would stop
    ///      matching what this contract does.
    function setFee(bytes32 issueId, uint256 bps) external onlyOwner {
        if (commitments[issueId].status != Status.None) revert CommitmentStarted();
        if (bps > MAX_FEE_BPS) revert FeeTooHigh(bps, MAX_FEE_BPS);
        feeBps[issueId] = bps;
        emit FeeUpdated(issueId, bps);
    }

    /// @notice Point the take rate at a platform treasury address. Rotating it
    ///         does not move fees already paid.
    function setFeeRecipient(address next) external onlyOwner {
        if (next == address(0)) revert ZeroAddress();
        emit FeeRecipientUpdated(feeRecipient, next);
        feeRecipient = next;
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
