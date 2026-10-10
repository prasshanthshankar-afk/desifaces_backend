import array
import __future__
import ast
import importlib.util
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

MODULE = Path(__file__).parents[1] / "app/app/services/talking_video_finish.py"
SPEC = importlib.util.spec_from_file_location("talking_video_finish", MODULE)
finish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finish)


class EligibilityTest(unittest.TestCase):
    def test_only_authoritative_single_person_web_path(self):
        tags = dict(source_surface="web_video_studio", audio_is_authoritative=True,
                    longform_profile="talking_video", api_mode="legacy")
        self.assertTrue(finish.should_finish_single_person(tags, 1))
        self.assertFalse(finish.should_finish_single_person(tags, 2))
        for patch in ({"source_surface": "mobile"}, {"source_surface": "director"},
                      {"audio_is_authoritative": False}, {"longform_profile": "cinematic_video_direction"},
                      {"api_mode": "directed"}, {"workflow_id": "group"}, {"story_id": "story"}):
            self.assertFalse(finish.should_finish_single_person({**tags, **patch}, 1))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class MediaTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.audio = str(Path(self.folder.name) / "voice.wav")
        self.video = str(Path(self.folder.name) / "render.mp4")
        self.out = str(Path(self.folder.name) / "finished.mp4")
        samples = array.array("h", (int(14000 * math.sin(2 * math.pi * 440 * i / 48000))
                                  for i in range(round(1.2375 * 48000))))
        with wave.open(self.audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(samples.tobytes())

    def render(self, duration):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=blue:s=160x90:r=30:d={duration}", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", self.video], check=True, capture_output=True)

    def decode(self, path):
        data = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
                               "-ar", "48000", "-f", "f32le", "-"],
                              capture_output=True, check=True).stdout
        values = array.array("f")
        values.frombytes(data)
        return values

    def test_preserves_final_sound_and_adds_silent_tail(self):
        self.render(1.2)
        receipt = finish.finish_single_person_video(self.video, self.audio, self.out)
        self.assertGreaterEqual(receipt["final_audio_duration_sec"], 1.9375)
        self.assertGreaterEqual(receipt["final_video_duration_sec"], 1.9375)
        original, output = self.decode(self.audio), self.decode(self.out)
        # A deliberately audible ending verifies that the last speech samples
        # survive, rather than only asserting padded container duration.
        start, stop = int(1.15 * 48000), int(1.23 * 48000)
        x, y = original[start:stop], output[start:stop]
        dot = sum(a*b for a, b in zip(x, y))
        norm = math.sqrt(sum(a*a for a in x) * sum(b*b for b in y))
        self.assertGreater(dot / norm, 0.98)
        quiet = output[int(1.4 * 48000):int(1.9 * 48000)]
        self.assertTrue(quiet)
        self.assertLess(math.sqrt(sum(v*v for v in quiet) / len(quiet)), 0.001)

    def test_materially_missing_performance_fails_before_output(self):
        self.render(0.5)
        with self.assertRaisesRegex(RuntimeError, "RENDER_TOO_SHORT"):
            finish.finish_single_person_video(self.video, self.audio, self.out)
        self.assertFalse(Path(self.out).exists())

    def test_audio_alignment_mismatch_fails(self):
        self.render(3.0)
        with self.assertRaisesRegex(RuntimeError, "AUDIO_ALIGNMENT_MISMATCH"):
            finish.finish_single_person_video(self.video, self.audio, self.out)


class FinalizationTest(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, tags, finish_error=None):
        source = MODULE.with_name("longform_orchestrator.py").read_text()
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "stitch_if_ready")
        namespace = {"__name__": "test_finalization"}
        exec("from __future__ import annotations\nimport os, tempfile\nfrom typing import Any, Dict, List", namespace)
        states = SimpleNamespace(**{s: SimpleNamespace(value=s) for s in
                                  ("queued", "running", "stitching", "failed", "succeeded")})
        response = SimpleNamespace(content=b"original speech", raise_for_status=lambda: None)
        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client
        jobs = SimpleNamespace(set_counts=AsyncMock(), set_status=AsyncMock(),
                               merge_tags=AsyncMock(), set_final=AsyncMock(), get_job=AsyncMock(return_value=None))
        segs = SimpleNamespace(any_failed=AsyncMock(return_value=False), count_done=AsyncMock(return_value=1),
                               list_by_job=AsyncMock(return_value=[{"segment_index": 0, "segment_video_url": "https://video"}]))
        finish_call = Mock(return_value={"closing_pause_sec": 0.75}, side_effect=finish_error)
        commit = AsyncMock(return_value={"state": "committed"})
        upload = Mock(return_value=("output", "https://final"))
        namespace.update(LongformJobStatus=states, _safe_str=lambda v: str(v or ""),
                         _as_dict_loose=lambda v: v if isinstance(v, dict) else {},
                         _job_tags_dict=lambda row: row.get("tags", {}), _extract_pricing_view=lambda t: {},
                         _emit_notification_best_effort=AsyncMock(), release_longform_pricing_for_job=AsyncMock(),
                         commit_longform_pricing_for_job=commit, pricing_logger=Mock(),
                         httpx=SimpleNamespace(AsyncClient=lambda **kw: client),
                         compose_timeline=Mock(return_value={"duration_sec": 21.2}), upload_final_mp4=upload,
                         should_finish_single_person=finish.should_finish_single_person,
                         finish_single_person_video=finish_call)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "finalization", "exec",
                     flags=__future__.annotations.compiler_flag), namespace)
        row = dict(id="job", status="stitching", total_segments=1, tags=tags, user_id="user")
        if finish_error:
            with self.assertRaisesRegex(RuntimeError, "RENDER_TOO_SHORT"):
                await namespace["stitch_if_ready"](jobs, segs, None, row)
        else:
            await namespace["stitch_if_ready"](jobs, segs, None, row)
        return jobs, finish_call, commit, upload, client

    async def test_restores_source_before_upload_without_billing_tail(self):
        tags = dict(source_surface="web_video_studio", audio_is_authoritative=True,
                    longform_profile="talking_video", api_mode="legacy", voice_audio_url="https://original")
        jobs, finishing, commit, upload, client = await self.run_case(tags)
        finishing.assert_called_once()
        self.assertTrue(upload.call_args.args[0].endswith("finished.mp4"))
        self.assertEqual(commit.call_args.kwargs["final_duration_sec"], 21.2)
        jobs.merge_tags.assert_awaited_once()
        self.assertEqual(client.get.call_args_list[-1].args[0], "https://original")

    async def test_director_finalization_is_unchanged(self):
        jobs, finishing, commit, upload, client = await self.run_case({"source_surface": "director"})
        finishing.assert_not_called()
        self.assertTrue(upload.call_args.args[0].endswith("final.mp4"))
        jobs.merge_tags.assert_not_awaited()

    async def test_quality_failure_prevents_upload_billing_and_success(self):
        tags = dict(source_surface="web_video_studio", audio_is_authoritative=True,
                    longform_profile="talking_video", api_mode="legacy", voice_audio_url="https://original")
        jobs, finishing, commit, upload, client = await self.run_case(tags, RuntimeError("RENDER_TOO_SHORT"))
        upload.assert_not_called()
        commit.assert_not_awaited()
        jobs.set_final.assert_not_awaited()
        self.assertEqual(jobs.set_status.call_args.args[2], "failed")


if __name__ == "__main__":
    unittest.main()
