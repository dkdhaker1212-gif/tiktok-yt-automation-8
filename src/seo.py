"""AI-generated YouTube SEO: clickbait title, description (+hashtags), tags,
plus a short thumbnail hook.

Preferred path: send a few video frames + the audio to Gemini, which describes
what actually happens and writes punchy, UNIQUE, click-driving metadata.
Falls back to Anthropic, then to a deterministic template, so a missing key
never drops a slot. Every title is checked against the channel's recently used
titles and regenerated / mutated if it collides.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field

_MODEL = os.environ.get("SEO_MODEL", "claude-haiku-4-5-20251001")
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


@dataclass
class Seo:
    title: str
    description: str
    tags: list[str]
    thumb_hook: str = ""
    used_titles: list[str] = field(default_factory=list)


_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
         "this", "that", "it", "is", "are", "my", "your", "you", "we", "so"}


def _keywords(text: str, n: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z']+", (text or "").lower())
    seen: list[str] = []
    for w in words:
        if len(w) > 2 and w not in _STOP and w not in seen:
            seen.append(w)
        if len(seen) >= n:
            break
    return seen


def _norm(t: str) -> str:
    t = (t or "").lower().replace("#shorts", "")
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _is_dupe(title: str, recent: list[str]) -> bool:
    n = _norm(title)
    if not n:
        return True
    for r in recent or []:
        rn = _norm(r)
        if not rn:
            continue
        if n == rn:
            return True
        # near-identical: >=85% word overlap both ways
        a, b = set(n.split()), set(rn.split())
        if a and b and len(a & b) / max(len(a), len(b)) >= 0.85:
            return True
    return False


_HOOK_POOL = [
    "Wait For The End", "This Took A Turn", "Nobody Expected This",
    "It Only Gets Better", "You Have To See This", "This Is Not Normal",
    "How Is This Real", "The Last Part Though", "Watch Till The End",
    "This Caught Everyone Off Guard",
]
_HOOK_POOL_ES = [
    "Espera Hasta El Final", "Esto Dio Un Giro", "Nadie Se Esperaba Esto",
    "Cada Vez Mejora Mas", "Tienes Que Ver Esto", "Esto No Es Normal",
    "Como Es Esto Real", "Mira La Ultima Parte", "Miralo Hasta El Final",
    "Esto Sorprendio A Todos",
]
_LANG_NAMES = {"en": "English", "es": "Spanish"}
_LANG_HOOKS = {"en": _HOOK_POOL, "es": _HOOK_POOL_ES}
_LANG_HASHTAGS = {
    "en": ["#shorts", "#viral", "#trending", "#fyp", "#usa", "#foryou",
           "#reels", "#viralvideo"],
    "es": ["#shorts", "#viral", "#tendencia", "#paraty", "#satisfactorio",
           "#fyp", "#reels", "#viralvideo"],
}


def _mutate(title: str, recent: list[str], language: str = "en") -> str:
    """Guaranteed-unique-ish fallback: prepend an unused hook."""
    base = title.split("#")[0].strip(" -|")
    used = {_norm(r) for r in (recent or [])}
    for h in _LANG_HOOKS.get(language, _HOOK_POOL):
        cand = f"{h}: {base}"
        if _norm(cand) not in used:
            return cand[:98]
    return f"{base} (take {abs(hash(base)) % 999})"[:98]


# --------------------------------------------------------------------------- #
_GEMINI_PROMPT = """You are a top YouTube Shorts strategist for a faceless channel.
You are given a few frames and/or the audio of ONE short vertical video.

First silently work out what literally happens in the clip (subject, action,
the single most surprising or satisfying beat). Then write metadata that makes a
viewer stop scrolling. Be bold and curiosity-driven ("clickbait" energy) but
NEVER promise something the clip does not deliver.

Write the title, description, tags and hashtags in {language_name} - natural,
native-sounding phrasing a real {language_name} speaker would write, NOT a
literal translation. Universal tags like #shorts/#viral may stay in English if
that is what real {language_name}-speaking creators use.

Return ONLY minified JSON with keys:
"title","description","tags","hashtags","thumb_hook"
- title: 40-90 chars, in {language_name}. A strong curiosity/hook opening,
  plain conversational tone, no ALL-CAPS words, no lies. End with " #Shorts".
  It MUST be clearly different in wording AND structure from every title in
  ALREADY_USED below - different hook, different phrasing, do not just swap a
  word. Vary how you open (question / bold claim / "POV" / number / "nobody...").
- description: in {language_name}. line 1 = a punchy hook. blank line. 1-2
  short lines of context. blank line. then 6-8 hashtags on one line. No links.
  <= 500 chars.
- tags: 18-25 lowercase search phrases in {language_name}, most specific
  first, no "#".
- hashtags: 8 strings starting with "#", lowercase.
- thumb_hook: 2-4 words in {language_name}, punchy, no emojis, no hashtags -
  goes big on the thumbnail. Different from the title's opening words.

SOURCE_CAPTION: {caption}
ALREADY_USED (do NOT repeat or lightly reword any of these):
{recent}
"""


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _extract_audio(media_path, out_m4a):
    try:
        r = _run([FFMPEG, "-y", "-i", media_path, "-vn", "-ac", "1", "-ar",
                  "16000", "-c:a", "aac", "-b:a", "64k", out_m4a], timeout=300)
        ok = os.path.isfile(out_m4a) and os.path.getsize(out_m4a) > 1000
        if not ok:
            print(f"[seo] audio extract rc={r.returncode}: {(r.stderr or '')[-200:]}")
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"[seo] audio extract failed: {exc}")
        return False


def _extract_frames(media_path, prefix, n=3):
    """Grab n small JPEG frames spread across the clip. Returns list of paths."""
    dur = 0.0
    try:
        r = _run([FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
                  "format=duration", "-of", "csv=p=0", media_path], timeout=60)
        dur = float((r.stdout or "0").strip() or 0)
    except Exception:
        dur = 0.0
    if dur <= 0:
        dur = 8.0
    out = []
    for i in range(n):
        t = dur * (i + 1) / (n + 1)
        p = f"{prefix}.f{i}.jpg"
        try:
            _run([FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", media_path, "-frames:v",
                  "1", "-q:v", "6", "-vf", "scale=360:-2", p], timeout=90)
            if os.path.isfile(p) and os.path.getsize(p) > 500:
                out.append(p)
        except Exception as exc:  # noqa: BLE001
            print(f"[seo] frame {i} failed: {exc}")
    return out


def _gemini(media_path, caption, base_tags, recent_titles, language="en"):
    import base64 as _b64
    import time
    import urllib.error
    import urllib.request

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not (key and media_path and os.path.isfile(media_path)):
        return None

    aud = media_path + ".seo.m4a"
    frames = []
    parts = []
    try:
        recent = "\n".join(f"- {t[:90]}" for t in (recent_titles or [])[:30]) or "(none)"
        lang_name = _LANG_NAMES.get(language, "English")
        parts.append({"text": _GEMINI_PROMPT
                      .replace("{caption}", (caption or "(none)")[:400])
                      .replace("{recent}", recent)
                      .replace("{language_name}", lang_name)})

        frames = _extract_frames(media_path, media_path + ".seo", n=3)
        for fp in frames:
            b = _b64.b64encode(open(fp, "rb").read()).decode()
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b}})

        if _extract_audio(media_path, aud) and os.path.getsize(aud) < 18 * 1024 * 1024:
            ab = _b64.b64encode(open(aud, "rb").read()).decode()
            parts.append({"inline_data": {"mime_type": "audio/mp4", "data": ab}})

        if len(parts) == 1:          # neither frames nor audio -> nothing to see
            return None

        body = json.dumps({
            "contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.9, "maxOutputTokens": 2200},
        }).encode()
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{_GEMINI_MODEL}:generateContent?key={key}")
        import socket
        resp = None
        waits = [0, 10, 25, 50, 90]
        for attempt, wait in enumerate(waits, start=1):
            if wait:
                time.sleep(wait)
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"})
            try:
                resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
                break
            except urllib.error.HTTPError as he:
                if he.code in (429, 500, 502, 503) and attempt < len(waits):
                    print(f"[seo] Gemini {he.code}, retry {attempt}")
                    continue
                raise
            except (socket.timeout, urllib.error.URLError, TimeoutError) as te:
                if attempt < len(waits):
                    print(f"[seo] Gemini timeout ({te}), retry {attempt}")
                    continue
                raise
        if resp is None:
            return None
        rparts = resp["candidates"][0]["content"]["parts"]
        raw = "".join(p["text"] for p in rparts if isinstance(p.get("text"), str))
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        i, j = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[i:j + 1] if i != -1 and j != -1 else raw)

        title = str(data.get("title", "")).strip()[:100]
        desc = str(data.get("description", "")).strip()[:4900]
        tags = [str(t).strip().lower().lstrip("#") for t in data.get("tags", []) if t]
        hashtags = [str(h).strip() for h in data.get("hashtags", []) if h]
        hook = str(data.get("thumb_hook", "")).strip()[:40]
        if hashtags and "#" not in desc:
            desc = (desc + "\n\n" + " ".join(hashtags[:8])).strip()
        tags = list(dict.fromkeys([*tags, *base_tags]))[:30]
        if not title or not tags:
            return None
        print("[seo] Gemini metadata OK")
        return Seo(title, desc, tags, thumb_hook=hook)
    except Exception as exc:  # noqa: BLE001
        print(f"[seo] Gemini failed ({exc}); falling back")
        return None
    finally:
        for p in [aud, *frames]:
            try:
                os.remove(p)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
_ANTHROPIC_PROMPT = """You are a YouTube Shorts SEO expert. Given a TikTok video's
caption and its original hashtags, produce click-driving YouTube metadata.

Write the title, description and tags in {language_name} - natural,
native-sounding phrasing, NOT a literal translation.

Return ONLY minified JSON with keys: "title","description","tags","thumb_hook".
- title: <= 90 chars, in {language_name}, punchy and curiosity-driven, no lies,
  end with " #Shorts" if is_short is true. It MUST be clearly different in
  wording and structure from every entry in ALREADY_USED.
- description: in {language_name}. hook line, blank line, 1-2 context lines,
  blank line, 6-8 hashtags.
- tags: 15-20 lowercase phrases in {language_name}, most specific first, no "#".
- thumb_hook: 2-4 punchy words in {language_name} for the thumbnail, no emojis.
Caption: {caption}
Original hashtags: {hashtags}
is_short: {is_short}
ALREADY_USED:
{recent}
"""


def _fallback(caption, tiktok_tags, base_tags, is_short, recent_titles=None,
              language="en"):
    cap = (caption or "").strip().replace("\n", " ")
    cap_clean = re.sub(r"#\S+", "", cap).strip(" .,-|")
    kws = _keywords(cap) or base_tags[:5] or ["viral", "trending"]

    # a caption that is just the uploader handle / a single junk token is useless
    _junk_cap = (" " not in cap_clean) or (len(cap_clean.split()) < 2)
    if len(cap_clean) < 12 or _junk_cap:
        base = " ".join((base_tags[:6] or kws[:6])).title()
        # make it read like a real title, not a tag dump: lead with a hook
        _hooks = _LANG_HOOKS.get(language, _HOOK_POOL)
        _used0 = {_norm(r) for r in (recent_titles or [])}
        for h in _hooks:
            cand = f"{h}: {base}"
            if _norm(cand) not in _used0:
                base = cand
                break
    else:
        base = cap_clean[:84].rstrip(" .,-")
    # rotate a distinct opener that has not been used recently
    used = {_norm(r) for r in (recent_titles or [])}
    title = base
    if _norm(title) in used:
        for h in _LANG_HOOKS.get(language, _HOOK_POOL):
            cand = f"{h}: {base}"
            if _norm(cand) not in used:
                title = cand
                break
    if is_short and "#shorts" not in title.lower():
        title = (title[:88] + " #Shorts").strip()

    tags = list(dict.fromkeys(
        [*base_tags, *[t.lower() for t in tiktok_tags], *kws,
         "shorts", "viral", "trending", "fyp"]))[:22]
    hs = (["#shorts"] if is_short else []) + _LANG_HASHTAGS.get(language, _LANG_HASHTAGS["en"])
    desc_lead = base if (_junk_cap or len(cap_clean) < 12) else cap_clean[:150]
    desc = "\n".join(filter(None, [
        desc_lead,
        "", " ".join(dict.fromkeys(hs))[:120],
    ])).strip()
    _hook_src = (base_tags[:3] or kws[:3]) if (_junk_cap or len(cap_clean) < 12) \
        else [w for w in base.split() if not w.endswith(":")]
    hook = " ".join(" ".join(_hook_src).split()[:3]).upper()
    return Seo(title[:100], desc[:4900], tags, thumb_hook=hook)


def _finish_title(title, is_short, recent, language="en"):
    if _is_dupe(title, recent):
        title = _mutate(title, recent, language)
    if is_short and "#shorts" not in title.lower() and len(title) <= 91:
        title = (title + " #Shorts").strip()
    return title[:100]


def generate(caption, tiktok_tags, base_tags, is_short, media_path=None,
             recent_titles=None, language="en"):
    recent_titles = recent_titles or []

    if os.environ.get("GEMINI_API_KEY", "").strip() and media_path:
        g = _gemini(media_path, caption, base_tags, recent_titles, language)
        if g and _is_dupe(g.title, recent_titles):        # one firm retry
            print("[seo] Gemini title collided; retrying once")
            g2 = _gemini(media_path, caption,
                         base_tags, recent_titles + [g.title], language)
            if g2:
                g = g2
        if g:
            g.title = _finish_title(g.title, is_short, recent_titles, language)
            return g

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            msg = client.messages.create(
                model=_MODEL, max_tokens=800,
                messages=[{"role": "user", "content": _ANTHROPIC_PROMPT.format(
                    caption=(caption or "")[:800],
                    hashtags=", ".join(tiktok_tags[:20]) or "(none)",
                    is_short=str(bool(is_short)).lower(),
                    recent="\n".join(f"- {t[:90]}" for t in recent_titles[:30])
                    or "(none)",
                    language_name=_LANG_NAMES.get(language, "English"),
                )}],
            )
            raw = "".join(b.text for b in msg.content
                          if getattr(b, "type", "") == "text")
            raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            data = json.loads(raw)
            title = str(data.get("title", "")).strip()[:100]
            desc = str(data.get("description", "")).strip()[:4900]
            tags = [str(t).strip().lower().lstrip("#")
                    for t in data.get("tags", []) if t]
            tags = list(dict.fromkeys([*base_tags, *tags]))[:25]
            hook = str(data.get("thumb_hook", "")).strip()[:40]
            if not title or not tags:
                raise ValueError("empty title/tags")
            title = _finish_title(title, is_short, recent_titles, language)
            print(f"[seo] AI metadata OK ({_MODEL})")
            return Seo(title, desc, tags, thumb_hook=hook)
        except Exception as exc:  # noqa: BLE001
            print(f"[seo] Anthropic failed ({exc}); template fallback")

    return _fallback(caption, tiktok_tags, base_tags, is_short, recent_titles, language)
