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

    async def scan_library(self, folder: Path) -> None:
        """Scans the library containing `folder` now.

        Jellyfin's own file watcher waits for a quiet period, which never comes during a batch upload.
        """
        r = await self._client.get("/Library/VirtualFolders")
        r.raise_for_status()
        libraries = [lib for lib in r.json() if str(folder) in lib.get("Locations", [])]
        if not libraries:
            raise RuntimeError(f"no Jellyfin library has the folder {folder}")
        r = await self._client.post(f"/Items/{libraries[0]['ItemId']}/Refresh", params={
            "Recursive": "true", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default",
        })
        r.raise_for_status()

    async def audio_index(self) -> dict[str, str]:
        """Path → item id of every song Jellyfin has indexed."""
        r = await self._client.get("/Items", params={
            "Recursive": "true", "IncludeItemTypes": "Audio", "Fields": "Path", "EnableImages": "false",
        }, timeout=60)
        r.raise_for_status()
        return {i["Path"]: i["Id"] for i in r.json()["Items"] if i.get("Path")}

    async def add_to_playlist(self, item_id: str, name: str) -> None:
        """Adds a song to the named playlist, creating the playlist if needed; no-op if it's already in it."""
        user_id = await self._user()
        playlist_id = await self._playlist(name, user_id)
        r = await self._client.get(f"/Playlists/{playlist_id}/Items", params={"userId": user_id})
        if r.status_code == 404:  # deleted in Jellyfin since we cached its id
            self._playlist_ids.pop(name, None)
            playlist_id = await self._playlist(name, user_id)
            r = await self._client.get(f"/Playlists/{playlist_id}/Items", params={"userId": user_id})
        r.raise_for_status()
        if any(i["Id"] == item_id for i in r.json()["Items"]):
            return
        r = await self._client.post(f"/Playlists/{playlist_id}/Items", params={"ids": item_id, "userId": user_id})
        r.raise_for_status()

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
