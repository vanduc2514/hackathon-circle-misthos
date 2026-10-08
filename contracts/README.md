# contracts

`MisthosEscrow` holds a funded issue's price on Arc until the work is accepted or
the deadline passes. The platform cannot move the money; it can only attest that
acceptance happened.

## Deploy to Arc testnet

Fund the deployer from <https://faucet.circle.com> first. USDC is Arc's gas token.

```bash
cast wallet import misthos-deployer --interactive
export MISTHOS_FEE_RECIPIENT_ADDRESS=0x...   # required: where the take rate is paid
export MISTHOS_ATTESTOR_ADDRESS=0x...        # the attestor's address, never its key
mise run contracts:deploy
```

The script refuses to deploy without a fee recipient: every issue carries a take rate,
and a release with a rate and no recipient reverts `FeeRecipientNotSet`.

The task deploys, verifies the source on <https://explorer.testnet.arc.io>
(Blockscout, no API key), and, because it broadcasts, writes
`deployments/5042002.json`. Commit that record: the API reads the escrow address
from it. A dry run writes no record. Mainnet (chain 5042) is refused unless
`MISTHOS_CONFIRM_MAINNET=5042` is set for that one command. See
[docs/DEPLOY.md](../docs/DEPLOY.md) for the rest of the system.

After any change to the contract, run `mise run abi:contracts` and commit the
ABI; `mise run lint` fails while it is stale.

## The per-issue ceiling

Every issue needs a ceiling before it can be funded: the price a human approved,
recorded on chain by the owner at the approval checkpoint. `commit` reverts with
`NoCeiling()` when there is none and `ExceedsCeiling(amount, ceiling)` above it,
so an agent holding the publisher's key cannot commit more than a person agreed
to. This is the budget guardrail on testnet, where Circle's wallet spending
policies are not available.

The same approval names the only wallet that may commit and the latest deadline it may
commit to: `setCeiling(issue, ceiling, publisher, latestDeadline)`. Anyone else's
commitment reverts `NotApprovedPublisher(sender)` and a later deadline reverts
`DeadlineTooLate(deadline, latest)`, so nobody can take an issue's one commitment slot
with dust, and a commitment the platform cannot book is refundable no later than the
approval said.

### Showing it on testnet

With the escrow deployed and a publisher wallet holding testnet USDC:

```bash
ESCROW=$(jq -r .escrow deployments/5042002.json)
USDC=0x3600000000000000000000000000000000000000
ISSUE=$(cast keccak ISS-1001)
DEADLINE=$(( $(date +%s) + 14 * 86400 ))
PUBLISHER=$(cast wallet address --account misthos-publisher)
RPC=$ARC_TESTNET_RPC_URL

# Owner: record the approved price, 100.00 USDC, for this publisher until the deadline.
cast send $ESCROW "setCeiling(bytes32,uint256,address,uint64)" $ISSUE 100000000 \
  $PUBLISHER $DEADLINE --rpc-url $RPC --account misthos-deployer

# Publisher: allow the escrow to pull 150.00 USDC.
cast send $USDC "approve(address,uint256)" $ESCROW 150000000 \
  --rpc-url $RPC --account misthos-publisher

# Over the ceiling: reverts with ExceedsCeiling(150000000, 100000000).
cast send $ESCROW "commit(bytes32,uint256,uint64)" $ISSUE 150000000 $DEADLINE \
  --rpc-url $RPC --account misthos-publisher

# At the ceiling: succeeds, and the commitment is readable on the explorer.
cast send $ESCROW "commit(bytes32,uint256,uint64)" $ISSUE 100000000 $DEADLINE \
  --rpc-url $RPC --account misthos-publisher
```

`cast send` estimates gas first, so the over-ceiling call fails before it is
broadcast and costs nothing. To leave a reverted transaction on the explorer as
evidence, add `--gas-limit 100000`; that spends a few cents of testnet USDC.
