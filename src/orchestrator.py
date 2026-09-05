"""Loops the configured channels for one slot, collects results, notifies Discord."""
from __future__ import annotations

from . import channel_runner, notifier
from .config import Channel, load_channels


_EMOJI = {
    "success": "✅",
    "skipped": "⏭️",
    "no_content": "\U0001f4ed",
    "failed": "❌",
}


def run_slot(slot: int, channel_id: str | None = None, dry_run: bool = False,
             force: bool = False) -> int:
    channels = [c for c in load_channels() if c.enabled]
    if channel_id:
        channels = [c for c in channels if c.id == channel_id]
    if not channels:
        print(f"[orchestrator] no enabled channel matched {channel_id!r}")
        return 1

    exit_code = 0
    lines: list[str] = []
    for ch in channels:
        print(f"\n=== {ch.id} slot {slot} (dry_run={dry_run} force={force}) ===")
        try:
            res = channel_runner.run(ch, slot, dry_run=dry_run, force=force)
        except Exception as exc:  # noqa: BLE001 -- one channel must not kill the batch
            import traceback
            traceback.print_exc()
            lines.append(f"{_EMOJI['failed']} `{ch.id}` slot {slot} CRASHED: {exc}")
            exit_code = 1
            continue

        emo = _EMOJI.get(res.status, "❓")
        lines.append(f"{emo} `{ch.id}` slot {slot}: **{res.status}** -- {res.detail}")
        if res.status == "failed":
            exit_code = 1

    _dry = " (dry run)" if dry_run else ""
    notifier.send(f"**TikTok->YT slot {slot}{_dry}**\n" + "\n".join(lines))
    return exit_code
