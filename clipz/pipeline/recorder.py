"""Always-on service: record Kick chat for every approved Kick streamer while live."""
from __future__ import annotations

import asyncio
import logging

from clipz.pipeline.config import PipelineSettings as Settings
from clipz.pipeline.schema import connect
from clipz.pipeline.run import kick_chat_path
from clipz.pipeline.sources.kick import KickAPI, KickBlocked
from clipz.pipeline.sources.kick_chat_recorder import record

log = logging.getLogger(__name__)


async def run(settings: Settings, poll_seconds: int = 60) -> None:
    conn = connect(settings.db_path)
    api = KickAPI(settings.kick_proxy)
    active: dict[str, tuple[asyncio.Task, asyncio.Event, int]] = {}
    while True:
        slugs = [r["channel"] for r in conn.execute(
            "SELECT channel FROM streamers WHERE platform = 'kick' AND active = 1")]
        for slug in slugs:
            try:
                ch = await asyncio.to_thread(api.channel, slug)
            except KickBlocked as e:
                log.error("Kick blocked the lookup for %s: %s (set KICK_PROXY)", slug, e)
                continue
            except Exception as e:
                log.warning("Kick lookup failed for %s: %s", slug, e)
                continue
            running = active.get(slug)
            if ch.is_live and ch.chatroom_id and ch.livestream_id:
                if running and running[2] == ch.livestream_id and not running[0].done():
                    continue
                if running:
                    running[1].set()
                stop = asyncio.Event()
                path = kick_chat_path(settings, slug, ch.livestream_id)
                log.info("%s is live; recording chat to %s", slug, path)
                task = asyncio.create_task(record(ch.chatroom_id, path, stop))
                active[slug] = (task, stop, ch.livestream_id)
            elif running:
                log.info("%s went offline; stopping chat recorder", slug)
                running[1].set()
                active.pop(slug)
        await asyncio.sleep(poll_seconds)
