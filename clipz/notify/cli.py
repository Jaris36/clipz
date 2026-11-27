"""Telegram notifier: python -m clipz.notify [run|once|whoami|test]

  run      (default) loop forever: send alerts about every minute and listen for button taps
  once     one send pass, then exit
  whoami   list chat ids that recently messaged the bot, to fill in TELEGRAM_CHAT_ID (sends nothing)
  test     send one test message to TELEGRAM_CHAT_ID

With TELEGRAM_BOT_TOKEN empty it runs as a dry run and prints messages instead of sending.
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from clipz.notify.notifier import Notifier
from clipz.notify.telegram import DryRunClient, TelegramClient, TelegramError

log = logging.getLogger("clipz.notify")
POLL_SECONDS = 50   # long-poll for button taps; a send pass runs between polls


def build() -> tuple[Notifier, bool]:
    from clipz.pipeline.config import PipelineSettings, load_dotenv
    from clipz.pipeline.schema import connect
    from clipz.settings import Settings

    load_dotenv()
    pipe = PipelineSettings.load()
    scout = Settings.load()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    live = bool(token and chat_id)
    if token and not chat_id:
        log.warning("TELEGRAM_CHAT_ID is empty: run `python -m clipz.notify whoami`. Dry run until then.")
    client = TelegramClient(token) if live else DryRunClient()
    conn = connect(pipe.db_path)
    return Notifier(conn, client, chat_id, scout.scoring.get("alerts", {}),
                    pipe.scoring.get("clip_alerts", {})), live


def whoami() -> int:
    from clipz.pipeline.config import load_dotenv
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env first (from @BotFather).")
        return 1
    # No offset: Telegram keeps these updates, so the notifier still sees them later.
    updates = TelegramClient(token).get_updates(None, timeout=0)
    chats = {}
    for u in updates:
        m = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        c = m.get("chat") or {}
        if "id" in c:
            chats[c["id"]] = c.get("username") or c.get("first_name") or c.get("title") or ""
    if not chats:
        print("No messages yet. Open your bot in Telegram, send it 'hi', then run this again.")
        return 1
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m clipz.notify", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", nargs="?", default="run", choices=["run", "once", "whoami", "test"])
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "whoami":
        return whoami()

    notifier, live = build()
    if not live:
        log.info("dry run: messages are printed, not sent")

    if args.cmd == "test":
        try:
            notifier.client.send_message(notifier.chat_id, "✅ CLIPZ alerts are connected. Nothing else will be sent from this test.")
        except TelegramError as e:
            print(f"Telegram refused the test message: {e}")
            return 1
        print("Test message sent." if live else "Dry run: printed above.")
        return 0

    if args.cmd == "once":
        print(notifier.run_once())
        return 0

    while True:
        try:
            counts = notifier.run_once()
            if any(counts.values()):
                log.info("pass: %s", counts)
        except Exception:
            log.exception("send pass failed; trying again next loop")
        try:
            notifier.handle_updates(timeout=POLL_SECONDS)
        except TelegramError as e:
            log.warning("getUpdates failed: %s", e)
            time.sleep(30)
        except Exception:
            log.exception("handling button taps failed")
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
