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

    r = sub.add_parser("render", help="render the best candidate clips through OpenShorts")
    r.add_argument("--limit", type=int, help="clips to render this pass (default: render.yaml)")

    rf = sub.add_parser("render-file", help="render one local video file (for testing)")
    rf.add_argument("video", type=Path)
    rf.add_argument("--gambling", action="store_true", help="apply the facecam crop and casino-text check")
    rf.add_argument("--facecam", help="facecam box as fractions x,y,w,h (default: detect the face)")
    rf.add_argument("--out", type=Path, default=Path("data/samples"))

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
        try:
            n = sync_streamers(conn, streamers)
        except ConfigError as e:
            print(f"Database ready at {settings.db_path}. Streamers not loaded: {e}")
            return 1
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

    if args.cmd == "render":
        from clipz.pipeline.render.stage import render_pending
        outcomes = render_pending(conn, settings, args.limit)
        for o in outcomes:
            print(f"{o.clip_id}: {o.state} ({o.detail})")
        if not outcomes:
            print("No candidate clips to render.")
        return 0

    if args.cmd == "render-file":
        return _render_file(args, settings)

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


def _render_file(args, settings) -> int:
    from clipz.pipeline.render import facecam as fc
    from clipz.pipeline.render.guards import casino_text_hits, integrated_loudness, is_music_heavy
    from clipz.pipeline.render.openshorts import OpenShortsClient

    cfg = settings.render
    args.out.mkdir(parents=True, exist_ok=True)
    source = args.video
    if args.gambling:
        box = None
        if args.facecam:
            x, y, w, h = (float(v) for v in args.facecam.split(","))
            box = {"x": x, "y": y, "w": w, "h": h}
        source, found = fc.facecam_only(args.video, args.out / f"{args.video.stem}-facecam.mp4", box)
        print(f"Cropped to face camera {found}")
    client = OpenShortsClient(settings.openshorts_url, settings.gemini_api_key or None)
    job = client.submit(source, int(cfg.get("clip_min_seconds", 15)), int(cfg.get("clip_max_seconds", 60)))
    print(f"OpenShorts job {job} started")
    clips = client.wait(job, float(cfg.get("job_timeout_seconds", 1800)))
    if not clips:
        print("OpenShorts returned no clip.")
        return 1
    out = client.download(clips[0], args.out / f"{args.video.stem}-vertical.mp4")
    words = client.transcript_words(job, 0)
    dur = (clips[0].end - clips[0].start) if clips[0].start is not None and clips[0].end is not None else 0
    print(f"Saved {out}\nHook: {clips[0].hook}\nMusic flag: {is_music_heavy(words, dur, integrated_loudness(out), cfg) if words else 'no transcript'}")
    if args.gambling:
        caption = " ".join(str(w.get('word', '')) for w in words)
        hits = casino_text_hits(out, clips[0].hook, caption, cfg)
        print("Casino text: " + ("; ".join(f"{h.where}: {h.text!r}" for h in hits) if hits else "none found"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
