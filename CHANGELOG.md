# Changelog

Notable changes to Misthos, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- **Enterprise staff sign in through their organisation's own identity provider** (#53).
  An Enterprise organisation connects its OpenID Connect provider (issuer, client id,
  the client secret by reference in the secret store, and the e-mail domains it speaks
  for). Staff sign in with a work e-mail and act for the organisation, and the decision
  log names the person. The organisation can require single sign-on, from a session that
  came through it; removing the connection or a domain ends its sessions at once.
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

- An issue's "Next step" panel no longer shows a source comment as text (#168, #169).
