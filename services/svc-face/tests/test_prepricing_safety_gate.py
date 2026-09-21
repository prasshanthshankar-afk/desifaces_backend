import asyncio

import pytest

from app.services.creator_orchestrator import CreatorOrchestrator


class _Safety:
    def check_keywords(self, text: str):
        if "gun" in text.lower() or "bloody" in text.lower():
            return False, (
                "PROMPT_POLICY_BLOCKED: Prompt needs changes. "
                "Blocked category: weapons."
            )
        return True, ""


def _orch():
    orch = object.__new__(CreatorOrchestrator)
    orch.safety_service = _Safety()

    async def _ensure(request_dict):
        return request_dict

    orch._ensure_required_config_codes = _ensure
    orch._normalize_request_framing = lambda request_dict: request_dict
    return orch


def test_pricing_preview_rejects_firearm_before_any_pricing_work():
    orch = _orch()

    with pytest.raises(ValueError, match="unsafe_prompt"):
        asyncio.run(
            orch._prepare_pricing_preview_request_dict(
                {
                    "mode": "text-to-image",
                    "user_prompt": "woman with gun and arms",
                }
            )
        )


def test_creator_submission_reuses_same_prepricing_safety_gate():
    orch = _orch()

    with pytest.raises(ValueError, match="unsafe_prompt"):
        asyncio.run(
            orch._prepare_creator_submission_request_dict(
                {
                    "mode": "text-to-image",
                    "user_prompt": "bloody woman portrait",
                }
            )
        )


def test_benign_arms_prompt_still_reaches_pricing_preparation():
    orch = _orch()

    request_dict, mode = asyncio.run(
        orch._prepare_pricing_preview_request_dict(
            {
                "mode": "text-to-image",
                "user_prompt": (
                    "woman with arms crossed, professional portrait, realistic lighting"
                ),
            }
        )
    )

    assert mode == "text-to-image"
    assert "arms crossed" in request_dict["user_prompt"]
