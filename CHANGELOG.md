# Changelog

Notable changes to Misthos, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Removed

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
