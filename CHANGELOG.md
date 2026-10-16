# Changelog

Notable changes to Misthos, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- **A single sign-in gate replaces bifurcated onboarding** (#181). Visitors now enter Misthos through a single mandatory three-step gate:
  1. Prove a wallet (MetaMask first, or a user-controlled Circle wallet signing via `ChallengeType.SIGN_MESSAGE`).
  2. Link a GitHub account before choosing a side. The login is held as a pending link against the wallet, enforcing one-login-one-wallet for pending links as well as accounts.
  3. Choose a side (publisher or contributor) and name the party, creating the account carrying the linked GitHub login.
  - The app opens at `/overview` only when all three steps are complete. Leaving early costs nothing and leaves no account behind; reopening resumes at the first unfinished step.
  - The gate is an accessible modal dialog (`role="dialog"`, `aria-modal="true"`, focus trap/restore, Esc to close).
  - All landing links into the app open the gate when unfinished; the landing header reads *Finish signing in* while unfinished.
  - The profile page (`/account`) displays identity and GitHub App installation rather than acting as a second onboarding wizard.
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
