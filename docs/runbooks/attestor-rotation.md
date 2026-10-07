# Runbook: rotating the acceptance attestation key

The acceptance attestation key is the only thing that can release a commitment. It lives
in a managed secret store, is read by reference at signing time
([services/attestor.py](../../backend/src/misthos/services/attestor.py)), and is never in
an environment file, a config value or the repository. Rotating it is an operational
step: the escrow keeps the attestor in storage and exposes `setAttestor`, so nothing is
redeployed and no commitment is disturbed.

Rotate on any of these: a suspected exposure, a person with access leaving, the scheduled
interval, or a deploy that would otherwise need the key in a new place.

## What does not change

- The escrow address, the committed funds and the deadlines.
- Commitments already held: the new attestor can release them, because authority is read
  from storage at call time rather than fixed at deployment.
- The publisher and contributor, who never see the key and are not told about the change
  beyond the release landing.

## Steps

1. **Mint the new key** in the managed secret store, under a new reference, so the old
   one can be deleted rather than overwritten. Record the signer address.

   ```bash
   # The store's own CLI. The key is generated inside it and never printed.
   aws secretsmanager create-secret --name misthos/prod/attestor-2026-10 \
     --generate-secret-string
   ```

2. **Point the deployment at the new reference.** Set `MISTHOS_ATTESTOR_SECRET_REF` to
   the new name in the secret manager the service reads its environment from, then roll
   the API and worker. A restart is enough; there is no migration.

3. **Give the new key the authority on chain.** This is the only step that touches the
   contract, and it is a call rather than a deploy.

   ```bash
   export ESCROW=0x… # MISTHOS_ESCROW_CONTRACT
   cast send "$ESCROW" "setAttestor(address)" 0xNEW_ATTESTOR \
     --rpc-url "$ARC_TESTNET_RPC_URL" --account misthos-deployer
   cast call "$ESCROW" "attestor()(address)" --rpc-url "$ARC_TESTNET_RPC_URL"
   ```

4. **Verify before revoking.** Release one held commitment with the new key and confirm
   the `Released` event and the contributor's balance. `AttestorUpdated` on the contract
   is the audit record of the switch.

5. **Revoke the old key.** Disable it in the secret store first, prove the next release
   fails with `NotAttestor`, then delete it.

## Rollback

The rotation is reversible until step 5: call `setAttestor` with the previous address and
restore the previous `MISTHOS_ATTESTOR_SECRET_REF`. After step 5 the old key is gone, so
roll forward with another key rather than back.

## Guards

- No private key is ever passed as a command-line argument outside local testing. Use an
  encrypted keystore (`cast wallet import`) or the signer's own account.
- A key in the environment fails the process at startup:
  `assert_key_is_not_in_the_environment` is called from `create_app`.
  `MISTHOS_ATTESTOR_PRIVATE_KEY` and `MISTHOS_ATTESTOR_KEY` are the names it refuses.
- A deployment with `MISTHOS_SIMULATED=false` and no `MISTHOS_ATTESTOR_SECRET_REF` also
  fails at startup, because it could never attest a release.
- The owner is not the attestor. The owner sets ceilings and the fee recipient and its
  address is immutable, so changing it takes a redeploy. The attestor is the hot key and
  rotates without one.
