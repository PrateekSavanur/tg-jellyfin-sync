"""Pipeline tests against an in-memory fake of the Telethon client."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.jellyfin import Jellyfin
from app.store import Store
from app.tmdb import Tmdb
from app.worker import Importer
from tests.test_jellyfin import FakeJellyfin, by_name
from tests.test_music import write_mp3


def make_msg(msg_id, name, size=1000, caption="", doc_id=None, mime="video/x-matroska", content=None,
             title=None, performer=None, voice=None):
    if content is not None:
        size = len(content)
    file = SimpleNamespace(name=name, size=size, mime_type=mime, ext=Path(name or "").suffix or ".mkv",
                           title=title, performer=performer)
    return SimpleNamespace(id=msg_id, document=SimpleNamespace(id=doc_id or msg_id), file=file, message=caption,
                           voice=voice, content=content)


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
        Path(file).write_bytes(msg.content if msg.content is not None else b"x" * msg.file.size)


@pytest.fixture
def env(tmp_path):
    cfg = Settings(
        tg_api_id=1, tg_api_hash="h", tg_bot_token="t", tg_channel_id=-100,
        movies_dir=tmp_path / "movies", tv_dir=tmp_path / "tv", music_dir=tmp_path / "music",
        unsorted_dir=tmp_path / "unsorted",
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


def tagged_mp3(tmp_path, **tags) -> bytes:
    return write_mp3(tmp_path / "upload.mp3", **tags).read_bytes()


def test_song_filed_by_tags(env, tmp_path):
    cfg, client, build = env
    content = tagged_mp3(tmp_path, title="Lag Jaa Gale", artist="Lata Mangeshkar", album="Woh Kaun Thi?",
                         tracknumber="2")

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, "lag jaa gale.mp3", mime="audio/mpeg", content=content))
        return await imp.store.get(1)

    job = run(go())
    dest = cfg.music_dir / "Lata Mangeshkar" / "Woh Kaun Thi" / "02 - Lag Jaa Gale.mp3"
    assert dest.read_bytes() == content
    assert job["status"] == "done" and job["dest"] == str(dest)
    assert client.sent[-1] == ("✅ Lata Mangeshkar – Lag Jaa Gale song is uploaded to Jellyfin", None)
    assert 1 in client.deleted
    assert not list(cfg.staging_dir.iterdir())
    assert not cfg.movies_dir.exists() or not any(cfg.movies_dir.iterdir())


def test_untagged_song_uses_telegram_metadata(env, tmp_path):
    cfg, client, build = env
    msg = make_msg(1, "AUD-001.mp3", mime="audio/mpeg", content=tagged_mp3(tmp_path),
                   title="Chura Liya Hai Tumne", performer="Asha Bhosle")

    async def go():
        imp = await build()
        await submit_and_process(imp, client, msg)

    run(go())
    assert (cfg.music_dir / "Asha Bhosle" / "Singles" / "Chura Liya Hai Tumne.mp3").exists()


def test_same_song_twice_is_duplicate(env, tmp_path):
    cfg, client, build = env
    content = tagged_mp3(tmp_path, title="Song", artist="Artist")

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, "a.mp3", mime="audio/mpeg", content=content, doc_id=9))
        await submit_and_process(imp, client, make_msg(2, "a.mp3", mime="audio/mpeg", content=content, doc_id=9))
        return await imp.store.get(2)

    assert run(go())["status"] == "duplicate"
    assert client.sent[-1][0] == "♻️ `Song.mp3` is already in Jellyfin"


def test_voice_note_skipped(env):
    cfg, client, build = env

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, None, mime="audio/ogg", voice=object()))
        return await imp.store.get(1)

    assert run(go()) is None
    assert client.sent[-1][0].startswith("⚠️ Skipped")


def test_song_queued_then_added_once_jellyfin_indexes_it(env, tmp_path):
    cfg, client, build = env
    cfg.default_playlist = "Old Songs"
    fake = FakeJellyfin(index_after_polls=3)  # Jellyfin is busy: the first few lookups don't see it
    content = tagged_mp3(tmp_path, title="Song", artist="Artist", album="Album")

    async def go():
        imp = await build()
        imp.jellyfin = Jellyfin("http://jf", "key", transport=httpx.MockTransport(fake.handler))
        fake.upload(cfg.music_dir / "Artist" / "Album" / "Song.mp3", "song-1")
        await submit_and_process(imp, client, make_msg(1, "a.mp3", mime="audio/mpeg", content=content,
                                                       caption="#playlist Evening"))
        for _ in range(3):
            await imp.sync_playlists()
        still_queued = len(await imp.store.queued_playlists())
        await imp.sync_playlists()
        left = await imp.store.queued_playlists()
        await imp.jellyfin.aclose()
        return still_queued, left

    still_queued, left = run(go())
    assert client.sent[-1][0] == "✅ Artist – Song song is uploaded to Jellyfin · playlist: Evening"
    assert still_queued == 1 and left == []
    assert by_name(fake) == {"Evening": ["song-1"]}


def test_never_indexed_song_dropped_after_timeout(env, tmp_path):
    cfg, client, build = env
    cfg.playlist_timeout = 0
    fake = FakeJellyfin()

    async def go():
        imp = await build()
        imp.jellyfin = Jellyfin("http://jf", "key", transport=httpx.MockTransport(fake.handler))
        await imp.store.queue_playlists("/data/music/x.mp3", ["Old Songs"], "Artist – X")
        await imp.sync_playlists()
        left = await imp.store.queued_playlists()
        await imp.jellyfin.aclose()
        return left

    assert run(go()) == []
    assert fake.playlists == {}


def test_no_playlist_without_jellyfin(env, tmp_path):
    cfg, client, build = env
    cfg.default_playlist = "Old Songs"

    async def go():
        imp = await build()
        await submit_and_process(imp, client, make_msg(1, "a.mp3", mime="audio/mpeg",
                                                       content=tagged_mp3(tmp_path, title="Song", artist="Artist")))

    run(go())
    assert client.sent[-1][0] == "✅ Artist – Song song is uploaded to Jellyfin"
