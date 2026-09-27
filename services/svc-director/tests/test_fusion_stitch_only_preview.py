from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app import fusion_execution_parent_pricing as target
from app.fusion_execution import SceneFusionBridgeError


class _Store:
    async def assert_startable(self, conn, *, stage_run_id):
        return None


class _ParentPricing:
    async def preview(self, *, headers, context):
        return {
            "pricing": {
                "unit_type": "minute",
                "quote_id": "quote-stitch-only",
                "preview_fingerprint": "fp-stitch-only",
            },
            "pricing_summary": {"display_estimate": "$1.40"},
        }


def test_stitch_only_preview_does_not_resolve_face_or_audio(monkeypatch):
    async def run():
        account_id = uuid4()
        workflow_id = uuid4()
        stage_run_id = uuid4()
        turn_id = uuid4()

        context = SimpleNamespace(
            stage_state="failed",
            turns=(SimpleNamespace(dialogue_turn_id=turn_id),),
            project_id=uuid4(),
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
        )

        async def fake_context(*args, **kwargs):
            return context

        async def fake_latest(*args, **kwargs):
            return {
                "metadata_json": {
                    "children": [
                        {
                            "dialogue_turn_id": str(turn_id),
                            "status": "succeeded",
                            "video_url": "https://example.invalid/existing.mp4",
                            "fusion_job_id": "existing-job-1",
                            "sequence_no": 1,
                        }
                    ]
                }
            }

        async def forbidden_compile(**kwargs):
            raise AssertionError(
                "stitch-only preview must not resolve Face/Audio or compile child generation inputs"
            )

        monkeypatch.setattr(target, "load_fusion_scene_context", fake_context)
        monkeypatch.setattr(target, "_latest_attempt", fake_latest)
        monkeypatch.setattr(target, "_compile_children", forbidden_compile)

        service = target.ParentPricedSceneFusionExecutionService(
            face_base_url="http://face.invalid",
            audio_base_url="http://audio.invalid",
            fusion_base_url="http://fusion.invalid",
            fusion_extension_base_url="http://fusion-extension.invalid",
            store=_Store(),
        )
        service.parent_pricing = _ParentPricing()

        _, bundle = await service.preview(
            object(),
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers={"Authorization": "Bearer test"},
            external_provider_ok=True,
        )

        assert bundle["preserved_child_count"] == 1
        assert bundle["required_child_count"] == 0
        assert bundle["children"] == []
        assert bundle["billable_parent_quote_count"] == 1
        assert bundle["billable_child_quote_count"] == 0
        assert bundle["parent"]["pricing"]["quote_id"] == "quote-stitch-only"

    asyncio.run(run())


def test_stitch_only_preview_rejects_preserved_child_from_old_dialogue(monkeypatch):
    async def run():
        account_id = uuid4()
        workflow_id = uuid4()
        stage_run_id = uuid4()
        current_turn = uuid4()
        stale_turn = uuid4()

        context = SimpleNamespace(
            stage_state="failed",
            turns=(SimpleNamespace(dialogue_turn_id=current_turn),),
            project_id=uuid4(),
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
        )

        async def fake_context(*args, **kwargs):
            return context

        async def fake_latest(*args, **kwargs):
            return {
                "metadata_json": {
                    "children": [
                        {
                            "dialogue_turn_id": str(stale_turn),
                            "status": "succeeded",
                            "video_url": "https://example.invalid/stale.mp4",
                            "fusion_job_id": "old-job",
                            "sequence_no": 1,
                        }
                    ]
                }
            }

        monkeypatch.setattr(target, "load_fusion_scene_context", fake_context)
        monkeypatch.setattr(target, "_latest_attempt", fake_latest)

        service = target.ParentPricedSceneFusionExecutionService(
            face_base_url="http://face.invalid",
            audio_base_url="http://audio.invalid",
            fusion_base_url="http://fusion.invalid",
            fusion_extension_base_url="http://fusion-extension.invalid",
            store=_Store(),
        )
        service.parent_pricing = _ParentPricing()

        try:
            await service.preview(
                object(),
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers={"Authorization": "Bearer test"},
                external_provider_ok=True,
            )
        except SceneFusionBridgeError as exc:
            assert str(exc) == "fusion_preserved_child_lineage_mismatch"
        else:
            raise AssertionError("stale preserved child lineage must fail closed")

    asyncio.run(run())
