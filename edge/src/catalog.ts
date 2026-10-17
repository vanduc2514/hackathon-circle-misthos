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
    name: 'Decision export',
    summary: 'The whole append-only decision log, a page at a time, with filters.',
    group: 'Trust',
    returns: 'DecisionPage',
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
    name: 'Contributor standing',
    summary: 'Score, percentile, verdicts, disputes and specialisation, from the ledger.',
    group: 'Trust',
    returns: 'Reputation',
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
    name: 'Escrow attestation',
    summary: 'A signed certificate of what the escrow holds, read from the chain.',
    group: 'Trust',
    returns: 'Attestation',
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
    name: 'Price an issue',
    summary: "The engine's suggested price, range and confidence, with the comparables behind it.",
    group: 'Analytics',
    returns: 'PriceReport',
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
    name: 'Comparables',
    summary: 'Settled issues of the same shape, their prices and what each implies.',
    group: 'Analytics',
    returns: 'ComparablePage',
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

/** #204: the review agent, as a pre-flight check anyone can buy. */
const REVIEW: PaidRoute[] = [
  {
    id: 'review',
    name: 'Pre-flight review',
    summary: "The review agent's verdict on a diff against acceptance criteria, before you submit.",
    group: 'Analytics',
    returns: 'ReviewJob',
    method: 'POST',
    path: '/paid/review',
    price: '2.00',
    description:
      'A pre-flight review: the Misthos review agent holds a diff to your acceptance ' +
      'criteria and returns the verdict, findings and a met / unmet / unjudged check ' +
      'per criterion with its evidence. Body: criteria (1 to 20), and a unified diff ' +
      'or a pull_request {repo, number}; optionally title, repo, checks_passed. ' +
      'Answers 202 with a job; poll it with the receipt. Changes no issue.',
    core: (req) => ({ method: 'POST', path: `${CORE_PAID_PREFIX}review${queryOf(req)}` }),
    async: true,
    capped: true,
    note:
      'Gateway settles the exact scheme only, so the cap is the price: you pay it once ' +
      'the finished review is delivered, and nothing if the review fails.',
    example: {
      body: {
        criteria: ['Covered by a test', 'Documented in the changelog'],
        diff: 'diff --git a/tests/test_x.py b/tests/test_x.py\n--- /dev/null\n+++ b/tests/test_x.py\n@@ -0,0 +1 @@\n+def test_x(): pass\n',
      },
    },
  },
]

/** #206: deal flow and settlement artefacts, and a screen that stays off. */
const DEALS: PaidRoute[] = [
  {
    id: 'opportunities',
    name: 'Funded work',
    summary: 'Funded issues still open: price, deadline, claim state and criteria.',
    group: 'Marketplace',
    returns: 'OpportunityPage',
    method: 'GET',
    path: '/paid/opportunities',
    price: '0.02',
    description:
      'Funded issues still open: price, what the contributor receives, deadline, claim ' +
      'state, repo, labels and acceptance criteria. Filters: repo, labels (comma ' +
      'separated), min_price, max_price, claimable (true or false), sort (newest, ' +
      'price, deadline). limit 1 to 100 (default 25); follow `next`.',
    core: (req) => ({ method: 'GET', path: `${CORE_PAID_PREFIX}opportunities${queryOf(req)}` }),
    freeTier: true,
    example: { query: { claimable: 'true', sort: 'price', limit: '25' } },
  },
  {
    id: 'receipt',
    name: 'Settlement receipt',
    summary: "A signed receipt of an issue's release or refund, checked against the ledger.",
    group: 'Trust',
    returns: 'Receipt',
    method: 'GET',
    path: '/paid/receipt/:settlement_id',
    price: '0.01',
    description:
      "A signed receipt of an issue's settlement (the settlement id is the issue's id): " +
      'released or refunded, gross, the platform fee, net, when, and the escrow and ' +
      'commitment transaction, checked against the money ledger and the escrow. The ' +
      "release transfer is in the contributor's own receipt only.",
    core: (req) => ({
      method: 'GET',
      path: `${CORE_PAID_PREFIX}receipt/${segment(req.params.settlement_id)}${queryOf(req)}`,
    }),
    freeTier: true,
    ref: (req) => String(req.params.settlement_id),
    example: { params: { settlement_id: 'ISS-1004' } },
  },
  {
    id: 'screen',
    name: 'Sanctions screen',
    summary: 'A sanctions-list check of one wallet. Off until its terms are cleared.',
    group: 'Trust',
    returns: 'Screening',
    method: 'POST',
    path: '/paid/screen',
    price: '0.25',
    description:
      'A sanctions-list check of one wallet: clear or hit, and the list. Body: ' +
      '{"address": "0x…"}. Off until the provider\'s terms and a legal review allow it.',
    core: (req) => ({ method: 'POST', path: `${CORE_PAID_PREFIX}screen${queryOf(req)}` }),
    unavailable: () =>
      process.env.EDGE_PAID_SCREENING === 'true'
        ? null
        : 'sanctions screening is off until its provider terms and a legal review clear it',
    example: { body: { address: '0x0000000000000000000000000000000000000001' } },
  },
]

export const CATALOG: PaidRoute[] = [...VIEWS, ...PRICING, ...REVIEW, ...DEALS]
