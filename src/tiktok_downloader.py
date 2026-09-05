"""yt-dlp wrapper: profile listing, format selection, download, audio verification.

Every rule in here fixed a real outage. See Automation.md Section 9. Do not "simplify".
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Optional

import yt_dlp

# Rule 3: TikTok serves a bot-challenge page to requests without this header.
_HTTP_HEADERS = {"Referer": "https://www.tiktok.com/"}

# Rule 1: never the watermarked stream. format_id^=download == "Save video" == watermark.
# Prefer h264 -- bytevc1/h265 streams claim acodec=aac but download video-only.
_FORMAT_CHAIN = "/".join([
    "bestvideo[format_id^=play][ext=mp4]+bestaudio",
    "best[format_id^=play][ext=mp4][vcodec!=none]",
    "best[format_id^=play][vcodec!=none]",
    "best[format_id^=h264][ext=mp4][vcodec!=none]",
    "best[ext=mp4][vcodec!=none]",
    "best[vcodec!=none]",
])

# Rule 6: fetch 150, not 50. Deeper unbounded pagination is what TikTok blocks on runners.
PROFILE_BATCH = 150


def _impersonate_target() -> Optional[str]:
    """Rule 7: resolve the target from what yt-dlp actually registered.
    Never hard-code "chrome" -- curl_cffi 0.15+ silently registers nothing.
    Returns an ImpersonateTarget (or None). yt-dlp's TikTok extractor forces
    impersonate=True, so this MUST resolve or listing throws a bare AssertionError.
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        rd = ydl._request_director
        available = []
        for rh in rd.handlers.values():
            for tgt in getattr(rh, "_SUPPORTED_IMPERSONATE_TARGET_MAP", {}) or {}:
                available.append(tgt)
        if not available:
            print("[impersonate] no targets registered -- curl_cffi missing or too new")
            return None
        for t in available:
            if "chrome" in str(t).lower():
                return t
        return available[0]
    except Exception as exc:  # noqa: BLE001
        print(f"[impersonate] resolution failed: {exc!r}")
        return None


def _cookiefile() -> Optional[str]:
    p = os.environ.get("TIKTOK_COOKIES_FILE", "").strip()
    return p if p and os.path.isfile(p) else None


_IMPERSONATE = _impersonate_target()


def _base_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "http_headers": _HTTP_HEADERS,
    }
    if _IMPERSONATE is not None:
        opts["impersonate"] = _IMPERSONATE
    ck = _cookiefile()
    if ck:
        opts["cookiefile"] = ck
    return opts


# --------------------------------------------------------------------------
# Profile listing
# --------------------------------------------------------------------------
def list_profile(username: str, limit: int = PROFILE_BATCH) -> list[dict]:
    """Return newest-first flat entries for a TikTok profile.

    Rule 5: an empty listing is a *failed attempt*, not an empty profile
    (ignoreerrors swallows the rejection). Retry 3x with 2/4/8s before giving up.
    Rule 2 (view_count): copied out of each entry here so "most-viewed" works.
    """
    url = f"https://www.tiktok.com/@{username.lstrip('@')}"
    opts = _base_opts()
    opts.update({
        "extract_flat": True,
        "playlistend": limit,
    })
    import traceback
    print(f"[list_profile] impersonate={_IMPERSONATE!r} cookies={bool(_cookiefile())}")
    last_err = ""
    for attempt, pause in enumerate(([2, 4, 8]), start=1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = [e for e in (info or {}).get("entries", []) if e]
            if entries:
                return [_normalise_entry(e) for e in entries]
            last_err = "empty listing (likely a rejected request)"
        except Exception as exc:  # noqa: BLE001
            last_err = (f"{type(exc).__name__}: {exc}\n" +
                        traceback.format_exc())[-1200:]
        print(f"[list_profile] attempt {attempt} failed: {last_err}\nsleeping {pause}s")
        time.sleep(pause)
    raise RuntimeError(f"could not list @{username} after 3 attempts: {last_err}")


def _normalise_entry(e: dict) -> dict:
    vid = str(e.get("id") or "")
    return {
        "id": vid,
        "url": e.get("url") or f"https://www.tiktok.com/@_/video/{vid}",
        "title": (e.get("title") or e.get("description") or "").strip(),
        "duration": e.get("duration"),
        "view_count": e.get("view_count"),          # Rule 2
        "timestamp": e.get("timestamp"),
        "is_photo": "/photo/" in (e.get("url") or ""),   # slideshow -> no video stream
        "width": e.get("width"),
        "height": e.get("height"),
    }


# --------------------------------------------------------------------------
# Download + audio verification
# --------------------------------------------------------------------------
_FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")


def _has_audio(path: str) -> bool:
    """Rule 2: ffprobe -select_streams a. yt-dlp metadata cannot be trusted."""
    try:
        out = subprocess.run(
            [_FFPROBE, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
        return bool(data.get("streams"))
    except Exception as exc:  # noqa: BLE001
        print(f"[_has_audio] ffprobe failed ({exc}); assuming no audio")
        return False


def _download_once(video_url: str, out_tmpl: str, fmt: str) -> Optional[str]:
    opts = _base_opts()
    opts.update({
        "format": fmt,
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "overwrites": True,
        "noplaylist": True,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        rc = ydl.download([video_url])
    if rc != 0:
        return None
    # resolve the produced file
    base = out_tmpl.replace("%(ext)s", "")
    for ext in ("mp4", "mkv", "webm", "mov"):
        cand = base + ext
        if os.path.isfile(cand) and os.path.getsize(cand) > 0:
            return cand
    return None


def download(entry: dict, dest_dir: str) -> Optional[str]:
    """Download one video without watermark, guaranteed to have audio.

    Returns the file path, or None if it cannot be downloaded with sound
    (Rule 2: never upload a silent video).
    """
    os.makedirs(dest_dir, exist_ok=True)
    vid = entry["id"]
    video_url = entry["url"]
    out_tmpl = os.path.join(dest_dir, f"{vid}.%(ext)s")

    # Rule 4: retry each download 3x with a pause. ~30% random rejection.
    for attempt, pause in enumerate((0, 4, 8), start=1):
        if pause:
            time.sleep(pause)
        path = _download_once(video_url, out_tmpl, _FORMAT_CHAIN)
        if not path:
            print(f"[download] {vid} attempt {attempt}: yt-dlp returned no file")
            continue
        if _has_audio(path):
            return path
        print(f"[download] {vid} attempt {attempt}: no audio track, retrying audio-safe")
        # audio-safe selector: force a separate bestaudio merge
        safe = "bestvideo[vcodec!=none]+bestaudio/best[acodec!=none][vcodec!=none]"
        path = _download_once(video_url, out_tmpl, safe)
        if path and _has_audio(path):
            return path
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    print(f"[download] {vid}: giving up -- could not get a version with audio")
    return None


def probe_entry_meta(username: str, video_id: str) -> dict:
    """Full metadata for one video (caption, tags) -- used at upload time."""
    url = f"https://www.tiktok.com/@{username.lstrip('@')}/video/{video_id}"
    opts = _base_opts()
    for attempt, pause in enumerate((0, 3, 6), start=1):
        if pause:
            time.sleep(pause)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                return {
                    "title": (info.get("title") or info.get("description") or "").strip(),
                    "description": (info.get("description") or "").strip(),
                    "tags": info.get("tags") or [],
                    "duration": info.get("duration"),
                    "width": info.get("width"),
                    "height": info.get("height"),
                }
        except Exception as exc:  # noqa: BLE001
            print(f"[probe_entry_meta] attempt {attempt}: {exc}")
    return {}
