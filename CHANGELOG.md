# Changelog

Notable changes to Misthos, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- **Pricing suggestions come from a score curve, not hours** (#196). The suggestion no
  longer computes an effort estimate or applies a $90 hourly rate. The weighted 1-5 score
  maps directly to a price: `p(s) = 20 * 10^((s - 2.5) / 2.5)`, so score 2.5 is $20, score
  5.0 is $200, and score 1.0 is $5.02.
  - The 0.7x-1.4x range brackets the curve's price, and the recommendation is the curve
    point rather than the band midpoint.
  - Comparables scale a settled price by the curve's relative price for the two shapes,
    not by hours.
  - Live calibration: the previous engine's median recommendation on the 10 priced issues
    was $1,736 against a median named price of $20 (84.5x). The curve predicts $28.91
    (1.52x).
  - `estimated_hours` is gone from the API. The legacy database column is written as zero
    for proposals created by the new model.
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
