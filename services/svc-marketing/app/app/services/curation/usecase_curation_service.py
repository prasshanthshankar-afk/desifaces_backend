# services/svc-marketing/app/app/services/curation/usecase_curation_service.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.config import settings
from app.repos.marketing_use_cases_repo import MarketingUseCasesRepo
from app.services.planning.llm_client import get_llm


class UseCaseCurationService:
    def __init__(self, repo: MarketingUseCasesRepo):
        self.repo = repo
        self.llm = get_llm()

    async def suggest_use_cases(
        self,
        created_by: UUID,
        persona: Optional[str],
        industry: Optional[str],
        recipe: Optional[str],
        season_event: Optional[str],
        tags: List[str],
        count: int,
    ) -> List[UUID]:
        if not settings.ENABLE_USECASE_CURATION:
            return []

        # RAG-lite: pull top approved use cases as examples (grounding)
        examples = await self.repo.list_candidates(
            persona=persona,
            industry=industry,
            tags=tags,
            season_event=season_event,
            recipe=recipe,
            limit=10,
            approved_only=True,
        )
        ex_list: List[Dict[str, Any]] = []
        for r in examples:
            ex_list.append(
                {
                    "use_case_id": str(r["use_case_id"]),
                    "persona": r["persona"],
                    "industry": r["industry"],
                    "recipe": r["recipe"],
                    "season_event": r["season_event"],
                    "tags": r["tags"],
                    "product_anchor": r["product_anchor"],
                    "default_hook": r["default_hook"],
                    "base_overlay_lines": r["base_overlay_lines"],
                    "base_script": r["base_script"],
                }
            )

        system = (
            "You are DesiFaces UseCase Curator.\n"
            "Goal: propose NEW, specific marketing use-cases for short IG promos (8–12s).\n"
            "Rules:\n"
            "- Must be concrete (persona+industry+anchor).\n"
            "- Anchor required: season_event OR default_offer OR product_anchor.\n"
            "- Do NOT invent metrics/numbers.\n"
            "- Output JSON ONLY as: {\"use_cases\": [ ... ]}\n"
        )

        user = (
            f"Constraints:\n"
            f"persona={persona}\nindustry={industry}\nrecipe={recipe}\nseason_event={season_event}\ntags={tags}\n"
            f"Need {count} suggestions.\n\n"
            f"Examples (ground truth):\n{json.dumps(ex_list, ensure_ascii=False, indent=2)}\n\n"
            "Return each suggestion with fields:\n"
            "- persona (creator|smb|user)\n"
            "- industry\n"
            "- recipe (FACE_AUDIO_VIDEO|FACE_MUSIC_MUSICVIDEO|FACE_CATALOG_PRODUCT_PROMO)\n"
            "- campaign_type\n"
            "- season_event (optional)\n"
            "- tags (list)\n"
            "- product_anchor (optional)\n"
            "- default_offer (optional)\n"
            "- default_seconds (6..15)\n"
            "- default_hook\n"
            "- base_overlay_lines (list)\n"
            "- base_script\n"
            "- default_music_prompt (optional)\n"
            "- required_assets_json (object)\n"
            "- notes\n"
        )

        schema_hint = (
            "{\"use_cases\": [{"
            "\"persona\": \"smb\", \"industry\": \"apparel\", \"recipe\": \"FACE_CATALOG_PRODUCT_PROMO\", "
            "\"campaign_type\": \"product_launch\", \"season_event\": null, \"tags\": [\"...\"], "
            "\"product_anchor\": \"...\", \"default_offer\": \"...\", \"default_seconds\": 10, "
            "\"default_hook\": \"...\", \"base_overlay_lines\": [\"...\"], \"base_script\": \"...\", "
            "\"default_music_prompt\": \"...\", \"required_assets_json\": {}, \"notes\": \"...\""
            "}]}",
        )

        payload = await self.llm.generate_json(system=system, user=user, schema_hint=schema_hint[0])
        use_cases = payload.get("use_cases") or []
        if not isinstance(use_cases, list):
            return []

        created_ids: List[UUID] = []
        for uc in use_cases[:count]:
            if not isinstance(uc, dict):
                continue

            # enforce at least one anchor
            if not (uc.get("season_event") or uc.get("default_offer") or uc.get("product_anchor")):
                continue

            new_id = uuid4()
            uc["parent_use_case_id"] = None
            await self.repo.insert_suggestion(use_case_id=new_id, created_by=created_by, payload=uc)
            created_ids.append(new_id)

        return created_ids