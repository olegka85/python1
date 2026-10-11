from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERP_", env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://garment:garment@localhost:5432/garment"
    auth_mode: Literal["dev", "zitadel"] = "zitadel"
    dev_token: SecretStr = SecretStr("")
    dev_tenant: str = "demo-factory"
    dev_role: Literal["admin", "planner", "warehouse", "purchaser", "viewer"] = "admin"
    zitadel_issuer: str = ""
    zitadel_audience: str = ""
    zitadel_jwks_url: str = ""
    cerbos_url: str = ""
    worker_lease_seconds: int = 30
    worker_max_attempts: int = 5

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.worker_lease_seconds < 10 or not 1 <= self.worker_max_attempts <= 20:
            raise ValueError("Invalid worker lease or retry configuration")
        if self.environment == "production":
            if self.auth_mode != "zitadel" or not self.cerbos_url:
                raise ValueError("Production requires Zitadel and Cerbos")
            if not self.database_url.startswith("postgresql+asyncpg://"):
                raise ValueError("Production requires PostgreSQL")
            if (
                not all(
                    x.startswith("https://") for x in (self.zitadel_issuer, self.zitadel_jwks_url)
                )
                or not self.zitadel_audience
            ):
                raise ValueError("Production requires HTTPS issuer/JWKS and an audience")
        return self
