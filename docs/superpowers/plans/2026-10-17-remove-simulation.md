# Remove simulation, run live only — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Delete simulation as a mode from the backend, edge and web app, replace the fake compliance providers with real ones, and keep the suite green through a test seam.

**Architecture:** Every boundary has one implementation. `build_chain`, `build_rail`, `build_github`, `build_reviewer`, `build_identity` and `build_screening` are configured from the environment and fail loudly when a required value is missing. Tests build a `Store` with in-memory fakes and install it through a test-only seam; none of that code ships.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, httpx, pytest; TypeScript/Express, node:test, Playwright; Helm/ArgoCD for the homelab.

**Spec:** `https://github.com/vanduc2514/hackathon-circle-misthos/issues/184` and `.../issues/155`.

## Global Constraints

- No `MISTHOS_SIMULATED` in shipped code. A deployment that still sets it logs once and continues live.
- `contracts/` is untouched; `contracts/deployments/5042002.json` stays byte-identical.
- Arc testnet stays labelled testnet. `MoneyKind` loses only `"simulated"`.
- Compliance fails closed: no configured identity/screening provider means funding and payout are refused, not approved.
- The homelab deployment at `https://mithios.nvduc.dev` keeps serving; its 10 issues, 2 publishers and 4 contributors are real rows and must not be deleted.

---

### Task 1: Remove the switch and the simulated money path

**Files**
- Modify: `backend/src/misthos/config.py`, `domain/network.py`, `main.py`, `services/chain/factory.py`, `services/billing/__init__.py`, `services/attestor.py`, `store.py`
- Delete: `services/chain/simulated.py`, `services/billing/simulated.py`
- Test: `backend/tests/unit/test_settings.py`, `test_network.py`, `test_chain_factory.py` (new), `test_attestor.py`

**Interfaces**
- `network_for(chain_id: int) -> Network`
- `build_chain(cfg: Settings, engine, issue_ids) -> ChainGateway` with no branch
- `build_rail() -> PaymentRail` returns `ArcRail`
- `assert_secrets_are_configured(reference: str, owner_reference: str | None)`

- [ ] Write failing tests: `MoneyKind` has no `simulated`; API startup refuses a live run with no escrow deployment; `MISTHOS_SIMULATED` present logs exactly one warning and does not change behaviour.
- [ ] Run them; expect failures.
- [ ] Delete the branches and the two modules; make `build_chain` always construct `ArcEscrow`; make the attestor always require references.
- [ ] Run the focused tests; expect pass.
- [ ] Commit.

### Task 2: Delete the demo surfaces, the seed and the anonymous-write branches

**Files**
- Modify: `backend/src/misthos/store.py`, `api/session.py`, `api/v1/issues.py`, `api/v1/auth.py`, `api/v1/plans.py`, `api/v1/publishers.py`, `schemas.py`, `services/statements.py`, `services/calibration.py`
- Test: `backend/tests/integration/test_demo_routes_are_gone.py` (new), `test_guards.py`, `test_api.py`

- [ ] Write failing tests: each of the seven demo routes answers 404; health has no `simulated`/`seeded_issues`; an anonymous write is 401/403; a fresh store has zero issues.
- [ ] Remove `_demo_only`, the seven routes, `store.reset`, `_seed`, `_seed_publishers`, `_seed_contributors`, the seed lock, `demo_pull_request`, `demo_merge`, `count_issues`, `allow_demo_reset`, and the simulation branches in the guards.
- [ ] Move `_ISSUE_SPECS` and the seed builders to `backend/tests/support/seed.py` (test-only), unchanged in content.
- [ ] Regenerate `frontend/src/lib/api-schema.d.ts` after the OpenAPI change.
- [ ] Commit.

### Task 3: Real compliance providers

**Files**
- Create: `backend/src/misthos/services/compliance/ofac.py`, `stripe_identity.py`, `backend/tests/unit/test_ofac.py`, `test_stripe_identity.py`, fixtures under `backend/tests/fixtures/`
- Delete: the `SimulatedIdentity`/`SimulatedScreening` classes
- Modify: `services/compliance/__init__.py`, `config.py`, `store.py`

**Interfaces**
- `OfacSdnScreening(url: str, refresh_seconds: int, cache_path: Path)` implements `screen(wallet) -> (ScreeningOutcome, str | None)`; loads the official SDN XML lazily, fails closed when the list is older than the refresh window or cannot be read.
- `StripeIdentity(secret_key: str, api_url: str)` implements `start(contributor_id) -> str` and `status(reference) -> IdentityStatus`; `UnconfiguredIdentity` returns `FAILED` and logs the missing variable.
- `build_screening(cfg)`, `build_identity(cfg)` select the provider or the fail-closed guard.

- [ ] Write failing tests against fixture XML and a stubbed HTTP transport: a listed address is a `HIT`; a clean address is `CLEAR`; a stale list makes funding fail; Stripe statuses map `requires_input`/`processing`/`verified`.
- [ ] Implement the providers; no network in tests.
- [ ] Wire them in `Store.__init__`; delete the fake classes.
- [ ] Commit.

### Task 4: Test seam and suite conversion

**Files**
- Create: `backend/tests/support/fakes.py`, `install.py`
- Modify: `backend/tests/conftest.py` and the 18 simulation-dependent suites

- [ ] `FakeChain` implements `ChainGateway` in memory (reuse the deleted `SimulatedChain` logic, test-only name); `FakeRail` likewise; `FakeGitHub` reuses the old fixtures; `FakeIdentity`/`FakeScreening` for unit tests only.
- [ ] `install_store(store)` swaps the module-level store for a test-built one; the application has no branch for it.
- [ ] Convert each suite to the fixture; delete tests that only proved a simulation branch.
- [ ] Run `mise run test:backend`; account for the fall in the count.
- [ ] Commit.

### Task 5: Edge and frontend

**Files**
- Delete: `edge/src/simulated.ts`, the simulated wallet service and gate branches
- Modify: `edge/src/x402-gate.ts`, `circle-wallets.ts`, `wallet-routes.ts`, `index.ts`, their tests
- Modify: `frontend/src/lib/wallet.ts`, `routes/Account.tsx`, `Dashboard.tsx`, `Plans.tsx`, `components/IssueActions.tsx`, `lib/github-link.ts`, `App.tsx`, `routes/Landing.tsx`, e2e specs
- Modify: `frontend/playwright.config.ts`, `.github/workflows/ci.yml`, `compose.yaml`, `mise.toml`

- [ ] Edge: gate and wallet service always live; missing Circle credentials or seller address refuse startup; health has no `simulated`.
- [ ] Frontend: remove `demoWallet`, the demo sign-in buttons, the reset control, the simulated-GitHub controls, and the `simulated` chip row. Sign-in is MetaMask or Circle.
- [ ] Harness: the API is built with the test seam; the specs sign in with a test wallet supplied by the seam, not a demo button.
- [ ] Run `mise run test:edge`, `test:web`, `test:e2e`.
- [ ] Commit.

### Task 6: Docs, changelog and the deployment for x402

**Files**
- Modify: `README.md`, `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`, `CHANGELOG.md`, `backend/.env.example`
- Homelab: `apps/misthos/misthos/values.yaml`, `secrets/misthos/*.sops.yaml` (keys only), ingress

- [ ] Replace "simulated by default" copy with the live configuration and the fail-closed compliance note.
- [ ] Generate an Arc testnet EOA for `MISTHOS_SELLER_ADDRESS`; store the key in the homelab secret, never in the repo.
- [ ] Add `MISTHOS_SELLER_ADDRESS`, `GATEWAY_*`, `EDGE_CORE_TOKEN`, `CIRCLE_API_KEY`, `CIRCLE_APP_ID` to the edge env; remove `MISTHOS_SIMULATED`.
- [ ] Add a Traefik ingress path `/paid` on `mithios.nvduc.dev` to the edge and verify a 402 body survives.
- [ ] Build and push `api-<sha>`, `web-<sha>`, `edge-<sha>`; update values; push the homelab repo; watch ArgoCD.
- [ ] Verify: health has no `simulated`, an unpaid request answers 402 with Arc testnet accepts, a paid settlement is on the explorer.
