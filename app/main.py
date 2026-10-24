import asyncio
import logging
import signal

from telethon import TelegramClient, events

from .config import Settings
from .jellyfin import Jellyfin
from .store import Store
from .tmdb import Tmdb
from .worker import Importer

log = logging.getLogger("tg-jellyfin-sync")

CATCH_UP_BATCH = 100


async def catch_up(client: TelegramClient, channel, store: Store, importer: Importer) -> None:
    """Imports posts made while we were down. Bots can't read history, but can fetch by id."""
    last = await store.last_seen()
    if last is None:
        log.info("First run: only new posts will be imported")
        return
    next_id = last + 1
    while True:
        ids = list(range(next_id, next_id + CATCH_UP_BATCH))
        msgs = await client.get_messages(channel, ids=ids)
        found = [m for m in msgs if m is not None]
        if not found:
            break
        for msg in found:
            await importer.submit(msg)
        next_id += CATCH_UP_BATCH
    log.info("Caught up to message %s", await store.last_seen())


async def main() -> None:
    cfg = Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    for d in (cfg.state_dir, cfg.staging_dir, cfg.unsorted_dir, cfg.movies_dir, cfg.tv_dir, cfg.music_dir):
        d.mkdir(parents=True, exist_ok=True)

    store = await Store.open(cfg.state_dir / "jobs.db")
    tmdb, jellyfin = Tmdb(cfg.tmdb_api_key), Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key)
    client = TelegramClient(str(cfg.state_dir / "bot"), cfg.tg_api_id, cfg.tg_api_hash)
    await client.start(bot_token=cfg.tg_bot_token)
    channel = await client.get_input_entity(cfg.tg_channel_id)
    importer = Importer(client, channel, cfg, store, tmdb, jellyfin)

    # Register live handlers before catching up so nothing slips through the gap;
    # store.add() dedups anything seen twice.
    @client.on(events.NewMessage(chats=cfg.tg_channel_id))
    async def on_post(event: events.NewMessage.Event) -> None:
        await importer.submit(event.message)

    @client.on(events.MessageEdited(chats=cfg.tg_channel_id))
    async def on_edit(event: events.MessageEdited.Event) -> None:
        await importer.caption_edited(event.message)

    await catch_up(client, channel, store, importer)
    log.info("Watching channel %s (TMDB %s, Jellyfin refresh %s)", cfg.tg_channel_id,
             "on" if tmdb.enabled else "off", "on" if jellyfin.enabled else "off")

    workers = asyncio.create_task(importer.run_workers())
    # As PID 1 in a container, Python would otherwise ignore SIGTERM until the kill timeout
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, client.disconnect)
    try:
        await client.run_until_disconnected()
    finally:
        workers.cancel()
        await asyncio.gather(tmdb.aclose(), jellyfin.aclose(), store.close())


if __name__ == "__main__":
    asyncio.run(main())
