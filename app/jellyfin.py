import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class Jellyfin:
    def __init__(self, url: str, api_key: str | None):
        self.enabled = bool(api_key)
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            timeout=15,
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
        )

    async def notify_created(self, path: Path) -> None:
        """Targeted scan of one new file instead of a full library refresh."""
        if not self.enabled:
            return
        try:
            r = await self._client.post(
                "/Library/Media/Updated",
                json={"Updates": [{"Path": str(path), "UpdateType": "Created"}]},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            # Not fatal: Jellyfin's scheduled scan will still pick the file up
            log.warning("Jellyfin refresh failed for %s: %s", path, e)

    async def aclose(self) -> None:
        await self._client.aclose()
