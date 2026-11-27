"""Turns database rows into Telegram messages (HTML parse mode) and their buttons."""
from __future__ import annotations

import json
from html import escape

from clipz.notify.telegram import Buttons

MAX_DIGEST_ITEMS = 10


def _money(x) -> str:
    if x is None:
        return "?"
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def pay_line(c: dict) -> str:
    if c.get("pay_per_1k_views") is not None:
        return f"{_money(c['pay_per_1k_views'])} per 1K views"
    if c.get("pay_per_submission") is not None:
        return f"{_money(c['pay_per_submission'])} per accepted clip"
    return "pay not stated"


def budget_line(c: dict) -> str:
    total, paid = c.get("budget_total"), c.get("budget_paid_out")
    if total is None:
        return "budget not stated"
    left = max(0.0, total - (paid or 0))
    pct = 100 * left / total if total else 0
    return f"{_money(left)} of {_money(total)} left ({pct:.0f}%)"


def campaign_buttons(ref: int, prefix: str = "") -> list[tuple[str, str]]:
    """ref is the campaign's SQLite rowid: Telegram caps callback data at 64 bytes."""
    return [(f"{prefix}I joined", f"c:joined:{ref}"),
            (f"{prefix}Interested", f"c:interested:{ref}"),
            (f"{prefix}Skip", f"c:skip:{ref}")]


def clip_buttons(clip_alert_id: int) -> list[tuple[str, str]]:
    return [("Interested", f"m:interested:{clip_alert_id}"), ("Skip", f"m:skip:{clip_alert_id}")]


def _clip_lines(clips: list[dict]) -> list[str]:
    if not clips:
        return ["No ready clips in the bank yet."]
    out = ["Ready clips:"]
    for cl in clips:
        out.append(f"• {escape(cl.get('hook_title') or cl['id'])} ({cl.get('length_seconds', 0):.0f}s, "
                   f"score {cl.get('moment_score', 0):.2f})")
    return out


def campaign_alert(c: dict, reasons: list[str], clips: list[dict]) -> tuple[str, Buttons]:
    lines = [f"🔥 <b>{escape(c['title'])}</b>  score {c.get('score') or 0:.0f}/100",
             f"{pay_line(c)} · {budget_line(c)}"]
    if c.get("streamer"):
        lines.append(f"Streamer: {escape(c['streamer'])}")
    if c.get("deadline"):
        lines.append(f"Deadline: {escape(str(c['deadline'])[:10])}")
    if reasons:
        lines.append("Why: " + escape("; ".join(reasons)))
    lines += _clip_lines(clips)
    if c.get("link"):
        lines.append(f'<a href="{escape(c["link"], quote=True)}">Open campaign</a> (join it yourself there)')
    return "\n".join(lines), [campaign_buttons(c["ref"])]


def digest(items: list[tuple[dict, list[str], list[dict]]]) -> tuple[str, Buttons]:
    lines = [f"📋 <b>Campaign digest</b> ({len(items)})"]
    buttons: Buttons = []
    for i, (c, reasons, clips) in enumerate(items[:MAX_DIGEST_ITEMS], 1):
        link = f' · <a href="{escape(c["link"], quote=True)}">open</a>' if c.get("link") else ""
        lines.append(f"\n<b>{i}. {escape(c['title'])}</b>  {c.get('score') or 0:.0f}/100")
        lines.append(f"{pay_line(c)} · {budget_line(c)} · {len(clips)} clips ready{link}")
        if reasons:
            lines.append(escape("; ".join(reasons)))
        buttons.append(campaign_buttons(c["ref"], prefix=f"{i}. "))
    return "\n".join(lines), buttons


def budget_warning(c: dict) -> tuple[str, Buttons]:
    state = "closed" if not c.get("status_open", 1) else "running low"
    lines = [f"⚠️ <b>Budget {state}</b>: {escape(c['title'])}", budget_line(c),
             "Stop posting new clips to this campaign. Views on posted clips may not be paid once it is empty."]
    if c.get("link"):
        lines.append(f'<a href="{escape(c["link"], quote=True)}">Open campaign</a>')
    return "\n".join(lines), []


def vod_link(platform: str, url: str | None, start_s: float) -> str | None:
    if not url:
        return None
    if platform == "twitch":
        s = int(start_s)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}t={s // 3600}h{s % 3600 // 60}m{s % 60}s"
    return url


def clip_alert(a: dict) -> tuple[str, Buttons]:
    lines = [f"🎬 <b>Strong moment</b>: {escape(a['streamer'])} ({escape(a['platform'])})",
             f"{ts(a['start_s'])} to {ts(a['end_s'])} · score {a['score']:.2f} · {escape(a.get('moment_type') or 'mixed')}"]
    if a.get("vod_title"):
        lines.append(f"VOD: {escape(a['vod_title'])}")
    sample = a.get("sample_chat")
    if isinstance(sample, str):
        sample = json.loads(sample or "[]")
    if sample:
        lines.append("Chat: " + escape(" / ".join(sample[:3]))[:200])
    link = vod_link(a["platform"], a.get("vod_url"), a["start_s"])
    if link:
        lines.append(f'<a href="{escape(link, quote=True)}">Watch at {ts(a["start_s"])}</a>')
    lines.append("Nothing is posted from here. Rendering and posting come in later phases.")
    return "\n".join(lines), [clip_buttons(a["id"])]
