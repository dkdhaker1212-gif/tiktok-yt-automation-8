#!/usr/bin/env python3
"""Entry point.

    python run.py --slot 1 --channel channel_1 [--dry-run]

Called by .github/workflows/upload-slotN.yml on the runner.
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from src.orchestrator import run_slot


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, required=True, choices=(1, 2))
    ap.add_argument("--channel", default=None, help="channel id, e.g. channel_1")
    ap.add_argument("--dry-run", action="store_true",
                    help="list + pick + 'would upload', but never actually upload")
    ap.add_argument("--force", action="store_true",
                    help="ignore the per-day guard (post even if this slot already ran)")
    args = ap.parse_args()
    return run_slot(args.slot, channel_id=args.channel, dry_run=args.dry_run,
                    force=args.force)


if __name__ == "__main__":
    sys.exit(main())
