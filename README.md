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

### Phase 2: rendering

`python -m clipz.pipeline render` takes the best `candidate` clips, downloads just that
moment, and sends it to a self-hosted OpenShorts (pinned in `docker-compose.yml`) for the
9:16 reframe, burned-in captions and a hook. Results become `rendered` clips with a file,
hook and caption, ready for review. Gambling streamers (`gambling_niches` in
`config/render.yaml`) are cropped to their face camera first, and a clip is rejected if a
casino name, web address or promo code shows up on screen (OCR), in the hook or in the
captions. Loud, nearly wordless clips get `music_flag = 1`. Test on one file with
`python -m clipz.pipeline render-file video.mp4 [--gambling]`.

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
| `clipz/pipeline/render/openshorts.py` | Client for the self-hosted OpenShorts API |
| `clipz/pipeline/render/facecam.py` | Finds the face camera and crops gambling clips to it |
| `clipz/pipeline/render/guards.py` | Casino-text check (OCR) and the music flag |
| `clipz/pipeline/render/stage.py` | Render pass: candidate to rendered or rejected |

## Telegram alerts (Phase 2)

`python -m clipz.notify` sends what the scout and pipeline find to your private Telegram chat:

| Alert | When |
| --- | --- |
| Campaign, instant | Score 75+ (the scout holds these overnight unless 90+) |
| Campaign digest | Scores 50 to 74, batched at 9 AM and 6 PM (`digest_hours` in `config/scoring.yaml`) |
| Budget warning | A campaign you joined is closing or under 15% budget (held during quiet hours) |
| Strong moment | A ranked moment scores at least `clip_alerts.min_score` in `config/moment_scoring.yaml` |

Buttons only record your choice (table `alert_choices`). **I joined** marks the campaign
joined so budget warnings start, **Skip** marks it skipped, **Interested** just notes it.
Nothing joins, posts or submits from Telegram. You join campaigns on Whop yourself.
Taps from any chat other than `TELEGRAM_CHAT_ID` are ignored. Each alert is sent at most once.

Setup, about 2 minutes, once the server exists:
1. In Telegram, message **@BotFather**, send `/newbot`, pick a name. Put the token it gives you in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Open your new bot and send it "hi". Run `python -m clipz.notify whoami` and copy the `TELEGRAM_CHAT_ID=...` line into `.env`.
3. `python -m clipz.notify test` sends one test message. Then `docker compose up -d notifier`.

With the token or chat id empty it runs as a dry run and prints alerts instead of sending.

```
python -m clipz.notify once      # one send pass
python -m clipz.notify           # loop: sends about every minute, listens for button taps
```

| File | What it does |
| --- | --- |
| `clipz/notify/telegram.py` | Bot API client (sendMessage, getUpdates, button edits) and a dry-run client |
| `clipz/notify/messages.py` | Alert text and buttons |
| `clipz/notify/notifier.py` | Send passes, digest timing, quiet hours, button taps |
| `clipz/notify/schema.py` | `clip_alerts`, `alert_choices`, `notify_state` tables |

Still to come: live Whop data (Phase 3), clip bank fed by the pipeline (Phase 4),
approve to post to submit (Phase 5).
