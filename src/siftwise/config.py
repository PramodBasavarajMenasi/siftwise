"""Settings loaded from environment variables (prefix ``SIFTWISE_``)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIFTWISE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://siftwise:siftwise@localhost:5432/siftwise"
    api_token: str | None = None
    max_upload_bytes: int = 50 * 1024 * 1024
    # test_stats are computed over this many days up to a repo's newest run (0 = all history).
    stats_window_days: int = Field(default=90, ge=0)
    # The read-only dashboard at /ui (README "Dashboard"): off unless set. Its routes aren't
    # mounted when off.
    dashboard: bool = False
    # Proxies whose X-Forwarded-Proto/-For/-Host are believed (comma-separated IPs or networks,
    # "*" for any): what `siftwise server start --forwarded-allow-ips` sets. The dashboard's
    # Origin check and cookie flags use the forwarded public scheme and host.
    forwarded_allow_ips: str = "127.0.0.1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
