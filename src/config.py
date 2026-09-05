"""channels.yaml loader + validation.

config.py validates upload_mode against a whitelist -- when adding a mode to the
picker (channel_runner.py), add it here too or the run fails at config load.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

# --- upload modes the picker knows how to handle -----------------------------
# Keep in sync with channel_runner.pick_video().
VALID_UPLOAD_MODES = {
    "popular_split",   # slot1 newest, slot2 most-viewed (whole profile)  <- default
    "short_only",      # slot1 newest, slot2 newest
    "popular_only",    # slot1 most-viewed  (1/day channels)
    "sequence",        # explicit ordered list + N-day gap
}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS_YAML = os.path.join(_REPO_ROOT, "channels.yaml")


@dataclass
class Channel:
    id: str
    tiktok_username: str
    youtube_channel_name: str
    owner_email: str
    google_credentials_file: str
    oauth_token_file: str
    videos_per_day: int = 2
    description_footer: str = ""
    default_tags: list = field(default_factory=list)
    youtube_category_id: str = "24"
    enabled: bool = True
    max_retry_days: int = 7
    shorts_max_seconds: int = 180
    upload_mode: str = "popular_split"
    max_download_candidates: int = 20
    slot_publish_times_utc: dict = field(default_factory=dict)

    # editing + SEO + thumbnail
    use_ai_seo: bool = True
    seo_language: str = "en"                        # "en" | "es" -> Gemini/AI output language
    edit: dict = field(default_factory=dict)        # -> video_editor.EditOptions
    thumbnail: dict = field(default_factory=dict)   # -> thumbnail.build cfg

    # optional
    tiktok_username_slot2: Optional[str] = None
    min_upload_date: Optional[str] = None
    min_backlog_for_slot1: Optional[int] = None
    fixed_title: Optional[str] = None
    sequence_ids: list = field(default_factory=list)
    sequence_gap_days: int = 1

    @property
    def repo_root(self) -> str:
        return _REPO_ROOT

    def abspath(self, rel: str) -> str:
        return rel if os.path.isabs(rel) else os.path.join(_REPO_ROOT, rel)

    def db_path(self) -> str:
        return os.path.join(_REPO_ROOT, "data", f"{self.id}.db")

    def tiktok_username_for_slot(self, slot: int) -> str:
        if slot == 2 and self.tiktok_username_slot2:
            return self.tiktok_username_slot2
        return self.tiktok_username


def _validate(ch: Channel) -> None:
    if ch.upload_mode not in VALID_UPLOAD_MODES:
        raise ValueError(
            f"channel {ch.id}: upload_mode '{ch.upload_mode}' is not in the whitelist "
            f"{sorted(VALID_UPLOAD_MODES)}"
        )
    if ch.videos_per_day not in (1, 2):
        raise ValueError(f"channel {ch.id}: videos_per_day must be 1 or 2")
    for slot in range(1, ch.videos_per_day + 1):
        if slot not in ch.slot_publish_times_utc:
            raise ValueError(
                f"channel {ch.id}: slot_publish_times_utc is missing slot {slot}"
            )
        hhmm = str(ch.slot_publish_times_utc[slot])
        if len(hhmm.split(":")) != 2:
            raise ValueError(
                f"channel {ch.id}: slot {slot} time '{hhmm}' must be HH:MM"
            )
    if ch.max_download_candidates < 5:
        raise ValueError(
            f"channel {ch.id}: max_download_candidates={ch.max_download_candidates} "
            f"is dangerously low; set 15-20"
        )
    if "REPLACE_WITH" in ch.owner_email:
        raise ValueError(
            f"channel {ch.id}: owner_email is still the placeholder -- set the real Gmail"
        )


def load_channels(path: str = CHANNELS_YAML) -> list[Channel]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    out: list[Channel] = []
    for entry in raw.get("channels", []):
        # normalise slot keys to int
        times = entry.get("slot_publish_times_utc", {}) or {}
        entry["slot_publish_times_utc"] = {int(k): str(v) for k, v in times.items()}
        known = {f.name for f in Channel.__dataclass_fields__.values()}  # type: ignore
        ch = Channel(**{k: v for k, v in entry.items() if k in known})
        _validate(ch)
        out.append(ch)
    return out


def get_channel(channel_id: str, path: str = CHANNELS_YAML) -> Channel:
    for ch in load_channels(path):
        if ch.id == channel_id:
            return ch
    raise KeyError(f"channel '{channel_id}' not found in {path}")
