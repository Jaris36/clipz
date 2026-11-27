# clipz

Clips streamers, finds paid clipping campaigns, and posts with a human tap.
Design: "11-2026-campaign-scout-design" (https://claude.ai/artifact/GcjsUPwdnGUAUT98CT75Ku).

## Campaign scout (Phase 1: offline core)

The scout checks for new clipping campaigns every 10 minutes, reads each brief into a
rules checklist, filters and scores it, matches ready clips from the clip bank, and
queues an alert. It never joins, posts or submits on its own.

```
pip install -e ".[dev]"
pytest                                   # runs on sample data, no accounts needed
python -m clipz.scout.runner --once      # one poll (manual campaigns only until WHOP_TOKEN is set)
```

Settings: copy `.env.example` to `.env` on the server. Tune `config/scoring.yaml`
and list streamers in `config/watchlist.yaml` (GTA and gambling Kick are tier 1).

| File | What it does |
| --- | --- |
| `clipz/db.py` | SQLite schema shared with the clipping pipeline |
| `clipz/scout/sources/whop_bounties.py` | Whop official API, `GET /bounties` |
| `clipz/scout/sources/manual.py` | Campaigns added by hand |
| `clipz/scout/rules_reader.py` | Claude Haiku turns a brief into a checklist; keyword fallback |
| `clipz/scout/scorer.py` | Hard filters and the 0-100 score |
| `clipz/scout/matcher.py` | Picks the best 3 banked clips |
| `clipz/scout/runner.py` | The 10-minute loop |

## Clipping pipeline (Phase 1: moment finder)

Design: "11-2026-clipz-pipeline-design" (https://claude.ai/artifact/7jpPZiH77hzoH5UHzsJXpu).
Finds new Twitch and Kick VODs for approved streamers, scores every 10-second window
from viewer-made clips, chat spikes (with chat lag removed) and optional audio loudness,
and stores the top moments as `candidate` clips. Nothing is rendered or posted yet.

```
python -m clipz.pipeline smoke --kick <slug> --twitch <login>   # day one on the VPS
python -m clipz.pipeline init          # DB + clippable streamers from config/watchlist.yaml
python -m clipz.pipeline watch         # one pass (the watcher service runs it hourly)
python -m clipz.pipeline moments       # ranked moments for recent VODs
python -m clipz.pipeline record-kick-chat   # always-on; Kick has no chat replay download
python -m clipz.pipeline analyze --duration 3600 --twitch-chat chat.json   # offline, no network
docker compose up -d --build           # on the VPS
```

Only streamers with a `permission` record in `config/watchlist.yaml` are clipped.
Moment scoring weights live in `config/moment_scoring.yaml`.

| File | What it does |
| --- | --- |
| `clipz/pipeline/schema.py` | Pipeline tables; extends the shared `clips` table |
| `clipz/pipeline/sources/twitch.py` | Twitch Helix: VODs and viewer clips with VOD offsets |
| `clipz/pipeline/sources/kick.py` | Kick channel, VOD and clip lookups (proxy-ready) |
| `clipz/pipeline/sources/kick_chat_recorder.py` | Records Kick chat live, read-only |
| `clipz/pipeline/fetch/` | Twitch chat download, VOD segment and audio download |
| `clipz/pipeline/signals/` | Chat spikes, reaction tokens, viewer clips, loudness |
| `clipz/pipeline/rank.py` | Merges signals and picks moments |
| `clipz/pipeline/run.py` | Watch pass: VODs to ranked moments in the database |
| `clipz/pipeline/smoke.py` | Day-one reachability test for Kick and Twitch |

Still to come: Telegram alerts (Phase 2), live Whop data (Phase 3), clip bank fed by the
pipeline (Phase 4), approve to post to submit (Phase 5).
