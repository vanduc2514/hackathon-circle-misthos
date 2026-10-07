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
    escrow_contract: str = "0x7A3f19bE5c2D80416aB9e0C7d3F5a12B6c8E4d90"
    usdc_address: str = "0x3600000000000000000000000000000000000000"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    github_webhook_secret: str = "dev-secret"
    # The GitHub App. With both set, the platform reads issues and repositories and
    # posts criteria, reviews and statuses as the App; empty keeps GitHub simulated.
    # The key is the App's PEM private key; escaped newlines (\n) are accepted.
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_api_url: str = "https://api.github.com"
    circle_api_key: str = ""

    # Empty keeps state in memory: the zero-config demo, reset on every restart. A
    # Postgres URL makes it durable, and sqlite:///path does the same in a local file.
    database_url: str = ""
    # The sweeper applies claim expiry, deadline refunds and the silent-publisher
    # release on a timer. It runs inside the API by default; turn this off where a
    # dedicated `python -m misthos.workers` process runs it instead.
    sweeper_in_process: bool = True
    sweep_interval_seconds: float = 60.0

    # Wallets the simulated sanctions screening treats as listed, comma separated. A
    # real screening provider replaces the simulated one; this is for demos and tests.
    screening_denylist: str = ""

    # Redis for the per-issue lock, idempotency keys and rate limits across processes.
    # Empty keeps them in this process: right for one process, wrong for two.
    redis_url: str = ""
    # Requests per client per minute. Zero turns the limit off.
    rate_limit_publish_per_minute: int = 30
    rate_limit_actions_per_minute: int = 120

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
