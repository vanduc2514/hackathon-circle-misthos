# Privacy and retention

What Misthos holds about the people it pays, where it is kept, how long, and the one thing it never publishes. This is the policy [08 Trust, compliance and risk](./misthos/08-trust-compliance-and-risk.md#privacy) asks for, written down so the code can be held to it.

The retention periods below are the defaults we hold ourselves to. They are not legal advice, and they will be confirmed per jurisdiction with counsel before the platform moves real money.

## What we never publish

**The link between a contributor's wallet and their GitHub identity.** A public ledger, a public handle and a payout together identify a person to anyone who looks. So:

- No public page or API response carries a contributor's wallet. The contributor list serves a profile with no wallet and no identity-provider reference.
- The release transfer that pays a contributor names their wallet on chain, so its reference stays off the issue page, the timeline and the decision log. It appears only on the contributor's own annual statement.
- The decision log names contributors by handle, never by wallet.

`test_no_public_response_links_a_wallet_to_a_handle` reads every public response and fails the build if a contributor wallet or a payout reference appears in one.

**Identity documents.** We never receive them. A contributor hands them to the verification provider, and we keep only the provider's reference and the outcome.

## What we hold, and where

| Data | Where it lives | Why we have it |
| --- | --- | --- |
| GitHub handle | Our database | Already public; it is how work is attributed |
| Wallet address | Our database | To pay the contributor and to screen them |
| Identity documents | The verification provider only | Required before a first payout, and only then |
| Identity outcome and provider reference | Our database | To know whether a payout may leave the escrow |
| Sanctions screening results | Our database, append-only | Evidence that every payout was screened |
| Payout records | Our database, as append-only money events, and the chain | Statements, reconciliation, the audit trail |
| Decision log | Our database, append-only | The record that makes delegated authority defensible |

Identity is verified at a contributor's first payout, never at signup. Claiming and submitting work need no identity step, so the cost lands only on people who actually get paid.

## Retention

| Record | Kept for | How it is enforced |
| --- | --- | --- |
| Sanctions screening results | 5 years from the check | The sweeper deletes older records (`SCREENING_RETENTION`) |
| Identity outcome and provider reference | While the contributor can be paid, then 5 years from their last payout | Not automated yet |
| Identity documents | The provider's own retention period, under its contract with us | The provider |
| Payout records and annual statements | 7 years from the end of the tax year of the payout | Not automated yet |
| Decision log | The life of the platform | Append-only by design; it holds handles, never wallets |

## Statements

A contributor's annual statement lists every payout in a calendar year with its date, issue, counterparty, amount and settlement reference, built for a finance team to file from without asking us a question. Because the settlement references would link a wallet to a handle, a statement is served only to the contributor. Until contributors can sign in, the API serves statements only in the simulation, whose numbers are not real, and an operator exports a real one with `python -m misthos.services.statements <contributor> <year> --csv`.

## Known gaps

- `GET /publishers` still serves each publisher's remaining budget. Publisher spend is commercially sensitive and should be visible only to the publisher; that needs sign-in (#70, #80).
- Retention for identity references and payout records is a commitment, not yet a job.
