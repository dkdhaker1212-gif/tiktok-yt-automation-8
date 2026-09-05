"""ffmpeg edit pass: audio loudness + quality polish + light de-dup transform.

Runs AFTER download, BEFORE upload. Opt-in via channels.yaml (`edit:` block).
If ffmpeg errors, the pipeline falls back to the original file so a bad filter
can never drop a slot.

Watermark blur is OFF by default: we download TikTok's `play` stream which is
already watermark-free, so blur boxes only vandalise clean footage. Set explicit
`blur_regions: [[x%,y%,w%,h%], ...]` only for a source that bakes in a logo.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")


@dataclass
class EditOptions:
    enabled: bool = True

    # de-dup transform (subtle -- changes the fingerprint, not the look)
    zoom_crop_pct: float = 2.0
    speed: float = 1.03                 # 1.0 = off; retimes video + audio
    hflip: bool = False                 # mirror -- strongest de-dup, changes framing
    fade_seconds: float = 0.25

    # quality polish
    sharpen: bool = True
    denoise: bool = False
    saturation: float = 1.06
    contrast: float = 1.04

    # audio
    loudnorm: bool = True               # -> ~-14 LUFS, the social standard (louder)
    volume_gain_db: float = 0.0         # extra gain applied before loudnorm

    # watermark cover (only when a source bakes in a logo)
    blur_regions: list = field(default_factory=list)   # [[x%,y%,w%,h%], ...]

    # brand badge overlay -- covers a source creator's baked-in logo + brands ours
    brand_overlay: dict = field(default_factory=dict)  # {enabled,image,corner,
                                                       #  width_pct,margin_pct,opacity}

    @classmethod
    def from_cfg(cls, cfg: Optional[dict]) -> "EditOptions":
        cfg = cfg or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
        return cls(**{k: v for k, v in cfg.items() if k in known})


def _probe(path: str) -> dict:
    # try ffprobe first
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,duration:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        dur = st.get("duration") or data.get("format", {}).get("duration") or 0
        if st.get("width"):
            return {"w": int(st["width"]), "h": int(st["height"]),
                    "duration": float(dur or 0)}
    except Exception:  # noqa: BLE001  -- fall through to ffmpeg -i parsing
        pass
    try:
        r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True,
                           timeout=60)
        err = r.stderr
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
        dm = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", err)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        dur = (int(dm.group(1)) * 3600 + int(dm.group(2)) * 60
               + float(dm.group(3))) if dm else 0.0
        return {"w": w, "h": h, "duration": dur}
    except Exception as exc:  # noqa: BLE001
        print(f"[video_editor] probe failed: {exc}")
        return {"w": 0, "h": 0, "duration": 0.0}


def _blur_chain(w: int, h: int, regions: list) -> tuple[list[str], str]:
    steps: list[str] = []
    src = "0:v"
    for i, r in enumerate(regions or []):
        if len(r) != 4:
            continue
        x, y = int(r[0] / 100 * w), int(r[1] / 100 * h)
        bw, bh = int(r[2] / 100 * w), int(r[3] / 100 * h)
        x = max(0, min(x, w - 8)); y = max(0, min(y, h - 8))
        bw = max(8, min(bw, w - x)); bh = max(8, min(bh, h - y))
        steps.append(
            f"[{src}]split=2[b{i}a][b{i}b];"
            f"[b{i}b]crop={bw}:{bh}:{x}:{y},boxblur=20:2[b{i}c];"
            f"[b{i}a][b{i}c]overlay={x}:{y}[wm{i}]"
        )
        src = f"wm{i}"
    return steps, src


def process(in_path: str, out_path: str, cfg: Optional[dict] = None) -> str:
    """Return out_path on success, or in_path unchanged if editing is off/failed."""
    opts = EditOptions.from_cfg(cfg)
    if not opts.enabled:
        return in_path

    meta = _probe(in_path)
    w, h = meta["w"], meta["h"]
    if not (w and h):
        print("[video_editor] could not probe size; skipping edit")
        return in_path

    chain, last = _blur_chain(w, h, opts.blur_regions)
    vf = list(chain)

    tail: list[str] = []
    if opts.zoom_crop_pct and opts.zoom_crop_pct > 0:
        keep = 1 - (opts.zoom_crop_pct / 100.0)
        tail.append(f"crop=iw*{keep:.4f}:ih*{keep:.4f}")
        tail.append(f"scale={w}:{h}:flags=lanczos")
    if opts.denoise:
        tail.append("hqdn3d=1.5:1.5:6:6")
    if opts.sharpen:
        tail.append("unsharp=5:5:0.8:5:5:0.0")
    eqs = []
    if opts.saturation and opts.saturation != 1.0:
        eqs.append(f"saturation={opts.saturation}")
    if opts.contrast and opts.contrast != 1.0:
        eqs.append(f"contrast={opts.contrast}")
    if eqs:
        tail.append("eq=" + ":".join(eqs))
    if opts.hflip:
        tail.append("hflip")
    if opts.speed and opts.speed != 1.0:
        tail.append(f"setpts=PTS/{opts.speed}")
    if opts.fade_seconds and meta["duration"] > 2 * opts.fade_seconds + 0.5:
        d = meta["duration"] / (opts.speed or 1.0)
        tail.append(f"fade=t=in:st=0:d={opts.fade_seconds}")
        tail.append(f"fade=t=out:st={max(0, d - opts.fade_seconds):.3f}:d={opts.fade_seconds}")
    tail.append("format=yuv420p")

    tail_str = ",".join(tail)
    filter_complex = (";".join(vf) + f";[{last}]{tail_str}[v]") if vf \
        else f"[0:v]{tail_str}[v]"

    # -- brand badge overlay ---------------------------------------------
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bo = opts.brand_overlay or {}
    badge = bo.get("image") or os.path.join("assets", "brand_badge.png")
    badge_abs = badge if os.path.isabs(badge) else os.path.join(_root, badge)
    extra_inputs: list[str] = []
    vmap = "[v]"
    if bo.get("enabled") and os.path.isfile(badge_abs):
        wpct = float(bo.get("width_pct", 26)) / 100.0
        mpct = float(bo.get("margin_pct", 2)) / 100.0
        opac = float(bo.get("opacity", 1.0))
        corner = (bo.get("corner") or "tl").lower()
        bw = max(16, int(w * wpct))
        mx = int(w * mpct)
        pos = {
            "tl": f"{mx}:{mx}",
            "tr": f"W-w-{mx}:{mx}",
            "bl": f"{mx}:H-h-{mx}",
            "br": f"W-w-{mx}:H-h-{mx}",
        }.get(corner, f"{mx}:{mx}")
        extra_inputs = ["-i", badge_abs]
        filter_complex += (
            f";[1:v]format=rgba,colorchannelmixer=aa={opac:.3f},"
            f"scale={bw}:-1[bd];[v][bd]overlay={pos}[vo]"
        )
        vmap = "[vo]"

    # -- audio: retime, optional gain, loudness normalise -------------------
    a_parts = []
    if opts.speed and opts.speed != 1.0:
        a_parts.append(f"atempo={opts.speed}")
    if opts.volume_gain_db:
        a_parts.append(f"volume={opts.volume_gain_db}dB")
    if opts.loudnorm:
        a_parts.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    a_filter = ["-filter:a", ",".join(a_parts)] if a_parts else []

    cmd = [
        FFMPEG, "-y", "-i", in_path, *extra_inputs,
        "-filter_complex", filter_complex,
        "-map", vmap, "-map", "0:a?",
        *a_filter,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        out_path,
    ]
    print("[video_editor] " + " ".join(shlex.quote(c) for c in cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not (os.path.isfile(out_path)
                                     and os.path.getsize(out_path) > 0):
            print("[video_editor] ffmpeg failed; using original:\n"
                  + r.stderr[-1500:])
            return in_path
    except Exception as exc:  # noqa: BLE001
        print(f"[video_editor] exception ({exc}); using original")
        return in_path
    return out_path
