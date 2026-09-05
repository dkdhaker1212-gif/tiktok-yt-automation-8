"""SQLite state -- the only thing that persists between runs.

The .db file lives in data/<channel_id>.db and is committed back to the repo after
every run. Two tables:

  posted_videos : one row per TikTok video the channel has *dealt with*
  runs          : one row per (channel, slot, UTC-day) attempt  -> the per-day guard
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

# statuses that count as "this video is done, do not pick it again"
POSTED_STATUSES = ("uploaded", "failed_permanent", "skipped", "pending_retry")


def _utc_today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


SCHEMA = """
CREATE TABLE IF NOT EXISTS posted_videos (
    channel_id      TEXT NOT NULL,
    video_id        TEXT NOT NULL,          -- TikTok video id
    status          TEXT NOT NULL,          -- uploaded | failed_permanent | skipped
                                            -- | pending_retry
    slot            INTEGER,
    title           TEXT,
    tiktok_url      TEXT,
    view_count      INTEGER,
    youtube_id      TEXT,
    error           TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    next_retry_date TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (channel_id, video_id)
);

CREATE TABLE IF NOT EXISTS runs (
    channel_id  TEXT NOT NULL,
    slot        INTEGER NOT NULL,
    run_date    TEXT NOT NULL,              -- UTC YYYY-MM-DD
    status      TEXT NOT NULL,              -- success | skipped | no_content | failed
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_lookup ON runs (channel_id, slot, run_date, status);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- context helpers ----------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        # collapse the WAL back into the main file so the committed .db is complete
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()

    # -- per-day guard ----------------------------------------------------
    def slot_already_ran_today(self, channel_id: str, slot: int) -> bool:
        """True if a *successful or skipped* run row exists for today+slot.

        A prior 'failed' / 'no_content' does NOT block a retry.
        """
        row = self._conn.execute(
            "SELECT 1 FROM runs WHERE channel_id=? AND slot=? AND run_date=? "
            "AND status IN ('success','skipped') LIMIT 1",
            (channel_id, slot, _utc_today()),
        ).fetchone()
        return row is not None

    def record_run(self, channel_id: str, slot: int, status: str,
                   detail: str = "") -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs (channel_id, slot, run_date, status, detail, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (channel_id, slot, _utc_today(), status, detail[:2000], now),
            )

    # -- posted_videos --------------------------------------------------
    def posted_video_ids(self, channel_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT video_id FROM posted_videos WHERE channel_id=? AND status IN "
            f"({','.join('?' * len(POSTED_STATUSES))})",
            (channel_id, *POSTED_STATUSES),
        ).fetchall()
        return {r["video_id"] for r in rows}

    def get_video(self, channel_id: str, video_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM posted_videos WHERE channel_id=? AND video_id=?",
            (channel_id, video_id),
        ).fetchone()

    def due_retries(self, channel_id: str) -> list[sqlite3.Row]:
        """pending_retry rows whose next_retry_date <= today, oldest first."""
        return self._conn.execute(
            "SELECT * FROM posted_videos WHERE channel_id=? AND status='pending_retry' "
            "AND (next_retry_date IS NULL OR next_retry_date<=?) "
            "ORDER BY next_retry_date ASC, created_at ASC",
            (channel_id, _utc_today()),
        ).fetchall()

    def _upsert(self, channel_id: str, video_id: str, **fields) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        existing = self.get_video(channel_id, video_id)
        with self._tx() as c:
            if existing is None:
                cols = ["channel_id", "video_id", "created_at", "updated_at", *fields]
                vals = [channel_id, video_id, now, now, *fields.values()]
                c.execute(
                    f"INSERT INTO posted_videos ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(vals))})",
                    vals,
                )
            else:
                sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
                c.execute(
                    f"UPDATE posted_videos SET {sets} WHERE channel_id=? AND video_id=?",
                    [*fields.values(), now, channel_id, video_id],
                )

    def mark_uploaded(self, channel_id: str, video_id: str, slot: int, title: str,
                      tiktok_url: str, view_count: Optional[int],
                      youtube_id: str) -> None:
        self._upsert(
            channel_id, video_id, status="uploaded", slot=slot, title=title,
            tiktok_url=tiktok_url, view_count=view_count, youtube_id=youtube_id,
            error=None,
        )

    def mark_pending_retry(self, channel_id: str, video_id: str, slot: int,
                           title: str, tiktok_url: str, view_count: Optional[int],
                           error: str, max_retry_days: int) -> None:
        row = self.get_video(channel_id, video_id)
        retry_count = (row["retry_count"] if row else 0) + 1
        if retry_count > max_retry_days:
            self._upsert(
                channel_id, video_id, status="failed_permanent", slot=slot,
                title=title, tiktok_url=tiktok_url, view_count=view_count,
                error=error[:1000], retry_count=retry_count, next_retry_date=None,
            )
            return
        nxt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        self._upsert(
            channel_id, video_id, status="pending_retry", slot=slot, title=title,
            tiktok_url=tiktok_url, view_count=view_count, error=error[:1000],
            retry_count=retry_count, next_retry_date=nxt,
        )

    # -- audit --------------------------------------------------------
    def uploads_on(self, channel_id: str, day: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) n FROM posted_videos WHERE channel_id=? AND "
            "status='uploaded' AND substr(updated_at,1,10)=?",
            (channel_id, day),
        ).fetchone()
        return row["n"]

    def recent_runs(self, channel_id: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM runs WHERE channel_id=? ORDER BY created_at DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()

    def recent_titles(self, channel_id: str, limit: int = 60) -> list[str]:
        """Titles already used on this channel -- so SEO never repeats one."""
        rows = self._conn.execute(
            "SELECT title FROM posted_videos WHERE channel_id=? AND title IS NOT NULL "
            "AND title<>'' ORDER BY updated_at DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
        return [r["title"] for r in rows]
