from __future__ import annotations

import json
import os
from typing import Any

from langchain_openai import ChatOpenAI

from df_contracts.v3.director import CreativeBrief, CreativeCritique, CreativeStoryPlan


_DIRECTOR_SYSTEM_PROMPT = """You are the desifaces Creative Director.
Create culturally aware, production-ready story plans without stereotyping people.
Preserve user intent, existing participant identity and continuity when context is supplied.
Never invent account IDs, media IDs, pricing, entitlements or provider capabilities.
Those are supplied and validated by deterministic tools outside the model.
Return only the requested structured schema.
"""

_CRITIC_SYSTEM_PROMPT = """You are the desifaces Creative Continuity Critic.
Evaluate story coherence, participant continuity, dialogue attribution, scene feasibility,
cultural sensitivity and whether the plan is sufficiently specified for downstream
Face, Audio and Fusion orchestration. Do not rewrite the story; return a structured
critique with actionable revision instructions.
"""


def _model_name() -> str:
    value = str(os.getenv("DF_DIRECTOR_LLM_MODEL") or "").strip()
    if not value:
        raise RuntimeError("DF_DIRECTOR_LLM_MODEL_required")
    return value


def _temperature() -> float:
    try:
        return float(os.getenv("DF_DIRECTOR_LLM_TEMPERATURE", "0.7"))
    except Exception:
        return 0.7


class OpenAICreativePlanner:
    """Initial provider adapter; graph/domain remain provider-neutral."""

    def __init__(self) -> None:
        self._model = ChatOpenAI(model=_model_name(), temperature=_temperature())
        self._structured = self._model.with_structured_output(CreativeStoryPlan, method="json_schema")

    async def plan(
        self,
        *,
        brief: CreativeBrief,
        retrieved_context: dict[str, Any],
        current_plan: CreativeStoryPlan | None = None,
        revision_feedback: str | None = None,
    ) -> CreativeStoryPlan:
        payload = {
            "creative_brief": brief.model_dump(mode="json"),
            "retrieved_context": retrieved_context,
            "current_plan": current_plan.model_dump(mode="json") if current_plan else None,
            "revision_feedback": revision_feedback,
        }
        result = await self._structured.ainvoke(
            [
                ("system", _DIRECTOR_SYSTEM_PROMPT),
                ("human", json.dumps(payload, ensure_ascii=False, default=str)),
            ]
        )
        return CreativeStoryPlan.model_validate(result)


class OpenAICreativeCritic:
    def __init__(self) -> None:
        self._model = ChatOpenAI(model=_model_name(), temperature=0)
        self._structured = self._model.with_structured_output(CreativeCritique, method="json_schema")

    async def critique(
        self,
        *,
        brief: CreativeBrief,
        plan: CreativeStoryPlan,
        retrieved_context: dict[str, Any],
    ) -> CreativeCritique:
        payload = {
            "creative_brief": brief.model_dump(mode="json"),
            "story_plan": plan.model_dump(mode="json"),
            "retrieved_context": retrieved_context,
        }
        result = await self._structured.ainvoke(
            [
                ("system", _CRITIC_SYSTEM_PROMPT),
                ("human", json.dumps(payload, ensure_ascii=False, default=str)),
            ]
        )
        return CreativeCritique.model_validate(result)
