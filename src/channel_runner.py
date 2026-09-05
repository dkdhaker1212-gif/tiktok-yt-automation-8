"""The per-channel pipeline + all the pickers.

  run(channel, slot, dry_run):
    per-day guard -> due retries first -> pick by mode -> download loop over
    candidates -> upload -> DB update
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Optional

from . import seo as seo_mod
from . import thumbnail as thumb_mod
from . import tiktok_downloader as td
from . import video_editor
from .config import Channel
from .db import DB


@dataclass
class SlotResult:
    channel_id: str
    slot: int
    status: str            # success | skipped | no_content | failed
    detail: str = ""
    youtube_id: Optional[str] = None
    tiktok_id: Optional[str] = None
    title: str = ""


# --------------------------------------------------------------------------
# Pickers -- return an ordered list of candidate entries (best first)
# --------------------------------------------------------------------------
def _unposted(entries: list[dict], posted: set[str]) -> list[dict]:
    out = []
    for e in entries:
        if not e["id"] or e["id"] in posted:
            continue
        if e["is_photo"]:                       # slideshow -> no video stream
            continue
        out.append(e)
    return out


def pick_candidates(channel: Channel, slot: int, entries: list[dict],
                    posted: set[str]) -> list[dict]:
    """Ordered candidate list for this mode+slot. channel_runner picker whitelist
    must stay in sync with config.VALID_UPLOAD_MODES."""
    mode = channel.upload_mode
    avail = _unposted(entries, posted)

    if channel.min_upload_date:
        cutoff = dt.datetime.strptime(channel.min_upload_date, "%Y-%m-%d")
        cutoff = cutoff.replace(tzinfo=dt.timezone.utc).timestamp()
        avail = [e for e in avail if (e["timestamp"] or 0) >= cutoff]

    newest = sorted(avail, key=lambda e: e["timestamp"] or 0, reverse=True)
    most_viewed = sorted(avail, key=lambda e: e["view_count"] or 0, reverse=True)

    if mode == "popular_split":
        return newest if slot == 1 else most_viewed
    if mode == "short_only":
        return newest
    if mode == "popular_only":
        return most_viewed
    if mode == "sequence":
        order = {vid: i for i, vid in enumerate(channel.sequence_ids)}
        seq = [e for e in avail if e["id"] in order]
        seq.sort(key=lambda e: order[e["id"]])
        return seq
    # unreachable: config.py validated the whitelist
    raise ValueError(f"unknown upload_mode {mode!r}")


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------
def _is_short(channel: Channel, meta: dict, entry: dict) -> bool:
    dur = meta.get("duration") or entry.get("duration") or 0
    w = meta.get("width") or entry.get("width") or 0
    h = meta.get("height") or entry.get("height") or 0
    vertical = (h >= w) if (w and h) else True     # assume vertical if unknown
    return bool(dur and dur <= channel.shorts_max_seconds and vertical)


def _title_for(channel: Channel, meta: dict, entry: dict) -> str:
    if channel.fixed_title:
        return channel.fixed_title
    return meta.get("title") or entry.get("title") or "Untitled"


def _description_for(channel: Channel, meta: dict, entry: dict) -> str:
    cap = meta.get("description") or entry.get("title") or ""
    foot = channel.description_footer or ""
    return (cap + ("\n\n" + foot if foot else "")).strip()


def run(channel: Channel, slot: int, dry_run: bool = False,
        force: bool = False) -> SlotResult:
    from . import youtube_uploader as yu       # imported late: needs google libs

    db = DB(channel.db_path())
    cid = channel.id
    try:
        # -- per-day guard (skipped with --force) ------------------------
        if not force and db.slot_already_ran_today(cid, slot):
            res = SlotResult(cid, slot, "skipped", "per-day guard: already ran today")
            db.record_run(cid, slot, "skipped", res.detail)
            return res

        username = channel.tiktok_username_for_slot(slot)

        # -- list profile (with the secondary-account fallback) ----------
        try:
            entries = td.list_profile(username)
        except RuntimeError as exc:
            res = SlotResult(cid, slot, "failed", f"listing failed: {exc}")
            db.record_run(cid, slot, "failed", res.detail)
            return res

        posted = db.posted_video_ids(cid)
        candidates = pick_candidates(channel, slot, entries, posted)

        # secondary account exhausted -> fall back to primary (Automation.md S6)
        if not candidates and slot == 2 and channel.tiktok_username_slot2:
            print("[run] slot2 source exhausted; falling back to primary account")
            entries = td.list_profile(channel.tiktok_username)
            candidates = pick_candidates(channel, slot, entries, posted)

        # -- due retries jump the queue --------------------------------
        due = db.due_retries(cid)
        if due:
            retry_ids = [r["video_id"] for r in due]
            by_id = {e["id"]: e for e in entries}
            retry_entries = [by_id[i] for i in retry_ids if i in by_id]
            candidates = retry_entries + [
                c for c in candidates if c["id"] not in retry_ids
            ]

        if not candidates:
            res = SlotResult(cid, slot, "no_content", "no unposted videos left")
            db.record_run(cid, slot, "no_content", res.detail)
            return res

        # -- download loop over candidates ----------------------------
        dest = os.path.join(channel.repo_root, "downloads")
        limit = channel.max_download_candidates
        last_err = ""
        for entry in candidates[:limit]:
            meta = td.probe_entry_meta(username, entry["id"]) or {}
            path = td.download(entry, dest)
            if not path:
                last_err = f"download failed for {entry['id']}"
                db.mark_pending_retry(
                    cid, entry["id"], slot,
                    _title_for(channel, meta, entry), entry["url"],
                    entry.get("view_count"), last_err, channel.max_retry_days,
                )
                continue

            short = _is_short(channel, meta, entry)
            caption = meta.get("description") or entry.get("title") or ""
            tiktok_tags = meta.get("tags", []) or []

            # -- SEO: AI title / description / tags -----------------------
            recent_titles = db.recent_titles(cid)
            if channel.use_ai_seo and not dry_run:
                s = seo_mod.generate(caption, tiktok_tags,
                                     channel.default_tags, short,
                                     media_path=path, recent_titles=recent_titles,
                                     language=channel.seo_language)
            else:
                s = seo_mod._fallback(caption, tiktok_tags,
                                      channel.default_tags, short,
                                      recent_titles=recent_titles,
                                      language=channel.seo_language)
            title = channel.fixed_title or s.title
            desc = (s.description + (
                "\n\n" + channel.description_footer
                if channel.description_footer else "")).strip()
            tags = s.tags

            # -- edit pass: blur watermark + light de-dup transform -----
            upload_path = path
            edited_path = None
            if not dry_run and (channel.edit or {}).get("enabled", True):
                edited_path = os.path.join(dest, f"{entry['id']}_edit.mp4")
                upload_path = video_editor.process(path, edited_path, channel.edit)

            thumb_path = None
            try:
                yt_id = yu.upload(
                    file_path=upload_path, title=title, description=desc,
                    category_id=channel.youtube_category_id, tags=tags,
                    is_short=short,
                    client_secret_file=channel.abspath(channel.google_credentials_file),
                    token_file=channel.abspath(channel.oauth_token_file),
                    dry_run=dry_run,
                )
                # build the thumbnail while the video file still exists
                if not dry_run and yt_id and (channel.thumbnail or {}).get("enabled", True):
                    try:
                        _tcfg = dict(channel.thumbnail or {})
                        if getattr(s, "thumb_hook", ""):
                            _tcfg["hook_text"] = s.thumb_hook
                        thumb_path = thumb_mod.build(
                            upload_path,
                            os.path.join(dest, f"{entry['id']}_thumb.jpg"),
                            title, _tcfg,
                            duration=(meta.get("duration") or entry.get("duration")),
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"[thumbnail] build error: {exc}")
            except yu.ChannelSuspended as exc:
                res = SlotResult(cid, slot, "failed",
                                 f"CHANNEL SUSPENDED: {exc}", title=title)
                db.record_run(cid, slot, "failed", res.detail)
                return res
            finally:
                if not dry_run:
                    for p in (path, edited_path):
                        if p and os.path.isfile(p):
                            try:
                                os.remove(p)
                            except OSError:
                                pass

            if dry_run:
                preview = (
                    f"DRY RUN: would upload {entry['id']}\n"
                    f"  title : {title}\n"
                    f"  tags  : {', '.join(tags[:20])}\n"
                    f"  desc  : {desc[:160].replace(chr(10), ' / ')}\n"
                    f"  short : {short}"
                )
                print(preview)
                res = SlotResult(cid, slot, "success", preview,
                                 tiktok_id=entry["id"], title=title)
                db.record_run(cid, slot, "success",
                              f"DRY RUN would upload {entry['id']} ({title!r})")
                return res

            db.mark_uploaded(
                cid, entry["id"], slot, title, entry["url"],
                entry.get("view_count"), yt_id,
            )
            if thumb_path:
                yu.set_thumbnail(
                    video_id=yt_id, thumb_path=thumb_path,
                    client_secret_file=channel.abspath(channel.google_credentials_file),
                    token_file=channel.abspath(channel.oauth_token_file),
                )
                try:
                    os.remove(thumb_path)
                except OSError:
                    pass
            res = SlotResult(cid, slot, "success",
                             f"uploaded {entry['id']} -> {yt_id}",
                             youtube_id=yt_id, tiktok_id=entry["id"], title=title)
            db.record_run(cid, slot, "success", res.detail)
            return res

        res = SlotResult(cid, slot, "failed",
                         f"all {limit} candidates failed to download; last: {last_err}")
        db.record_run(cid, slot, "failed", res.detail)
        return res
    finally:
        db.close()
