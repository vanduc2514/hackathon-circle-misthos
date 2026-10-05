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
    circle_api_key: str = ""

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
