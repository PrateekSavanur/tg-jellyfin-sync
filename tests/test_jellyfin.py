"""Playlist handling against a small in-memory fake of the Jellyfin API."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.jellyfin import Jellyfin

SONG = Path("/data/music/Lata Mangeshkar/Album/01 - Song.mp3")


class FakeJellyfin:
    def __init__(self, index_after_polls=0):
        self.users = [{"Id": "u-guest", "Name": "guest", "Policy": {"IsAdministrator": False}},
                      {"Id": "u-root", "Name": "root", "Policy": {"IsAdministrator": True}}]
        self.audio = {}            # path → item id, once "indexed"
        self.pending = {}          # path → polls left before it shows up
        self.playlists = {}        # id → {"Name", "items", "owner"}
        self.index_after_polls = index_after_polls

    def upload(self, path: Path, item_id: str):
        self.pending[str(path)] = (item_id, self.index_after_polls)

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        if p == "/Users":
            return httpx.Response(200, json=self.users)
        if p == "/Items" and q.get("IncludeItemTypes") == "Audio":
            for path, (item_id, left) in list(self.pending.items()):
                if left <= 0:
                    self.audio[path] = item_id
                    del self.pending[path]
                else:
                    self.pending[path] = (item_id, left - 1)
            return httpx.Response(200, json={"Items": [{"Id": i, "Path": pth} for pth, i in self.audio.items()]})
        if p == "/Items" and q.get("IncludeItemTypes") == "Playlist":
            term = q.get("searchTerm", "").lower()
            return httpx.Response(200, json={"Items": [
                {"Id": pid, "Name": pl["Name"]} for pid, pl in self.playlists.items()
                if term in pl["Name"].lower() and pl["owner"] == q.get("userId")
            ]})
        if p == "/Playlists" and request.method == "POST":
            body = json.loads(request.content)
            pid = f"pl-{len(self.playlists) + 1}"
            self.playlists[pid] = {"Name": body["Name"], "items": list(body["Ids"]), "owner": body["UserId"]}
            return httpx.Response(200, json={"Id": pid})
        if p.startswith("/Playlists/") and p.endswith("/Items"):
            pid = p.split("/")[2]
            if pid not in self.playlists:
                return httpx.Response(404)
            if request.method == "POST":
                self.playlists[pid]["items"].extend(q["ids"].split(","))
                return httpx.Response(204)
            return httpx.Response(200, json={"Items": [{"Id": i} for i in self.playlists[pid]["items"]]})
        return httpx.Response(404)


def client(fake, user=None):
    return Jellyfin("http://jf", "key", user, transport=httpx.MockTransport(fake.handler))


def by_name(fake):
    return {pl["Name"]: pl["items"] for pl in fake.playlists.values()}


def test_waits_for_indexing_then_creates_playlist_for_admin():
    fake = FakeJellyfin(index_after_polls=2)
    fake.upload(SONG, "song-1")

    async def go():
        jf = client(fake)
        await jf.add_to_playlists(SONG, ["Old Songs"], poll=0)
        await jf.aclose()

    asyncio.run(go())
    assert by_name(fake) == {"Old Songs": ["song-1"]}
    assert next(iter(fake.playlists.values()))["owner"] == "u-root"


def test_existing_playlist_reused_and_no_duplicate_entries():
    fake = FakeJellyfin()
    fake.playlists["pl-old"] = {"Name": "old songs", "items": ["song-1"], "owner": "u-root"}
    fake.upload(SONG, "song-1")

    async def go():
        jf = client(fake)
        await jf.add_to_playlists(SONG, ["Old Songs"], poll=0)
        await jf.aclose()

    asyncio.run(go())
    assert list(fake.playlists) == ["pl-old"]
    assert fake.playlists["pl-old"]["items"] == ["song-1"]


def test_concurrent_songs_create_the_playlist_once():
    fake = FakeJellyfin()
    other = SONG.with_name("02 - Other.mp3")
    fake.upload(SONG, "song-1")
    fake.upload(other, "song-2")

    async def go():
        jf = client(fake)
        await asyncio.gather(jf.add_to_playlists(SONG, ["Old Songs"], poll=0),
                             jf.add_to_playlists(other, ["Old Songs"], poll=0))
        await jf.aclose()

    asyncio.run(go())
    assert len(fake.playlists) == 1
    assert sorted(by_name(fake)["Old Songs"]) == ["song-1", "song-2"]


def test_playlist_deleted_in_jellyfin_is_recreated():
    fake = FakeJellyfin()
    fake.upload(SONG, "song-1")
    other = SONG.with_name("02 - Other.mp3")
    fake.upload(other, "song-2")

    async def go():
        jf = client(fake)
        await jf.add_to_playlists(SONG, ["Old Songs"], poll=0)
        fake.playlists.clear()  # user deleted it
        await jf.add_to_playlists(other, ["Old Songs"], poll=0)
        await jf.aclose()

    asyncio.run(go())
    assert by_name(fake) == {"Old Songs": ["song-2"]}


def test_named_user_owns_playlists():
    fake = FakeJellyfin()
    fake.upload(SONG, "song-1")

    async def go():
        jf = client(fake, user="guest")
        await jf.add_to_playlists(SONG, ["Mine"], poll=0)
        await jf.aclose()

    asyncio.run(go())
    assert next(iter(fake.playlists.values()))["owner"] == "u-guest"


def test_gives_up_when_never_indexed():
    fake = FakeJellyfin()

    async def go():
        jf = client(fake)
        try:
            await jf.add_to_playlists(SONG, ["Old Songs"], timeout=0, poll=0)
        finally:
            await jf.aclose()

    with pytest.raises(TimeoutError):
        asyncio.run(go())
    assert fake.playlists == {}
