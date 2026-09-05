# tiktok-yt-automation-1

Daily TikTok → YouTube Shorts pipeline for **one** channel, running entirely on
GitHub Actions + cron-job.org. Built from `Automation.md`.

- **Source TikTok:** `@kimvirtanen`
- **Target YouTube:** `@FurryFollies-v2p`
- **Schedule:** 2 uploads/day — 22:00 UTC (US 6 PM ET) and 00:00 UTC (US 8 PM ET)
- **Mode:** `popular_split` — slot 1 = newest unposted, slot 2 = most-viewed unposted
- **Per upload:** download watermark-free → verify audio → blur + light de-dup edit
  (`src/video_editor.py`) → AI title/description/tags (`src/seo.py`, Claude Haiku) →
  upload as public Short → record in `data/channel_5.db` (committed back) → Discord ping

## Layout

```
channels.yaml            THE config (schema: Automation.md §5)
run.py                   entry: python run.py --slot N --channel channel_5 [--dry-run]
reauth_nobrowser.py      mint the OAuth token (prints a URL, no auto browser)
src/
  config.py              channels.yaml loader + validation
  db.py                  SQLite state (posted_videos, runs) + per-day guard
  tiktok_downloader.py   yt-dlp: listing, format chain, download, ffprobe audio check
  video_editor.py        ffmpeg: watermark blur + zoom/speed/color de-dup pass
  seo.py                 AI (or template) title / description+hashtags / tags
  youtube_uploader.py    Data API v3 upload + token refresh
  channel_runner.py      per-channel pipeline + pickers
  orchestrator.py        loop channels for a slot, notify
  audit.py               python -m src.audit  — per-day health, independent of any portal
.github/workflows/
  upload-slot1.yml       workflow_dispatch only; fired by cron-job.org
  upload-slot2.yml
  daily-summary.yml      03:00 UTC Discord digest
  update-ytdlp.yml       06:00 UTC yt-dlp pin bump
```

## GitHub Secrets required

| Secret | What | Needed for |
|---|---|---|
| `CHANNEL_5_CLIENT_SECRET` | base64 of `credentials/channel_5_client_secret.json` | upload |
| `CHANNEL_5_TOKEN` | base64 of `tokens/channel_5_token.json` | upload |
| `DISCORD_WEBHOOK_URL` | one webhook for all notifications | notifications |
| `TIKTOK_COOKIES` | base64 of a Netscape `cookies.txt` | listing reliability (optional) |
| `ANTHROPIC_API_KEY` | Claude API key | AI SEO (optional; template fallback otherwise) |

## Local dry run

```
pip install -r requirements.txt
python run.py --slot 1 --channel channel_5 --dry-run
```

A dry run lists + picks + prints the SEO it *would* use, and never uploads. It still
writes a `runs` row, so use it sparingly against the live DB (Automation.md §12).

## cron-job.org — 5 jobs (US group)

| title | time UTC | workflow |
|---|---|---|
| `TikTok-YT Ch1 Slot1` | 22:00 | `upload-slot1.yml` |
| `TikTok-YT Ch1 Slot1 RETRY (+90m)` | 23:30 | `upload-slot1.yml` |
| `TikTok-YT Ch1 Slot2` | 00:00 | `upload-slot2.yml` |
| `TikTok-YT Ch1 Slot2 RETRY (+90m)` | 01:30 | `upload-slot2.yml` |
| `TikTok-YT Ch1 Daily Summary` | 03:00 | `daily-summary.yml` |

Each job: `POST https://api.github.com/repos/OWNER/REPO/actions/workflows/<file>/dispatches`,
headers `Accept: application/vnd.github.v3+json`, `Authorization: token <PAT>`,
body `{"ref":"main"}`. The per-day guard makes the RETRY jobs no-ops on success.
