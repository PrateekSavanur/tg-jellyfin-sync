"""The import pipeline for one channel message: validate → name → download → place → announce."""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.custom import Message

from . import naming
from .config import Settings
from .jellyfin import Jellyfin
from .naming import MediaInfo
from .notify import alert
from .store import Store
from .tmdb import Tmdb

log = logging.getLogger(__name__)


def gb(size: int) -> str:
    return f"{size / 1024**3:.2f} GB"


class Importer:
    def __init__(self, client: TelegramClient, channel, cfg: Settings, store: Store, tmdb: Tmdb, jellyfin: Jellyfin):
        self.client, self.channel, self.cfg = client, channel, cfg
        self.store, self.tmdb, self.jellyfin = store, tmdb, jellyfin
        self.queue: asyncio.Queue[int] = asyncio.Queue()

    # ── Intake ────────────────────────────────────────────────────

    async def submit(self, msg: Message) -> None:
        """Called for every new channel post (live or catch-up)."""
        await self.store.bump_last_seen(msg.id)
        if not msg.document:
            return  # text, photos, and the bot's own status posts
        if not naming.is_supported(msg.file.name, msg.file.mime_type):
            await self._reply(msg.id, f"⚠️ Skipped `{msg.file.name or 'file'}`: not a video or subtitle file.")
            return
        if await self.store.add(msg.id, msg.document.id, msg.file.name, msg.file.size):
            self.queue.put_nowait(msg.id)

    async def caption_edited(self, msg: Message) -> None:
        """Lets an unidentified file be filed by editing its caption to `#movie …` / `#tv …`."""
        job = await self.store.get(msg.id)
        if not job or job["status"] != "unsorted" or not (info := naming.parse_caption(msg.message)):
            return
        info = await self.tmdb.enrich(info)
        src = Path(job["dest"])
        try:
            await self._file(msg, job["status_msg"], src, info, src.suffix.lower())
        except Exception as e:
            log.exception("Re-filing message %s failed", msg.id)
            await self._fail(msg.id, e, keep_status="unsorted")

    async def run_workers(self) -> None:
        for msg_id in await self.store.pending():
            self.queue.put_nowait(msg_id)
        await asyncio.gather(*(self._worker() for _ in range(self.cfg.concurrency)))

    async def _worker(self) -> None:
        while True:
            msg_id = await self.queue.get()
            try:
                await self._process(msg_id)
            except Exception as e:
                log.exception("Import of message %s failed", msg_id)
                await self._fail(msg_id, e)
            finally:
                self.queue.task_done()

    # ── Pipeline ──────────────────────────────────────────────────

    async def _process(self, msg_id: int) -> None:
        # Re-fetch so restarts work and file references are fresh
        msg = await self.client.get_messages(self.channel, ids=msg_id)
        if msg is None or not msg.document:
            await self.store.set(msg_id, "failed", error="message was deleted")
            return
        name = msg.file.name or f"video_{msg_id}{msg.file.ext or '.mp4'}"
        size = msg.file.size

        if size > self.cfg.max_file_size:
            await self._reply(
                msg_id,
                f"❌ `{name}` is too large ({gb(size)}). The maximum is "
                f"{gb(self.cfg.max_file_size)}, so it was not uploaded to Jellyfin.",
            )
            await self.store.set(msg_id, "rejected", error=f"too large: {size} bytes")
            return

        if prior := await self.store.imported_dest(msg.document.id, msg_id):
            await self.store.set(msg_id, "duplicate", dest=prior)
            await self._finish(msg, None, f"♻️ `{Path(prior).name}` is already in Jellyfin")
            return

        info = naming.resolve(msg.file.name, msg.message)
        if info:
            info = await self.tmdb.enrich(info)
        ext = (Path(name).suffix or msg.file.ext or ".mp4").lower()

        free = shutil.disk_usage(self.cfg.staging_dir).free
        if free < size * 1.1:
            raise RuntimeError(f"not enough disk space ({gb(free)} free, need {gb(size)})")

        status = await self._reply(msg_id, f"⬇️ Downloading `{name}` ({gb(size)})…")
        await self.store.set_status_msg(msg_id, status.id)
        await self.store.set(msg_id, "downloading")

        part = self.cfg.staging_dir / f"{msg_id}.part"
        try:
            await self.client.download_media(msg, file=str(part), progress_callback=self._progress(status.id, name))
            if info is None:
                dest = _free_path(self.cfg.unsorted_dir / naming.sanitize(name))
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(part, dest)
                await self.store.set(msg_id, "unsorted", dest=str(dest))
                await self._edit(
                    status.id,
                    f"🤔 Couldn't identify `{name}`, so it was saved to unsorted.\n"
                    "Edit the caption of the original post to `#movie Title 2010` or "
                    "`#tv Show S01E02` and I'll move it into the library.",
                )
                return
            await self._file(msg, status.id, part, info, ext)
        finally:
            part.unlink(missing_ok=True)

    async def _file(self, msg: Message, status_id: int | None, src: Path, info: MediaInfo, ext: str) -> None:
        """Moves a downloaded file into its library folder and announces it."""
        dest = naming.build_path(info, ext, self.cfg.movies_dir, self.cfg.tv_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            src.unlink()  # identical file already in place
        else:
            dest = _free_path(dest)
            os.replace(src, dest)  # same filesystem → atomic, Jellyfin never sees a partial file
            dest.chmod(0o644)

        await self.jellyfin.notify_created(dest)
        await self.store.set(msg.id, "done", dest=str(dest))
        kind = "subtitle" if ext in naming.SUBTITLE_EXTS else info.kind
        log.info("Imported message %s → %s", msg.id, dest)
        await self._finish(msg, status_id, f"✅ {info.label} {kind} is uploaded to Jellyfin")

    async def _finish(self, msg: Message, status_id: int | None, text: str) -> None:
        """Posts the final message; with delete_after_import, removes the original and progress posts."""
        if not self.cfg.delete_after_import:
            await (self._edit(status_id, text) if status_id else self._reply(msg.id, text))
            return
        await self.client.send_message(self.channel, text)
        try:
            await self.client.delete_messages(self.channel, [msg.id] + ([status_id] if status_id else []))
        except RPCError as e:
            log.warning("Could not delete message %s (is the bot allowed to delete?): %s", msg.id, e)

    async def _fail(self, msg_id: int, error: Exception, keep_status: str = "failed") -> None:
        job = await self.store.get(msg_id)
        await self.store.set(msg_id, keep_status, error=str(error))
        name = (job["filename"] if job else None) or f"message {msg_id}"
        text = f"❌ Failed to import `{name}`: {error}"
        if job and job["status_msg"]:
            await self._edit(job["status_msg"], text)
        else:
            try:
                await self._reply(msg_id, text)
            except RPCError as e:
                log.warning("Could not report failure of message %s: %s", msg_id, e)
        await alert(self.cfg.homelert_webhook_url, "Jellyfin import failed", f"{name}: {error}")

    # ── Telegram helpers ──────────────────────────────────────────

    def _progress(self, status_id: int, name: str):
        start = last = time.monotonic()

        async def cb(done: int, total: int) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last < self.cfg.progress_interval:
                return
            last = now
            speed = done / (now - start) / 1024**2
            await self._edit(status_id, f"⬇️ Downloading `{name}`: {done * 100 // total}% · {speed:.1f} MB/s")

        return cb

    async def _reply(self, msg_id: int, text: str) -> Message:
        return await self.client.send_message(self.channel, text, reply_to=msg_id)

    async def _edit(self, status_id: int, text: str) -> None:
        try:
            await self.client.edit_message(self.channel, status_id, text)
        except RPCError as e:
            # Progress edits are cosmetic; never let them fail an import
            log.debug("Edit of status %s failed: %s", status_id, e)


def _free_path(path: Path) -> Path:
    """`path`, or `name - v2.ext`, `- v3`… if taken (Jellyfin groups these as versions)."""
    if not path.exists():
        return path
    n = 2
    while (candidate := path.with_name(f"{path.stem} - v{n}{path.suffix}")).exists():
        n += 1
    return candidate
