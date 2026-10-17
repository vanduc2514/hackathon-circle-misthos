"""Settings.

A deployment names its chain, its GitHub App and its keys here. Values without a
safe production default are refused at startup rather than guessed at.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from misthos.domain.network import Network, network_for


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MISTHOS_", env_file=".env", extra="ignore")

    app_name: str = "Misthos"
    # The network's name, and whether its money is real, follow from the chain id;
    # there is deliberately no separate label to set. See `network` below.
    chain_id: int = 5042002
    rpc_url: str = "https://rpc.testnet.arc.io"
    explorer_url: str = "https://explorer.testnet.arc.io"
    # Where `mise run contracts:deploy` recorded MisthosEscrow. Empty means the record
    # for `chain_id` under contracts/deployments/. An explicit `escrow_contract` wins.
    # With neither the process refuses to start.
    escrow_deployment_file: str = ""
    escrow_contract: str = ""
    usdc_address: str = "0x3600000000000000000000000000000000000000"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    github_webhook_secret: str = "dev-secret"
    # The GitHub App. With both set, the platform reads issues and repositories and
    # posts criteria, reviews and statuses as the App. Both are required.
    # The key is the App's PEM private key; escaped newlines (\n) are accepted.
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_api_url: str = "https://api.github.com"
    # Linking a user's own GitHub account (#80): the App's OAuth client credentials.
    # Empty means linking is not offered.
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""

    # Sign-in (#70). Where the web app is served: the domain a sign-in message must
    # name, and where GitHub returns after linking. The session secret signs session
    # tokens; set it wherever more than one process, or a restart, must keep sessions.
    public_url: str = "http://localhost:5173"
    session_secret: str = ""

    # GitHub, driven from the repository (#6): an issue given this label, in a repository
    # a publisher installed the App on, is priced without opening the web app.
    github_label: str = "misthos"
    # The App's slug on github.com, for the web app's "install on a repository" link.
    github_app_slug: str = ""
    circle_api_key: str = ""
    # The edge service holds the Circle wallet SDK and sells the paid API. The token
    # proves a call came from the core, and one to the core came from the edge: the
    # edge refuses wallet routes without it, and the internal paid routes answer 404.
    edge_url: str = "http://127.0.0.1:8080"
    edge_core_token: str = ""
    # Where buyers reach the edge's paid API from outside, such as
    # https://paid.example.com. Shown on the web app's API page; `edge_url` is the
    # address the core uses inside the network, and is never shown.
    paid_public_url: str = ""

    # The review agent. With a key, a model judges each pull request against its
    # acceptance criteria; without one, the rule reviewer judges what the file list
    # proves. The URL is any Anthropic-compatible /v1/messages endpoint, and it is
    # DeepSeek's by default so a DeepSeek key is all this needs; set it to
    # https://api.anthropic.com to send the review to Claude instead. Prices are
    # dollars per million tokens, for the cost of each verdict; check them, and the
    # model id, against the provider's models page. DeepSeek maps an unknown
    # `claude-*` name onto its own model, so the default model id still resolves.
    anthropic_api_key: str = ""
    anthropic_api_url: str = "https://api.deepseek.com/anthropic"
    review_model: str = "claude-sonnet-5-5"
    review_input_usd_per_mtok: float = 2.0
    review_output_usd_per_mtok: float = 10.0
    # A disputed verdict is reviewed again by this model; empty uses review_model.
    review_dispute_model: str = ""

    # Empty keeps state in memory, lost on restart. A
    # Postgres URL makes it durable, and sqlite:///path does the same in a local file.
    database_url: str = ""
    # The sweeper applies claim expiry, deadline refunds and the silent-publisher
    # release on a timer. It runs inside the API by default; turn this off where a
    # dedicated `python -m misthos.workers` process runs it instead.
    sweeper_in_process: bool = True
    sweep_interval_seconds: float = 60.0

    # Compliance. Identity verification is Stripe Identity; sanctions screening reads
    # the official OFAC SDN list. Both are real providers. Without the Stripe key a
    # first payout stays pending and is not paid.
    stripe_secret_key: str = ""
    stripe_api_url: str = "https://api.stripe.com"
    ofac_sdn_url: str = (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
    )
    ofac_refresh_seconds: int = 86_400
    ofac_cache_path: str = ""

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
    # The key that signs the paid API's certificates: escrow attestations and
    # settlement receipts (#202). A reference into the secret store, like the two
    # above, and a different key from both: it vouches for what the escrow holds and
    # can move nothing. Unset, the signed routes answer 503 and nobody is charged.
    certificate_secret_ref: str = ""
    # The paid sanctions screen (#206). Off until the screening provider's terms and a
    # legal review allow reselling its answers; until then it answers 503.
    paid_screening_enabled: bool = False
    # A mounted directory holding the referenced keys, one file each, mode 600. For
    # local testing and anvil; a production deployment supplies its managed store.
    secret_store_dir: str = ""

    @property
    def network(self) -> Network:
        return network_for(self.chain_id)

    @property
    def chain(self) -> str:
        """The network's name, as recorded on escrows, wallets and statements."""
        return self.network.name

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
