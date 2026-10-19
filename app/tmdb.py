"""Canonicalises guessed titles via TMDB so Jellyfin matches reliably."""

import logging
from dataclasses import replace

import httpx

from .naming import MediaInfo

log = logging.getLogger(__name__)
API = "https://api.themoviedb.org/3"


class Tmdb:
    def __init__(self, api_key: str | None):
        self.enabled = bool(api_key)
        # v4 "read access tokens" are long JWTs sent as a bearer; v3 keys go in the query
        if api_key and len(api_key) > 40:
            self._client = httpx.AsyncClient(base_url=API, timeout=15, headers={"Authorization": f"Bearer {api_key}"})
        else:
            self._client = httpx.AsyncClient(base_url=API, timeout=15, params={"api_key": api_key or ""})

    async def enrich(self, info: MediaInfo) -> MediaInfo:
        """Returns info with TMDB's title/year/id, or unchanged if lookup fails."""
        if not self.enabled:
            return info
        try:
            hit = await self._search(info, with_year=True) or await self._search(info, with_year=False)
        except httpx.HTTPError as e:
            log.warning("TMDB lookup failed for %r: %s", info.title, e)
            return info
        if not hit:
            log.info("TMDB: no match for %r", info.title)
            return info

        title = hit.get("title") or hit.get("name") or info.title
        date = hit.get("release_date") or hit.get("first_air_date") or ""
        year = int(date[:4]) if date[:4].isdigit() else info.year
        return replace(info, title=title, year=year, tmdb_id=hit["id"])

    async def _search(self, info: MediaInfo, with_year: bool) -> dict | None:
        if with_year and not info.year:
            return None
        is_movie = info.kind == "movie"
        params = {"query": info.title}
        if with_year:
            params["year" if is_movie else "first_air_date_year"] = info.year
        r = await self._client.get("/search/movie" if is_movie else "/search/tv", params=params)
        r.raise_for_status()
        results = r.json().get("results") or []
        return results[0] if results else None

    async def aclose(self) -> None:
        await self._client.aclose()
