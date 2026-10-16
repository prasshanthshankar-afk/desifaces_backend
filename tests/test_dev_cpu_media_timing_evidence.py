"""Tests for DEV-only CPU media evidence collector (no real media required)."""
import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "dev_cpu_media_timing_evidence.py"
spec = importlib.util.spec_from_file_location("dev_cpu_media_timing_evidence", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class EvidenceTests(TestCase):
    def setUp(self):
        self.manifest = {
            "attempt_id": "test-only",
            "turns": [{"turn": i, "speaker_id": "A" if i % 2 else "B",
                       "video": f"/missing-{i}.mp4"} for i in range(1, 7)],
        }

    @patch.object(module.os, "uname")
    def test_requires_dev(self, uname):
        uname.return_value.nodename = "desifaces-gpu"
        with self.assertRaisesRegex(RuntimeError, "DEV_HOST_REQUIRED"):
            module.collect(self.manifest)

    @patch.object(module.os, "uname")
    def test_missing_files_fail_closed(self, uname):
        uname.return_value.nodename = "desifaces-dev"
        evidence = module.collect(self.manifest)
        self.assertEqual(evidence["decision"], "REVIEW_REQUIRED")
        self.assertEqual(len(evidence["turns"]), 6)
        self.assertTrue(all(t["reason"] == "VIDEO_NOT_FOUND" for t in evidence["turns"]))

    @patch.object(module.os, "uname")
    def test_rejects_out_of_order_turns(self, uname):
        uname.return_value.nodename = "desifaces-dev"
        self.manifest["turns"][0]["turn"] = 2
        with self.assertRaisesRegex(ValueError, "EXACTLY_SIX_ORDERED_TURNS_REQUIRED"):
            module.collect(self.manifest)

    @patch.object(module.os, "uname")
    @patch.object(module.Path, "is_file", return_value=True)
    @patch.object(module, "probe")
    def test_reports_track_offsets_without_claiming_lipsync(self, probe, is_file, uname):
        uname.return_value.nodename = "desifaces-dev"
        probe.return_value = {"duration_s": 1.3, "tracks": {
            "audio": {"start_time_s": 0.0, "duration_s": 1.0},
            "video": {"start_time_s": 0.0, "duration_s": 1.3},
        }}
        evidence = module.collect(self.manifest)
        self.assertEqual(evidence["decision"], "REVIEW_REQUIRED")
        self.assertEqual(evidence["turns"][0]["video_minus_audio_duration_ms"], 300.0)
        self.assertEqual(evidence["turns"][0]["track_start_difference_ms"], 0.0)
        self.assertEqual(evidence["turns"][0]["reason"], "VISUAL_AV_SYNC_NOT_MEASURED")
