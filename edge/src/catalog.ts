/**
 * Every route an agent can pay for, and its default price (#200).
 *
 * This list is the allowlist. The edge serves a paid route only if it is here, and
 * every one lives under `/paid/` and runs in the core under `/api/v1/internal/paid/`.
 * Nothing here may reach an auth, wallet, statement, policy or lifecycle route: the
 * catalog test walks every entry and checks.
 *
 * Prices are USDC, decimal text, inside the ranges #200 set. `EDGE_PRICES` overrides
 * any of them by id. Raw public reads stay free; what is sold here is bulk, derived
 * or signed.
 */

import { CORE_PAID_PREFIX, queryOf, segment, type PaidRoute } from './paid.js'

/** #202: paid views over data the platform already holds. */
const VIEWS: PaidRoute[] = [
  {
    id: 'decisions',
    method: 'GET',
    path: '/paid/decisions',
    price: '0.02',
    description:
      'The append-only decision log, a page at a time, oldest first. Filters: issue_id, ' +
      'actor, action, since, until. limit 1 to 200 (default 50); follow `next` as cursor.',
    core: (req) => ({ method: 'GET', path: `${CORE_PAID_PREFIX}decisions${queryOf(req)}` }),
    freeTier: true,
    example: { query: { limit: '50', actor: 'agent' } },
  },
  {
    id: 'reputation',
    method: 'GET',
    path: '/paid/reputation/:contributor_id',
    price: '0.05',
    description:
      "A contributor's standing taken apart: score, percentile, verdicts, rework and " +
      'disputes, and where they settled work. Aggregates only, from the money ledger.',
    core: (req) => ({
      method: 'GET',
      path: `${CORE_PAID_PREFIX}reputation/${segment(req.params.contributor_id)}${queryOf(req)}`,
    }),
    freeTier: true,
    ref: (req) => String(req.params.contributor_id),
    example: { params: { contributor_id: 'CON-1' } },
  },
  {
    id: 'escrow-attestation',
    method: 'GET',
    path: '/paid/escrow/:issue_id/attestation',
    price: '0.02',
    description:
      'A signed certificate of what MisthosEscrow holds for an issue: amount, status, ' +
      'deadline, publisher, contract, chain and commitment transaction, read from the ' +
      'chain and signed with EIP-191 by the published signer.',
    core: (req) => ({
      method: 'GET',
      path: `${CORE_PAID_PREFIX}escrow/${segment(req.params.issue_id)}/attestation${queryOf(req)}`,
    }),
    freeTier: true,
    ref: (req) => String(req.params.issue_id),
    example: { params: { issue_id: 'ISS-1001' } },
  },
]

export const CATALOG: PaidRoute[] = [...VIEWS]
