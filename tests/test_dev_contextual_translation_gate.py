"""No synthesis occurs without a reviewed contextual translation."""
import importlib.util
import sys
from pathlib import Path
import pytest

p=Path(__file__).resolve().parents[1]/"services/svc-audio/app/app/services/dev_contextual_translation_gate.py"
spec=importlib.util.spec_from_file_location("dev_contextual_translation_gate",p)
m=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=m
spec.loader.exec_module(m)


@pytest.mark.asyncio
async def test_rejected_possession_is_repaired_and_reviewed():
    calls=[]
    async def clarify(original,previous,next_turn):
        calls.append(("clarify",next_turn))
        if "Review feedback:" in next_turn:
            return "Yes, I have seen the announcements."
        return "I have."
    async def translate(meaning):
        calls.append(("translate",meaning))
        return "मेरे पास है!" if meaning=="I have." else "हाँ, मैंने घोषणाएँ देखी हैं!"
    async def review(original,candidate,previous,next_turn,meaning):
        calls.append(("review",candidate))
        return m.Review("देखी हैं" in candidate,"possession error")
    result=await m.contextual_translation_with_review(
        original="I have!",previous_turn="Did you see the announcements?",
        next_turn="",clarify_meaning=clarify,provider_translate=translate,
        independent_review=review)
    assert result.attempts==2
    assert result.text=="हाँ, मैंने घोषणाएँ देखी हैं!"
    assert [x[0] for x in calls].count("review")==2


@pytest.mark.asyncio
async def test_two_repair_limit_and_never_accept_failure():
    count=0
    async def clarify(a,b,c): return "I have seen the announcements."
    async def translate(a): return "मेरे पास है!"
    async def review(*args):
        nonlocal count
        count+=1
        return m.Review(False,"meaning not preserved")
    with pytest.raises(m.TranslationReviewRequired,match="TRANSLATION_REVIEW_REQUIRED"):
        await m.contextual_translation_with_review(
            original="I have!",previous_turn="Did you see the announcements?",
            next_turn="",clarify_meaning=clarify,provider_translate=translate,
            independent_review=review)
    assert count==3


@pytest.mark.asyncio
async def test_review_unavailable_is_fail_closed():
    async def clarify(*args): return "I saw it."
    async def translate(*args): return "देख लिया"
    async def review(*args): raise RuntimeError("timeout")
    with pytest.raises(m.TranslationReviewRequired,match="independent_review_unavailable"):
        await m.contextual_translation_with_review(
            original="I have!",previous_turn="Did you see it?",next_turn="",
            clarify_meaning=clarify,provider_translate=translate,independent_review=review)


@pytest.mark.asyncio
async def test_invalid_repair_limit_rejected():
    async def unused(*args): raise AssertionError("should not be called")
    with pytest.raises(m.TranslationReviewRequired,match="invalid_translation_contract"):
        await m.contextual_translation_with_review(
            original="Hi",previous_turn="",next_turn="",clarify_meaning=unused,
            provider_translate=unused,independent_review=unused,max_repairs=3)
