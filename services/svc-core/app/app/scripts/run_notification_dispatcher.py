from __future__ import annotations

import argparse
import asyncio
import json

from app.services.notification_dispatcher import get_notification_dispatcher


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bounded notification dispatch pass.")
    parser.add_argument("--channel", default=None, help="Optional channel filter: push or email")
    parser.add_argument("--limit", type=int, default=200, help="Maximum deliveries to process in one run")
    args = parser.parse_args()

    dispatcher = await get_notification_dispatcher()
    summary = await dispatcher.dispatch_once(
        channel=args.channel,
        limit=max(1, min(int(args.limit), 1000)),
    )
    print(json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
