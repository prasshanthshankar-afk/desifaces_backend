from __future__ import annotations

import asyncio
import json

from app.repos.music_style_presets_repo import MusicStylePresetsRepo
from app.services.shot_cookbook_generator import generate_shot_cookbook_from_preset_row


async def main() -> None:
    repo = MusicStylePresetsRepo()
    presets = await repo.list_presets_for_cookbook_backfill()

    updated = 0
    skipped = 0

    for p in presets:
        v = int(p.get("shot_cookbook_version") or 0)
        if v >= 1 and p.get("shot_cookbook_json"):
            skipped += 1
            continue

        cookbook = generate_shot_cookbook_from_preset_row(preset=p, cookbook_version=1)
        await repo.update_shot_cookbook(
            preset_id=p["id"],
            cookbook_version=1,
            cookbook_json=cookbook,
        )
        updated += 1

    print(json.dumps({"updated": updated, "skipped": skipped, "total": len(presets)}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())