"""Build a hook-text thumbnail for a Short and (optionally) set it via the API.

Note: custom thumbnails do NOT appear in the Shorts swipe feed -- only on the
channel's Shorts grid, in search, and in shares. Still worth it for CTR there.

Setting a thumbnail needs OAuth scope youtube.force-ssl (or youtube) -- NOT
just youtube.upload. If the token lacks it, set_thumbnail() logs and returns
False; the upload itself is unaffected.
"""
from __future__ import annotations

import os
import random
import subprocess
import textwrap
from typing import Optional

THUMB_W, THUMB_H = 1280, 720
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")


def _grab_frame(video_path: str, out_png: str, at_seconds: float) -> bool:
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-ss", f"{at_seconds:.2f}", "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out_png],
            capture_output=True, text=True, timeout=120,
        )
        return r.returncode == 0 and os.path.isfile(out_png)
    except Exception as exc:  # noqa: BLE001
        print(f"[thumbnail] frame grab failed: {exc}")
        return False


_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",       # Ubuntu (CI)
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",                              # macOS
    r"C:\Windows\Fonts\ariblk.ttf",                               # Windows
    r"C:\Windows\Fonts\arialbd.ttf",
]


def _bold_font() -> Optional[str]:
    for p in _BOLD_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None   # PIL falls back to its built-in bitmap font


def _hook_from(cfg: dict, title: str) -> str:
    explicit = (cfg.get("hook_text") or "").strip()
    if explicit:
        return explicit.split("#")[0].strip(" -|").upper()[:38]
    templates = cfg.get("hook_templates") or [
        "YOU WON'T BELIEVE THIS",
        "WAIT FOR IT",
        "THIS MACHINE IS INSANE",
        "HOW IS THIS REAL?",
    ]
    src = (cfg.get("hook_source") or "title").lower()
    if src == "template":
        return random.choice(templates).upper()
    # from title: take the punchy front, strip hashtags, cap length
    t = title.split("#")[0].strip(" -|").upper()
    words = t.split()
    return " ".join(words[:6])[:38] or random.choice(templates).upper()


def build(video_path: str, out_jpg: str, title: str,
          cfg: Optional[dict] = None, duration: Optional[float] = None) -> Optional[str]:
    """Render out_jpg (1280x720). Returns the path, or None on failure."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return None

    at = cfg.get("frame_at")
    if at is None:
        at = (duration or 6) * 0.45
    tmp_png = out_jpg + ".frame.png"
    if not _grab_frame(video_path, tmp_png, float(at)):
        return None

    try:
        src = Image.open(tmp_png).convert("RGB")
        # blurred cover background
        cover = src.copy()
        sr = cover.width / cover.height
        tr = THUMB_W / THUMB_H
        if sr > tr:
            nh = THUMB_H
            nw = int(nh * sr)
        else:
            nw = THUMB_W
            nh = int(nw / sr)
        cover = cover.resize((nw, nh)).filter(ImageFilter.GaussianBlur(28))
        canvas = Image.new("RGB", (THUMB_W, THUMB_H))
        canvas.paste(cover, ((THUMB_W - nw) // 2, (THUMB_H - nh) // 2))
        # sharp portrait frame centred
        fh = THUMB_H
        fw = int(fh * src.width / src.height)
        canvas.paste(src.resize((fw, fh)), ((THUMB_W - fw) // 2, 0))

        d = ImageDraw.Draw(canvas)
        hook = _hook_from(cfg, title)
        fp = cfg.get("font") or _bold_font()

        def _font(sz):
            return ImageFont.truetype(fp, sz) if fp else ImageFont.load_default(sz)

        # fit text width
        size = 96
        while size > 30:
            fnt = _font(size)
            lines = textwrap.wrap(hook, width=18) or [hook]
            widest = max(d.textbbox((0, 0), ln, font=fnt)[2] for ln in lines)
            if widest <= THUMB_W - 120:
                break
            size -= 4
        line_h = int(size * 1.18)
        block_h = line_h * len(lines)
        pos = (cfg.get("position") or "bottom").lower()
        y = 40 if pos == "top" else THUMB_H - block_h - 54
        accent = tuple(cfg.get("accent_rgb", [240, 164, 32]))
        for ln in lines:
            w = d.textbbox((0, 0), ln, font=fnt)[2]
            x = (THUMB_W - w) // 2
            d.text((x, y), ln, font=fnt, fill=(255, 255, 255),
                   stroke_width=max(6, size // 12), stroke_fill=(0, 0, 0))
            y += line_h
        # accent underline
        d.rectangle([THUMB_W // 2 - 160, y + 6, THUMB_W // 2 + 160, y + 14],
                    fill=accent)

        canvas.save(out_jpg, "JPEG", quality=88)
        return out_jpg
    except Exception as exc:  # noqa: BLE001
        print(f"[thumbnail] render failed: {exc}")
        return None
    finally:
        for p in (tmp_png,):
            try:
                os.remove(p)
            except OSError:
                pass
