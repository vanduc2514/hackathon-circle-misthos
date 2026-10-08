"""Settings.

Everything has a safe default so the demo runs with no configuration at all.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MISTHOS_", env_file=".env", extra="ignore")

    app_name: str = "Misthos"
    simulated: bool = True
    chain: str = "arc-testnet"
    chain_id: int = 5042002
    rpc_url: str = "https://rpc.testnet.arc.io"
    explorer_url: str = "https://explorer.testnet.arc.io"
    # Where `mise run contracts:deploy` recorded MisthosEscrow. Empty means the record
    # for `chain_id` under contracts/deployments/. An explicit `escrow_contract` wins.
    # With neither, only the simulation runs, under a placeholder labelled as such.
    escrow_deployment_file: str = ""
    escrow_contract: str = ""
    usdc_address: str = "0x3600000000000000000000000000000000000000"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    github_webhook_secret: str = "dev-secret"
    # The GitHub App. With both set, the platform reads issues and repositories and
    # posts criteria, reviews and statuses as the App; empty keeps GitHub simulated.
    # The key is the App's PEM private key; escaped newlines (\n) are accepted.
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_api_url: str = "https://api.github.com"
    # Linking a user's own GitHub account (#80): the App's OAuth client credentials.
    # Empty keeps linking simulated.
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""

    # Sign-in (#70). Where the web app is served: the domain a sign-in message must
    # name, and where GitHub returns after linking. The session secret signs session
    # tokens; set it wherever more than one process, or a restart, must keep sessions.
    public_url: str = "http://localhost:5173"
    session_secret: str = ""

    # A publisher's books, read-only, for the affordability ceiling (#43). JSON from
    # publisher id to a connection; see services/finance. Empty uses the declared
    # budget alone.
    finance_connections: str = ""

    # Plans (#53). Where subscription payments are sent: the platform's own wallet on
    # Arc. The simulation uses a made-up one when this is empty; anywhere else plans
    # cannot be bought until it is set.
    platform_wallet: str = ""

    # GitHub, driven from the repository (#6): an issue given this label, in a repository
    # a publisher installed the App on, is priced without opening the web app.
    github_label: str = "misthos"
    # The App's slug on github.com, for the web app's "install on a repository" link.
    github_app_slug: str = ""
    circle_api_key: str = ""
    # The edge service holds the Circle wallet SDK. The token proves a call came
    # from the core; the edge refuses wallet routes without it.
    edge_url: str = "http://127.0.0.1:8080"
    edge_core_token: str = ""

    # The review agent. With a key, Claude judges each pull request against its
    # acceptance criteria; without one, the rule reviewer judges what the file list
    # proves. Prices are dollars per million tokens, for the cost of each verdict;
    # check them, and the model id, against Anthropic's models page.
    anthropic_api_key: str = ""
    anthropic_api_url: str = "https://api.anthropic.com"
    review_model: str = "claude-sonnet-5-5"
    review_input_usd_per_mtok: float = 2.0
    review_output_usd_per_mtok: float = 10.0
    # A disputed verdict is reviewed again by this model; empty uses review_model.
    review_dispute_model: str = ""

    # Empty keeps state in memory: the zero-config demo, reset on every restart. A
    # Postgres URL makes it durable, and sqlite:///path does the same in a local file.
    database_url: str = ""
    # The simulation's reset deletes every row of that database, and anyone who can
    # reach the API can ask for it, so it is refused while a database is configured
    # unless this is set. Set it only where the database is a throwaway demo.
    allow_demo_reset: bool = False
    # The sweeper applies claim expiry, deadline refunds and the silent-publisher
    # release on a timer. It runs inside the API by default; turn this off where a
    # dedicated `python -m misthos.workers` process runs it instead.
    sweeper_in_process: bool = True
    sweep_interval_seconds: float = 60.0

    # Wallets the simulated sanctions screening treats as listed, comma separated. A
    # real screening provider replaces the simulated one; this is for demos and tests.
    screening_denylist: str = ""

    # Logging and metrics. JSON lines for a log pipeline, plain text for a terminal.
    log_json: bool = False
    log_level: str = "INFO"
    # Where a separate worker serves its Prometheus metrics; zero serves none.
    worker_metrics_port: int = 0

    # Redis for the per-issue lock, idempotency keys and rate limits across processes.
    # Empty keeps them in this process: right for one process, wrong for two.
    redis_url: str = ""
    # Requests per client per minute. Zero turns the limit off.
    rate_limit_publish_per_minute: int = 30
    rate_limit_actions_per_minute: int = 120
    # Sign-in is the only unauthenticated write, and each attempt costs a secp256k1
    # recovery and a nonce the store has to remember, so it is budgeted like publish.
    rate_limit_signin_per_minute: int = 20

    # The name of the acceptance attestation key in the managed secret store. A
    # reference, never the key: see docs/runbooks/attestor-rotation.md.
    attestor_secret_ref: str = ""
    # The escrow owner's key, which records each approved price as the escrow's
    # ceiling (#34). Also a reference into the secret store, never the key.
    owner_secret_ref: str = ""
    # A mounted directory holding the referenced keys, one file each, mode 600. For
    # local testing and anvil; a production deployment supplies its managed store.
    secret_store_dir: str = ""

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
