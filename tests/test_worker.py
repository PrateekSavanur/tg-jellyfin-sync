"""Pipeline tests against an in-memory fake of the Telethon client."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.jellyfin import Jellyfin
from app.store import Store
from app.tmdb import Tmdb
from app.worker import Importer


def make_msg(msg_id, name, size=1000, caption="", doc_id=None):
    file = SimpleNamespace(name=name, size=size, mime_type="video/x-matroska", ext=".mkv")
    return SimpleNamespace(id=msg_id, document=SimpleNamespace(id=doc_id or msg_id), file=file, message=caption)


class FakeClient:
    def __init__(self):
        self.msgs, self.sent, self.edits, self.deleted = {}, [], {}, []
        self._next_id = 1000

    async def get_messages(self, channel, ids):
        return self.msgs.get(ids)

    async def send_message(self, channel, text, reply_to=None):
        self._next_id += 1
        self.sent.append((text, reply_to))
        return SimpleNamespace(id=self._next_id)

    async def edit_message(self, channel, msg_id, text):
        self.edits[msg_id] = text

    async def delete_messages(self, channel, ids):
        self.deleted.extend(ids)

    async def download_media(self, msg, file, progress_callback=None):
        Path(file).write_bytes(b"x" * msg.file.size)


@pytest.fixture
def env(tmp_path):
    cfg = Settings(
        tg_api_id=1, tg_api_hash="h", tg_bot_token="t", tg_channel_id=-100,
        movies_dir=tmp_path / "movies", tv_dir=tmp_path / "tv", unsorted_dir=tmp_path / "unsorted",
        staging_dir=tmp_path / "staging", state_dir=tmp_path / "state", max_file_size=5000,
        _env_file=None,
    )
    for d in (cfg.staging_dir, cfg.state_dir):
        d.mkdir()
    client = FakeClient()

    async def build():
        store = await Store.open(cfg.state_dir / "jobs.db")
        _open_stores.append(store)
        return Importer(client, "chan", cfg, store, Tmdb(None), Jellyfin("http://x", None))

    return cfg, client, build


_open_stores: list[Store] = []


def run(coro):
    async def wrapped():
        try:
            return await coro
        finally:
            while _open_stores:
                await _open_stores.pop().close()

    return asyncio.run(wrapped())


async def submit_and_process(imp, client, msg):
    client.msgs[msg.id] = msg
    await imp.submit(msg)
    while not imp.queue.empty():
        await imp._process(imp.queue.get_nowait())


def test_movie_imported_original_deleted_and_announced(env):
    cfg, client, build = env

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, "Inception.2010.1080p.mkv"))
        return await imp.store.get(1)

    job = run(go())
    dest = cfg.movies_dir / "Inception (2010)" / "Inception (2010).mkv"
    assert dest.read_bytes() == b"x" * 1000
    assert job["status"] == "done"
    assert client.sent[-1] == ("✅ Inception (2010) movie is uploaded to Jellyfin", None)
    assert 1 in client.deleted  # original post
    assert len(client.deleted) == 2  # + the progress message
    assert not list(cfg.staging_dir.iterdir())


def test_episode_message(env):
    cfg, client, build = env

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, "Breaking.Bad.S01E02.720p.mkv"))

    run(go())
    assert (cfg.tv_dir / "Breaking Bad" / "Season 01" / "Breaking Bad S01E02.mkv").exists()
    assert client.sent[-1][0] == "✅ Breaking Bad S01E02 episode is uploaded to Jellyfin"


def test_too_large_rejected_without_download(env):
    cfg, client, build = env

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, "Big.Movie.2020.mkv", size=6000))
        return await imp.store.get(1)

    job = run(go())
    text, reply_to = client.sent[-1]
    assert text.startswith("❌ `Big.Movie.2020.mkv` is too large") and reply_to == 1
    assert job["status"] == "rejected"
    assert client.deleted == []
    assert not cfg.movies_dir.exists() or not any(cfg.movies_dir.iterdir())


def test_same_document_forwarded_twice_is_duplicate(env):
    cfg, client, build = env

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, "Heat.1995.mkv", doc_id=77))
        await submit_and_process(imp, client, make_msg(2, "Heat.1995.mkv", doc_id=77))
        return await imp.store.get(2)

    job = run(go())
    assert job["status"] == "duplicate"
    assert client.sent[-1][0] == "♻️ `Heat (1995).mkv` is already in Jellyfin"
    assert not (cfg.movies_dir / "Heat (1995)" / "Heat (1995) - v2.mkv").exists()


def test_unsorted_then_fixed_by_caption_edit(env):
    cfg, client, build = env

    async def go():
        imp = await build()
        msg = make_msg(1, "1.mkv")  # nothing guessit can work with
        msg.file.name = None
        await submit_and_process(imp, client, msg)
        unsorted = await imp.store.get(1)
        msg.message = "#movie Heat 1995"
        await imp.caption_edited(msg)
        return unsorted, await imp.store.get(1)

    unsorted, done = run(go())
    assert unsorted["status"] == "unsorted"
    assert not Path(unsorted["dest"]).exists()  # moved out of unsorted
    assert done["status"] == "done"
    assert (cfg.movies_dir / "Heat (1995)" / "Heat (1995).mkv").exists()
    assert client.sent[-1][0] == "✅ Heat (1995) movie is uploaded to Jellyfin"


def test_keep_original_when_delete_disabled(env):
    cfg, client, build = env
    cfg.delete_after_import = False

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, "Inception.2010.mkv"))

    run(go())
    assert client.deleted == []
    assert "✅ Inception (2010) movie is uploaded to Jellyfin" in client.edits.values()
