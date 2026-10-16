import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "strict_lipsync_evidence_gate.py"
spec = importlib.util.spec_from_file_location("qc", path)
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)

def valid_turn(i):
    return dict(turn=i, speaker_id="A" if i % 2 else "B",
        evaluator_validated=True, confidence_adequate=True,
        median_absolute_av_offset_ms=20, p95_absolute_av_offset_ms=50,
        speech_complete=True, no_perceptible_lag=True,
        no_progressive_drift=True, correct_speaker=True, boundaries_clean=True)

def valid_report():
    return dict(turns=[valid_turn(i) for i in range(1,7)],
        final_stitch_av_verified=True, transition_review_passed=True,
        pricing_regressions_passed=True, audio_identity_preserved=True)

def test_validated_complete_evidence_passes():
    assert qc.evaluate(valid_report())["decision"] == "PASS"

def test_audio_offset_alone_never_passes():
    assert qc.evaluate({"turns": [{"turn":i,"source_to_embedded_audio_offset_ms":300}
                                  for i in range(1,7)]})["decision"] != "PASS"

def test_missing_visual_assessment_blocks():
    report=valid_report()
    report["turns"][0]["evaluator_validated"]=False
    assert qc.evaluate(report)["decision"] != "PASS"

def test_perceptible_lag_blocks_even_good_metric():
    report=valid_report()
    report["turns"][1]["no_perceptible_lag"]=False
    assert qc.evaluate(report)["decision"] != "PASS"

def test_any_turn_over_threshold_blocks():
    report=valid_report()
    report["turns"][5]["p95_absolute_av_offset_ms"]=110
    assert qc.evaluate(report)["decision"] != "PASS"

def test_final_stitch_and_pricing_required():
    report=valid_report()
    report["pricing_regressions_passed"]=False
    assert qc.evaluate(report)["decision"] != "PASS"
