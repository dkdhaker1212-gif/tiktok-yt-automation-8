"""Read the DB directly and report per-day upload health. Independent of any portal.

    python -m src.audit            # last 7 days, all channels
    python -m src.audit --days 14
"""
from __future__ import annotations

import argparse
import datetime as dt

from .config import load_channels
from .db import DB
from .notifier import send


def _slot_due(publish_hhmm: str, day: dt.date) -> bool:
    """A slot counts as 'due' only once its time + 100 min retry window has passed."""
    h, m = (int(x) for x in publish_hhmm.split(":"))
    fire = dt.datetime.combine(day, dt.time(h, m), tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) >= fire + dt.timedelta(minutes=100)


def build_report(days: int = 7) -> str:
    today = dt.datetime.now(dt.timezone.utc).date()
    out: list[str] = [f"**Daily audit** ({today} UTC, last {days}d)"]
    for ch in load_channels():
        db = DB(ch.db_path())
        try:
            lines = [f"__`{ch.id}`__ ({ch.youtube_channel_name})"]
            short_days = 0
            for d in range(days):
                day = today - dt.timedelta(days=d)
                got = db.uploads_on(ch.id, day.isoformat())
                due = sum(
                    1 for slot in range(1, ch.videos_per_day + 1)
                    if _slot_due(ch.slot_publish_times_utc[slot], day)
                )
                mark = "✅" if got >= due else "⚠️"
                if got < due:
                    short_days += 1
                lines.append(f"  {mark} {day.isoformat()}  {got}/{due}")
            if short_days:
                for r in db.recent_runs(ch.id, 10):
                    lines.append(
                        f"    · {r['run_date']} slot{r['slot']} "
                        f"{r['status']} — {(r['detail'] or '')[:80]}"
                    )
            out.append("\n".join(lines))
        finally:
            db.close()
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-discord", action="store_true")
    args = ap.parse_args()
    report = build_report(args.days)
    print(report)
    if not args.no_discord:
        send(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
