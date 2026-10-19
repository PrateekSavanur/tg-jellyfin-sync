from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

GiB = 1024**3


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram — api_id/hash from my.telegram.org, token from @BotFather
    tg_api_id: int
    tg_api_hash: str
    tg_bot_token: str
    tg_channel_id: int  # e.g. -1001234567890

    # Optional integrations — each is skipped when its key is unset
    tmdb_api_key: str | None = None
    jellyfin_url: str = "http://jellyfin.homelab.svc.cluster.local:8096"
    jellyfin_api_key: str | None = None
    homelert_webhook_url: str | None = None

    # Library paths must match what Jellyfin sees (same mount paths in both pods)
    movies_dir: Path = Path("/data/movies")
    tv_dir: Path = Path("/data/tvshows")
    unsorted_dir: Path = Path("/data/unsorted")
    # Must be on the same filesystem as the libraries so the final move is an atomic rename
    staging_dir: Path = Path("/data/staging")
    state_dir: Path = Path("/state")

    # Telegram caps non-Premium uploads at 2 GiB
    max_file_size: int = 2 * GiB
    delete_after_import: bool = True
    concurrency: int = 1
    progress_interval: float = 10.0
