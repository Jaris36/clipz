"""Clipping pipeline command line: python -m clipz.pipeline <command>."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from clipz.pipeline.config import ConfigError, PipelineSettings as Settings, load_streamers, sync_streamers
from clipz.pipeline.schema import connect


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clipz-pipeline", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database and load clippable streamers from config/watchlist.yaml")

    s = sub.add_parser("smoke", help="check this server can reach Kick and Twitch")
    s.add_argument("--kick", required=True, help="a Kick channel slug to test with")
    s.add_argument("--twitch", required=True, help="a Twitch login to test with")

    a = sub.add_parser("analyze", help="rank moments from saved files (no network)")
    a.add_argument("--duration", type=float, required=True, help="VOD length in seconds")
    a.add_argument("--twitch-chat", type=Path, help="TwitchDownloader chat JSON")
    a.add_argument("--kick-chat", type=Path, help="recorded Kick chat JSONL")
    a.add_argument("--stream-start", type=float, help="Kick stream start, unix seconds")
    a.add_argument("--clips", type=Path, help="JSON list of {offset, duration, views}")
    a.add_argument("--audio", type=Path, help="audio file for loudness scoring")
    a.add_argument("--json", action="store_true", help="print JSON instead of a table")

    sub.add_parser("watch", help="one pass: find new VODs for every source and rank moments")
    sub.add_parser("record-kick-chat", help="run forever, recording Kick chat while streamers are live")

    m = sub.add_parser("moments", help="show ranked moments stored for recent VODs")
    m.add_argument("--limit", type=int, default=3)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.load()

    if args.cmd == "init":
        conn = connect(settings.db_path)
        try:
            streamers, skipped = load_streamers()
        except ConfigError as e:
            print(f"Database ready at {settings.db_path}. Streamers not loaded: {e}")
            return 1
        n = sync_streamers(conn, streamers)
        print(f"Database ready at {settings.db_path}; {n} streamers will be clipped.")
        for reason in skipped:
            print(f"  skipped {reason}")
        return 0

    if args.cmd == "smoke":
        from clipz.pipeline.smoke import run
        results = run(settings, args.kick, args.twitch)
        for r in results:
            print(r)
        return 1 if any(r.status == "FAIL" for r in results) else 0

    if args.cmd == "analyze":
        from clipz.pipeline.run import analyze, moments_json, moments_report
        rows = None
        if args.twitch_chat:
            from clipz.pipeline.fetch.chat import load_twitch_chat
            rows = load_twitch_chat(args.twitch_chat)
        elif args.kick_chat:
            from clipz.pipeline.sources.kick_chat_recorder import load_kick_chat
            if args.stream_start is None:
                p.error("--kick-chat needs --stream-start")
            rows = load_kick_chat(args.kick_chat, args.stream_start)
        clips = None
        if args.clips:
            clips = [(c["offset"], c.get("duration", 30), c.get("views", 0)) for c in json.loads(args.clips.read_text())]
        loud = None
        if args.audio:
            from clipz.pipeline.signals.audio import momentary_loudness
            loud = momentary_loudness(args.audio)
        moments = analyze(args.duration, settings.scoring, rows, clips, loud)
        print(moments_json(moments) if args.json else moments_report(moments))
        return 0

    conn = connect(settings.db_path)

    if args.cmd == "watch":
        from clipz.pipeline.run import watch_once
        print(json.dumps(watch_once(conn, settings), indent=2))
        return 0

    if args.cmd == "record-kick-chat":
        import asyncio
        from clipz.pipeline.recorder import run
        asyncio.run(run(settings))
        return 0

    if args.cmd == "moments":
        vods = conn.execute(
            """SELECT v.id, v.title, v.url, s.channel FROM vods v JOIN streamers s ON s.id = v.streamer_id
               WHERE v.status = 'ranked' ORDER BY v.processed_at DESC LIMIT ?""", (args.limit,)).fetchall()
        for v in vods:
            print(f"\n{v['channel']}: {v['title']}  {v['url']}")
            for m in conn.execute("SELECT * FROM moments WHERE vod_id = ? ORDER BY score DESC", (v["id"],)):
                print(f"  {int(m['start_s']):>6}s-{int(m['end_s']):>6}s  score {m['score']:.2f}  {m['moment_type']}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
