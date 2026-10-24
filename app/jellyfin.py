import asyncio
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class Jellyfin:
    def __init__(self, url: str, api_key: str | None, user: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.enabled = bool(api_key)
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            timeout=15,
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
            transport=transport,
        )
        # Playlists belong to a user; None = the first administrator
        self._user_name = user
        self._user_id: str | None = None
        self._playlist_ids: dict[str, str] = {}
        self._playlist_lock = asyncio.Lock()  # two songs for a new playlist must not create it twice

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

    async def add_to_playlists(self, path: Path, playlists: list[str], timeout: float = 300, poll: float = 5) -> None:
        """Waits for Jellyfin to index the file, then adds it to each playlist, creating any that are missing.

        Raises on failure so the caller can report it.
        """
        if not self.enabled or not playlists:
            return
        item_id = await self._wait_for_item(path, timeout, poll)
        user_id = await self._user()
        for name in playlists:
            playlist_id = await self._playlist(name, user_id)
            r = await self._client.get(f"/Playlists/{playlist_id}/Items", params={"userId": user_id})
            if r.status_code == 404:  # deleted in Jellyfin since we cached its id
                self._playlist_ids.pop(name, None)
                playlist_id = await self._playlist(name, user_id)
                r = await self._client.get(f"/Playlists/{playlist_id}/Items", params={"userId": user_id})
            r.raise_for_status()
            if any(i["Id"] == item_id for i in r.json()["Items"]):
                continue
            r = await self._client.post(f"/Playlists/{playlist_id}/Items", params={"ids": item_id, "userId": user_id})
            r.raise_for_status()
            log.info("Added %s to playlist %r", path, name)

    async def _wait_for_item(self, path: Path, timeout: float, poll: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            # Newest audio first: the file was just added, so it's at the top once indexed
            r = await self._client.get("/Items", params={
                "Recursive": "true", "IncludeItemTypes": "Audio", "Fields": "Path",
                "SortBy": "DateCreated", "SortOrder": "Descending", "Limit": 50,
            })
            r.raise_for_status()
            for item in r.json()["Items"]:
                if item.get("Path") == str(path):
                    return item["Id"]
            if loop.time() >= deadline:
                raise TimeoutError(f"Jellyfin did not index {path} within {timeout:.0f}s")
            await asyncio.sleep(poll)

    async def _user(self) -> str:
        if self._user_id:
            return self._user_id
        r = await self._client.get("/Users")
        r.raise_for_status()
        users = r.json()
        match = [u for u in users if u["Name"] == self._user_name] if self._user_name else \
            [u for u in users if u["Policy"]["IsAdministrator"]]
        if not match:
            raise RuntimeError(f"Jellyfin user {self._user_name or '(any administrator)'} not found")
        self._user_id = match[0]["Id"]
        return self._user_id

    async def _playlist(self, name: str, user_id: str) -> str:
        async with self._playlist_lock:
            if name in self._playlist_ids:
                return self._playlist_ids[name]
            r = await self._client.get("/Items", params={
                "Recursive": "true", "IncludeItemTypes": "Playlist", "userId": user_id, "searchTerm": name,
            })
            r.raise_for_status()
            existing = [i for i in r.json()["Items"] if i["Name"].casefold() == name.casefold()]
            if existing:
                playlist_id = existing[0]["Id"]
            else:
                r = await self._client.post(
                    "/Playlists", json={"Name": name, "Ids": [], "UserId": user_id, "MediaType": "Audio"}
                )
                r.raise_for_status()
                playlist_id = r.json()["Id"]
                log.info("Created playlist %r", name)
            self._playlist_ids[name] = playlist_id
            return playlist_id

    async def aclose(self) -> None:
        await self._client.aclose()
