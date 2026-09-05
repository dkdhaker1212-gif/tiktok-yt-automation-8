"""Render a single self-contained dashboard.html for all configured channels.

    python -m src.dashboard            # writes ./dashboard.html

Shows per channel: last upload (+ link), next scheduled run in IST, today's
status (posted / due / missed), total posted, and recent run outcomes.
No third-party deps beyond pyyaml (already required).
"""
from __future__ import annotations

import datetime as dt
import html
import os

from .config import load_channels
from .db import DB

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _fmt_ist(when_utc: dt.datetime) -> str:
    return when_utc.astimezone(IST).strftime("%d %b, %I:%M %p IST")


def _next_run(times_utc: dict) -> dt.datetime:
    now = dt.datetime.now(dt.timezone.utc)
    cands = []
    for hhmm in times_utc.values():
        h, m = (int(x) for x in str(hhmm).split(":"))
        for day_off in (0, 1):
            t = (now + dt.timedelta(days=day_off)).replace(
                hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                cands.append(t)
    return min(cands) if cands else now


def _channel_block(ch) -> str:
    db = DB(ch.db_path())
    try:
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        rows = db._conn.execute(
            "SELECT video_id, youtube_id, title, updated_at FROM posted_videos "
            "WHERE channel_id=? AND status='uploaded' ORDER BY updated_at DESC LIMIT 1",
            (ch.id,),
        ).fetchone()
        total = db._conn.execute(
            "SELECT COUNT(*) n FROM posted_videos WHERE channel_id=? AND status='uploaded'",
            (ch.id,),
        ).fetchone()["n"]
        got_today = db.uploads_on(ch.id, today)
        due_today = 0
        for slot, hhmm in ch.slot_publish_times_utc.items():
            h, m = (int(x) for x in str(hhmm).split(":"))
            fire = dt.datetime.combine(
                dt.date.fromisoformat(today), dt.time(h, m), tzinfo=dt.timezone.utc)
            if dt.datetime.now(dt.timezone.utc) >= fire + dt.timedelta(minutes=100):
                due_today += 1
        runs = db.recent_runs(ch.id, 6)
    finally:
        db.close()

    if got_today >= max(due_today, 1) or (due_today == 0):
        badge, bcls = ("on track", "ok") if due_today else ("waiting", "idle")
    else:
        badge, bcls = (f"missed {due_today - got_today}", "bad")

    if rows:
        last_when = dt.datetime.fromisoformat(rows["updated_at"])
        last = (f'<a href="https://youtu.be/{html.escape(rows["youtube_id"] or "")}" '
                f'target="_blank">{html.escape((rows["title"] or "")[:70])}</a>'
                f'<span class="dim"> — {_fmt_ist(last_when)}</span>')
    else:
        last = '<span class="dim">nothing yet</span>'

    run_rows = "".join(
        f'<tr><td>{html.escape(r["run_date"])}</td>'
        f'<td class="s-{html.escape(r["status"])}">{html.escape(r["status"])}</td>'
        f'<td class="dim">{html.escape((r["detail"] or "")[:80])}</td></tr>'
        for r in runs
    ) or '<tr><td colspan="3" class="dim">no runs recorded</td></tr>'

    return f"""
    <section class="card">
      <div class="head">
        <h2>{html.escape(ch.youtube_channel_name)} <span class="dim">({html.escape(ch.id)})</span></h2>
        <span class="badge {bcls}">{badge}</span>
      </div>
      <div class="grid">
        <div><label>Source</label>@{html.escape(ch.tiktok_username)}</div>
        <div><label>Cadence</label>{ch.videos_per_day}/day · {" & ".join(str(v) for v in ch.slot_publish_times_utc.values())} UTC</div>
        <div><label>Next run</label>{_fmt_ist(_next_run(ch.slot_publish_times_utc))}</div>
        <div><label>Total uploaded</label>{total}</div>
      </div>
      <p class="last"><label>Last upload</label>{last}</p>
      <table><thead><tr><th>Date</th><th>Status</th><th>Detail</th></tr></thead>
        <tbody>{run_rows}</tbody></table>
    </section>"""


def render() -> str:
    chans = load_channels()
    body = "".join(_channel_block(c) for c in chans if c.enabled)
    updated = _fmt_ist(dt.datetime.now(dt.timezone.utc))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Automation dashboard</title>
<style>
 :root{{--bg:#0e1f16;--card:#14261c;--line:#26402f;--txt:#eef2ee;--dim:#8fae9b;--amber:#f0a420}}
 *{{box-sizing:border-box}}
 body{{margin:0;padding:24px;background:var(--bg);color:var(--txt);
   font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
 h1{{font-size:1.3rem;margin:0 0 4px}}
 .sub{{color:var(--dim);margin:0 0 24px;font-size:.9rem}}
 .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;
   padding:18px 20px;margin:0 0 16px;max-width:820px}}
 .head{{display:flex;justify-content:space-between;align-items:center;gap:12px}}
 h2{{font-size:1.05rem;margin:0}}
 .dim{{color:var(--dim)}}
 label{{display:block;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);margin-bottom:2px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0}}
 .last{{margin:8px 0 14px}}
 .badge{{font-size:.75rem;padding:3px 10px;border-radius:999px;white-space:nowrap}}
 .badge.ok{{background:#1c3a29;color:#8fe0a8}}
 .badge.bad{{background:#3a1c1c;color:#f0a0a0}}
 .badge.idle{{background:#2a2f38;color:#c0c8d0}}
 table{{width:100%;border-collapse:collapse;font-size:.85rem}}
 th,td{{text-align:left;padding:5px 8px;border-top:1px solid var(--line);vertical-align:top}}
 a{{color:var(--amber)}}
 .s-success{{color:#8fe0a8}}.s-failed{{color:#f0a0a0}}.s-skipped{{color:#c0c8d0}}.s-no_content{{color:#e0c890}}
</style></head><body>
<h1>Automation dashboard</h1>
<p class="sub">Updated {updated} · {sum(1 for c in chans if c.enabled)} channel(s)</p>
{body}
</body></html>"""


def main() -> int:
    out = os.path.join(_ROOT, "dashboard.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render())
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
