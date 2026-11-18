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

Still to come: Telegram alerts (Phase 2), live Whop data (Phase 3), clip bank fed by the
pipeline (Phase 4), approve to post to submit (Phase 5).
