"""Execute the production planner with lightweight domain/service dependencies."""
import ast
import __future__
import math
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).parents[1] / "app/app/services/longform_orchestrator.py"


class Model(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


class FullAudioPlanTest(unittest.TestCase):
    def plan(self, duration, requested=None, quality="economy", provider="veed_fabric", cap=30, script_seconds=None):
        namespace = {"os": os, "math": math, "re": re}
        functions = ["build_talking_video_segment_payloads", "_assign_audio_windows_to_rows",
                     "_split_text_into_n_chunks", "_safe_segment_seconds", "_talking_required_duration_seconds"]
        nodes = [n for n in ast.parse(SOURCE.read_text()).body
                 if isinstance(n, ast.FunctionDef) and n.name in functions]
        intent = Model(constraints=SimpleNamespace(aspect_ratios=["16:9"]), meta={}, goal="Saved Voice audio",
                       assets=SimpleNamespace(face_artifact_id="face", voice_audio_artifact_id="audio", logo_url=None,
                                              image_urls=[], video_urls=[], screenshot_urls=[]),
                       mode=SimpleNamespace(value="legacy"))
        voice = dict(duration_sec=duration, audio_url="https://original", audio_artifact_id="audio")
        namespace.update(
            normalize_request_to_intent=lambda p: intent,
            _as_dict_loose=lambda v: v if isinstance(v, dict) else {},
            _safe_str=lambda v: str(v or ""), _safe_int=lambda v, default=0: int(v) if v else default,
            _quality_tier=lambda p, t: quality, _provider_hint=lambda p, t: provider,
            _effective_voice_audio_source=lambda p: voice,
            _script_text_from_payload=lambda p: "one two three four five six seven eight nine ten",
            _requested_duration_hint_seconds=lambda p, t: duration if requested is None else requested,
            _economy_effective_duration_seconds=lambda p, t: min(cap, duration or script_seconds or 1),
            _estimate_text_duration_seconds=lambda text: script_seconds or 1,
            estimate_longform_duration_seconds=lambda p: duration,
            _single_segment_provider_cap_seconds=lambda p: cap,
            _attach_voice_audio_metadata=lambda target, voice: dict(target),
            choose_scenario_plan=lambda i: Model(scenario_type=SimpleNamespace(value="brand_film")),
            RenderRoute=SimpleNamespace(fusion=SimpleNamespace(value="fusion")),
            SegmentStatus=SimpleNamespace(queued=SimpleNamespace(value="queued")),
            ShotType=SimpleNamespace(talking_head="talking_head"), ShotSpec=Model, ScriptSpec=Model,
            TimelineManifest=Model, QcDecision=SimpleNamespace(accept=SimpleNamespace(value="accept")),
            LongformStage=SimpleNamespace(shot_planning=SimpleNamespace(value="shot_planning")))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "planner", "exec",
                     flags=__future__.annotations.compiler_flag), namespace)
        return namespace["build_talking_video_segment_payloads"]({})

    def test_audio_beyond_cap_is_fully_covered_without_gaps(self):
        for duration in [22, 30, 31, 45, 60, 90, 125]:
            with self.subTest(duration=duration):
                result = self.plan(duration)
                rows = result["segments"]
                self.assertEqual(sum(row["duration_sec"] for row in rows), duration)
                self.assertTrue(all(row["duration_sec"] <= 30 for row in rows))
                if duration > 30:
                    self.assertGreater(len(rows), 1)
                windows = result["job_tags"]["directed_plan"]["segments_by_index"]
                cursor = 0
                for i in range(len(rows)):
                    self.assertEqual(windows[str(i)]["audio_start_sec"], cursor)
                    cursor = windows[str(i)]["audio_end_sec"]
                self.assertEqual(cursor, duration)

    def test_source_audio_longer_than_request_is_not_capped(self):
        result = self.plan(90, requested=22)
        self.assertEqual(sum(r["duration_sec"] for r in result["segments"]), 90)

    def test_script_estimate_over_cap_creates_multiple_children(self):
        result = self.plan(0, requested=22, script_seconds=65)
        self.assertEqual(sum(r["duration_sec"] for r in result["segments"]), 65)
        self.assertEqual(result["segments_total"], 3)

    def test_premium_and_custom_provider_caps_remain_safe(self):
        for quality, provider, cap in [("premium", "kling", 30), ("economy", "veed_fabric", 20)]:
            result = self.plan(65, quality=quality, provider=provider, cap=cap)
            self.assertEqual(sum(r["duration_sec"] for r in result["segments"]), 65)
            self.assertTrue(all(r["duration_sec"] <= cap for r in result["segments"]))

    def test_preview_and_reservation_use_full_duration_before_bucket_selection(self):
        source = SOURCE.read_text()
        tree = ast.parse(source)
        for name in ["build_longform_pricing_preview_spec", "reserve_longform_pricing_for_job"]:
            function = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
            body = ast.get_source_segment(source, function)
            self.assertIn("requested_for_pricing = max(requested_for_pricing, _talking_required_duration_seconds(payload, tags))", body)
            self.assertLess(body.index("requested_for_pricing = max"), body.index("segmented = requested_for_pricing > provider_limit_sec"))


if __name__ == "__main__":
    unittest.main()
