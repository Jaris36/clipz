"""Pipeline settings. Shares .env and config/watchlist.yaml with the scout."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from clipz.settings import ROOT

CONFIG_DIR = Path(os.environ.get("CLIPZ_CONFIG_DIR", ROOT / "config"))
PERMISSION_TYPES = {"campaign", "clipper_program", "direct_deal"}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines; the real environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class PipelineSettings:
    db_path: Path
    data_dir: Path
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    kick_proxy: str = ""
    twitch_downloader_cli: str = "TwitchDownloaderCLI"
    scoring: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "PipelineSettings":
        load_dotenv()
        scoring_file = CONFIG_DIR / "moment_scoring.yaml"
        scoring = yaml.safe_load(scoring_file.read_text()) if scoring_file.exists() else {}
        db_path = Path(os.environ.get("CLIPZ_DB_PATH", ROOT / "data" / "clipz.db"))
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        return cls(
            db_path=db_path,
            data_dir=Path(os.environ.get("CLIPZ_DATA_DIR", db_path.parent)),
            twitch_client_id=os.environ.get("TWITCH_CLIENT_ID", ""),
            twitch_client_secret=os.environ.get("TWITCH_CLIENT_SECRET", ""),
            kick_proxy=os.environ.get("KICK_PROXY", ""),
            twitch_downloader_cli=os.environ.get("TWITCH_DOWNLOADER_CLI", "TwitchDownloaderCLI"),
            scoring=scoring or {},
        )


class ConfigError(ValueError):
    pass


def load_streamers(path: Path | None = None) -> tuple[list[dict], list[str]]:
    """Streamers from config/watchlist.yaml that the pipeline may clip.

    Returns (clippable, skipped_reasons). A streamer is clipped only when it has a
    permission record: permission.type and permission.proof (a link).
    """
    path = path or CONFIG_DIR / "watchlist.yaml"
    if not path.exists():
        raise ConfigError(f"{path} not found")
    doc = yaml.safe_load(path.read_text()) or {}
    clippable, skipped = [], []
    for s in doc.get("streamers") or []:
        name = s.get("name") or "?"
        if s.get("platform") not in {"twitch", "kick"}:
            skipped.append(f"{name}: platform {s.get('platform')!r} is not clipped yet")
            continue
        perm = s.get("permission") or {}
        if perm.get("type") not in PERMISSION_TYPES or not perm.get("proof"):
            skipped.append(f"{name}: no permission record (permission.type and permission.proof)")
            continue
        if not s.get("niche"):
            skipped.append(f"{name}: no niche")
            continue
        clippable.append(s)
    return clippable, skipped


def sync_streamers(conn: sqlite3.Connection, streamers: list[dict]) -> int:
    """Upsert streamers; any no longer listed (or lacking permission) are deactivated."""
    seen = set()
    for s in streamers:
        key = (s["platform"], s["name"].lower())
        seen.add(key)
        conn.execute(
            """INSERT INTO streamers (platform, channel, niche, permission_type, permission_proof, active)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (platform, channel) DO UPDATE SET
                 niche = excluded.niche, permission_type = excluded.permission_type,
                 permission_proof = excluded.permission_proof, active = excluded.active""",
            (key[0], key[1], s["niche"], s["permission"]["type"], s["permission"]["proof"],
             1 if s.get("active", True) else 0),
        )
    for row in conn.execute("SELECT id, platform, channel FROM streamers WHERE active = 1").fetchall():
        if (row["platform"], row["channel"]) not in seen:
            conn.execute("UPDATE streamers SET active = 0 WHERE id = ?", (row["id"],))
    conn.commit()
    return len(seen)
