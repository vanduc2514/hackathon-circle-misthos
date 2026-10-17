# Changelog

Notable changes to Misthos, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- **The API page** (#208). The app's `API` link opens `/public-api`, a Misthos page in
  place of Swagger: every route a stranger may call, free or x402, grouped by job (Core,
  Marketplace, Analytics, Trust) with the price as a column, filtered by All, Free or
  x402. A row opens to show a call that works.
  - The free rows are the routes the API marks `x-misthos-public` in `openapi.json`,
    and no route that needs a session is marked; a test calls every one as a stranger.
  - The x402 rows are the edge's own catalog at the prices it charges, read through the
    new `GET /api/v1/catalog/paid` and kept a minute; if the edge cannot be read, the
    page still shows the free rows. `MISTHOS_PAID_PUBLIC_URL` names where buyers reach
    the edge.
  - The edge's seller page (`GET /paid` in a browser) wears the same design.
  - Screenshots are in `docs/screenshots/api-page/`.
- **The paid API's rail** (#200, #201). The edge sells x402 calls from an allowlist,
  `edge/src/catalog.ts`, and nothing else: anything under `/paid` it does not list is
  `404`, and nothing wraps `/api/v1/*`. `GET /paid` is the catalog, free. See
  [docs/PAID-API.md](docs/PAID-API.md).
  - **A failed call is free.** The edge verifies the payment with Circle Gateway, has
    the core answer, and settles only then; Circle's middleware settled first. A
    settlement that fails withholds the answer.
  - **A payment buys one request.** The same `PAYMENT-SIGNATURE` again gets the first
    answer, marked `X-Misthos-Replayed`, without running or settling twice; on another
    request it is `409 payment_already_used`.
  - **Every settlement is on the books**: one `x402.settled` log line, and a row in the
    core's new ledger of paid calls (migration `0019`), read back by
    `GET /api/v1/internal/paid/calls` with totals per route and network to reconcile
    with Gateway.
  - **A free tier**: free-tier routes are free to an `EDGE_FREE_API_KEYS` key, or to a
    signed-in session where the core's rule for the route says so.
  - **Per-route prices**, each the catalog's default unless `EDGE_PRICES` names it.
  - Long work answers `202` and a job, polled free with its receipt, and is settled when
    the result is delivered, once. A job that fails is never charged.
  - The core's paid routes live under `/api/v1/internal/paid/`, answer only the edge's
    `X-Misthos-Core-Token` (404 to anyone else), and are not in the API schema.

- **Paid views over existing data** (#202). Three routes on the edge, each free to its
  owner signed in and paid by everyone else:
  - `GET /paid/decisions`, $0.02 a page: the whole decision log, oldest first, filtered
    by issue, actor, action and time, up to 200 a page with a cursor. The public read
    still shows the newest 500 free.
  - `GET /paid/reputation/{contributor_id}`, $0.05: the ledger's standing, a 0 to 100
    score, a percentile, the verdicts, rework and disputes on the contributor's work,
    and where they settled it. Aggregates only: no wallet, no handle.
  - `GET /paid/escrow/{issue_id}/attestation`, $0.02: a certificate of what the escrow
    holds, read from the chain, compared with the record, and signed with EIP-191 by a
    new certificate key (`MISTHOS_CERTIFICATE_SECRET_REF`) the escrow does not know. A
    divergence is not certified. The signer is published at `GET /paid`.
  - An unknown filter is refused with 400 rather than ignored and charged for.
- **Paid pricing** (#203), free to a signed-in publisher:
  - `POST /paid/price`, $0.25: the engine's suggestion for an issue the caller
    describes, exactly what the issue page would say for the same inputs, with every
    comparable that moved it and a plain reason when the history is thin. No budget or
    policy in it. Each quote is kept against its payment (migration `0020`).
  - `GET /paid/comparables`, $0.10: the settled history, filtered by repository,
    labels and signals, a page at a time.
- **A paid pre-flight review** (#204). `POST /paid/review`, $2.00: the review agent
  holds a diff, or a pull request it reads, to acceptance criteria and answers the
  verdict, findings and per-criterion evidence, through the same reviewer and rules as
  the lifecycle review. It runs as a job: `202`, then a free poll with the receipt that
  delivers the result once and settles only then; a failed review is never charged.
  It changes no issue, and the signed-in review is unchanged. Circle Gateway has no
  `upto`, so the review is sold at its cap, and the 402 says so. Each review's latency,
  cost and verdict is kept for calibration (migration `0021`).
- **Opportunities, receipts and a screen that stays off** (#206).
  - `GET /paid/opportunities`, $0.02 a page, free signed in: funded issues still open,
    with price, net to the contributor, deadline and claim state, filtered by repo,
    labels, price and claimability, sorted and paged. Never who holds an issue.
  - `GET /paid/receipt/{settlement_id}`, $0.01, free to the issue's contributor and
    publisher: a signed receipt of the release or refund, checked against the ledger,
    the record and the escrow. The release transfer is in the contributor's own
    receipt only.
  - `POST /paid/screen`: off until the provider's terms and a legal review clear it,
    and until then a 503 before any price.
- **The paid API, hardened after review** (#200).
  - A payment is known by its payer and nonce, so the same authorisation spelled another
    way buys one answer, not one per spelling.
  - A settlement that goes unheard keeps the answer back and is retried with the same
    payment, without running the work again.
  - A review is settled when it finishes, polled or not, its result stays readable with
    its receipt until the job expires, and one wallet holds at most three unsettled
    jobs. A core outage while polling no longer drops a job.
  - An empty or non-JSON answer from the core is not settled.
  - The free tier needs a linked GitHub login, not just a wallet.
  - The paid review reads public pull requests only, and answers a private one as a
    missing one.
- **Distribution for the paid API** (#205).
  - `mcp/`, a new runtime folder: an MCP server with one tool per paid route, priced
    from the seller's catalog, that pays each 402 from the agent's own Gateway balance
    inside a spend cap and a per-call cap, waits for a review and collects it, and
    answers every error as a code with nothing internal in it. `npm run wallet` funds
    the Gateway balance. Its own CI job runs `tsc` and its tests.
  - `GET /paid` is also the seller page, in HTML for a browser.
  - The edge answers every error in JSON, without a stack or an address.
  - [docs/BUYING.md](docs/BUYING.md) for buyers, [docs/LISTING.md](docs/LISTING.md) for
    the marketplace listings, which are done by hand.

### Removed

- **`/paid/pricing-report`** (#201). It charged $0.25 for `/api/v1/metrics`, which is
  free. The edge's `/health` no longer names the core's address, and an unreachable
  core is `502 core_unavailable` with nothing else.
- **Simulation is gone** (#184). `MISTHOS_SIMULATED`, `SimulatedChain`, the simulated
  billing rail, the demo endpoints and stepper, the seed, the demo wallets and the
  edge's simulated wallet service are deleted. A deployment that still sets the old
  variable runs live and is told once at startup; every path now has one
  implementation, and it is the live one.
  - Compliance is no longer faked: identity is Stripe Identity and screening reads the
    official OFAC SDN list. Without a Stripe key a first payout stays pending; an
    unreadable SDN list blocks funding and payouts.
  - The browser suite is removed with the demo wallets and reset endpoint it drove; the
    pytest, vitest and node suites remain, and the API tests inject in-memory gateways
    at the boundaries. The suite count falls accordingly.
  - Migration `0018` drops the `simulated_escrow` and `simulated_escrow_ceilings` tables.
- - **The Loop, Spend and Plans pages, and the subscription model behind them** (#183).
  `/loop`, `/spend` and `/plans` are no longer routed and no link points at them; their
  API endpoints, `domain/plans.py`, `api/v1/plans.py`, `services/billing/` and
  `services/finance/` are gone, and a migration drops the `subscriptions` and
  `subscription_payments` tables and the publisher's `tier` column.
  - No endpoint answers 402 any more: nothing is gated by a plan. The release approval
    gate and the audit export stay, ungated, because a money control and an auditor's
    record are not tier features. A release over the publisher's approval threshold still
    waits for one of the named approvers.
  - The platform has one price discipline: the publisher names the price and the escrow
    carves out a flat 10 percent on settlement. The pricing engine, its advice on the
    issue page, the calibration corpus and `mise run pricing:calibrate` are untouched.
  - `MISTHOS_PLATFORM_WALLET` and `MISTHOS_FINANCE_CONNECTIONS` are gone from the
    configuration, `.env.example` and `compose.yaml`, and the documentation that priced a
    plan or described the spend view and finance integration is removed.

### Changed

- **Publish from connected repositories and issues** (#199). The publish form no
  longer takes an `owner/name` and an issue number as free text. It offers the
  publisher's connected repositories, then the open issues in the chosen one; pulling
  one lists that GitHub issue and adds the platform label to it, so the label-driven
  flow prices the same issue. Creating an issue is a button that reveals the title,
  description and labels fields, and creation applies the platform label too. The
  price field and the pricing flow are unchanged.
- **One way in: the sign-in gate** (#181). Signing in is a dialog in the landing page's
  look that asks for a wallet, then a GitHub account, then a side, and opens the app at
  `/overview` only when all three are done. Every way in from the landing page — *Sign
  in*, *Plans*, an issue's row, *See every issue* — and the app's own *Sign in* open it
  while it is unfinished, and the header says *Finish signing in* until then.
  - **Two wallet doors.** MetaMask first, and a Circle wallet as the second way in. A
    Circle wallet signs the same Sign-In with Ethereum message through a `SIGN_MESSAGE`
    challenge (`POST /auth/circle/session`, `/auth/circle/wallet`, `/auth/circle/sign`), and
    `/auth/verify` recovers its address exactly as it recovers MetaMask's. Its wallets
    are EOAs, because a smart account's signature cannot be recovered. Without a wallet
    extension the gate says so.
  - **GitHub before the side.** `POST /auth/github/start` and `/auth/github/simulate`
    take a signed-in wallet with no account, and the login is held against it until
    `POST /auth/role` creates the account, which adopts it. A held login is already one
    wallet's: another wallet is refused it, pending or linked, with the conflict named.
    `GET /auth/me` says which login is held, and GitHub's return lands on the gate,
    which says why when the link was refused or not approved.
  - **Leaving costs nothing.** Every step is kept by the API as it is done, and the gate
    reopens at the first one that is not.
  - **The profile is not a second sign-in.** `/account` shows who you are, the wallet
    the money moves through and a publisher's repositories, and sends you to the gate
    for anything missing. Its Circle wallet panel is gone: the wallet you sign in with is
    the wallet the money moves through.
- **The app wears the landing page's look** (#168, #169). Every page behind `/` is now light:
  white pages, black ink and buttons, Georgia headings and figures, and Arial text. The
  dark, mono, orange theme is gone.
  - Every app page has the landing's header and footer. The header carries the
    `m. misthos` brand, the nav, the network and money chips, and the way in. The footer
    still carries the deployment's own words about its money.
  - State colours stay, darkened for a white background, and text meets WCAG AA contrast.
  - On a phone the nav takes a row of its own and scrolls inside it, and tables scroll
    inside their panels, so no page scrolls sideways.
  - The web fonts are gone: Georgia, Arial and the system monospace need no download.
  - Screenshots of every page are in `docs/screenshots/app-restyle/`.
- **The web app opens on a landing page** (#140, #167). `/` is now the public front page
  from the `landing-page-for-misthos` prototype: what Misthos is, how an issue goes from
  funded to paid, and the way in.
  - Its community snapshot reads `/api/v1/metrics` and names the network and the money
    from `/api/v1/health`.
  - Its "Current issues" table lists `/api/v1/issues`, with the prototype's filters, and
    each row opens the issue in the app.
  - Signing in stays the API's own. The prototype's wallet sign-in, database and profile
    pages were not ported.
- **The overview moved from `/` to `/overview`.** The nav follows it, and the app's brand
  now carries the landing's `m.` mark and links home. The favicon is the same mark.

### Fixed

- The header's money chip says USDC rather than TEST USDC (#138).
- An issue's "Next step" panel no longer shows a source comment as text (#168, #169).
- **A `misthos` label is priced even when nothing recorded the repository's connection**
  (#180). A connection row is written by the `installation_repositories` webhook and by
  nothing else, and GitHub does not redeliver a delivery that failed, so a repository the
  App was installed on could have no row and no way to get one: every label on it was
  refused for ever, and refused silently. A label now asks GitHub which installation
  covers the repository and records what it says, and the sweeper's `reconcile_connections`
  does the same for every installation on each pass.
  - A label that cannot be priced answers on the issue and says so in the log, instead of
    posting nothing and leaving no trace.
