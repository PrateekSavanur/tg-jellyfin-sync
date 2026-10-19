import logging

import httpx

log = logging.getLogger(__name__)


async def alert(webhook_url: str | None, title: str, body: str, severity: str = "warning") -> None:
    """Sends an event to Homelert's generic webhook; routing to Telegram is up to its rules."""
    if not webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook_url, json={"title": title, "body": body, "severity": severity})
    except httpx.HTTPError as e:
        log.warning("Homelert alert failed: %s", e)
