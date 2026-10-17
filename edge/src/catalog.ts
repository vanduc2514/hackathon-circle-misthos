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

/** #203: the pricing engine and the settled history behind it. */
const PRICING: PaidRoute[] = [
  {
    id: 'price',
    method: 'POST',
    path: '/paid/price',
    price: '0.25',
    description:
      'What the Misthos pricing engine suggests for an issue you describe: the ' +
      'recommendation, its range, how confident it is and why, the signals it read, ' +
      'every settled comparable that moved it, and the justification. Body: title, ' +
      'body, repo, labels, and optionally signals (any of the six, 1 to 5), ' +
      'compliance_driven, prior_attempts. Advice: it never bounds a price.',
    core: (req) => ({ method: 'POST', path: `${CORE_PAID_PREFIX}price${queryOf(req)}` }),
    freeTier: true,
    example: {
      body: {
        title: 'Retry webhook deliveries that time out',
        body: 'Deliveries that time out are dropped. Expected: retried with backoff.',
        repo: 'acme/hooks',
        labels: ['bug'],
      },
    },
  },
  {
    id: 'comparables',
    method: 'GET',
    path: '/paid/comparables',
    price: '0.10',
    description:
      'Settled issues the engine compares against, with price, settled date, repo, ' +
      'distance and the price each implies. Filters: repo, labels (comma separated), ' +
      'and all six signals or none (code_surface, requirement_clarity, test_coverage, ' +
      'dependency_depth, prior_attempts, blast_radius). limit 1 to 100; follow `next`.',
    core: (req) => ({ method: 'GET', path: `${CORE_PAID_PREFIX}comparables${queryOf(req)}` }),
    freeTier: true,
    example: { query: { repo: 'acme/hooks', labels: 'bug', limit: '20' } },
  },
]

export const CATALOG: PaidRoute[] = [...VIEWS, ...PRICING]
