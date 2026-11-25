"""DEV101 regression: regional English is not cross-language translation.

Run with the existing backend test environment/PYTHONPATH; no external provider,
database, wallet, or story creation is involved.
"""
from pathlib import Path

import pytest

from desifaces_shared.v3.english_source_policy import requires_translation

@pytest.mark.parametrize("source,target,expected", [
    ("en-GB", "en-US", False),
    ("en-US", "en-AU", False),
    ("en-IN", "en-GB", False),
    ("en-US", "en-US", False),
    ("en-US", "hi-IN", True),
    ("en-IN", "hi-IN", True),
    ("en-GB", "ru-RU", True),
    ("en-AU", "es-ES", True),
    ("en-US", "", False),
])
def test_r1_language_direction(source, target, expected):
    assert requires_translation(source, target) is expected

@pytest.mark.parametrize("source,target", [
    ("ru-RU", "en-GB"),
    ("hi-IN", "hi-IN"),
    ("es-ES", "fr-FR"),
])
def test_non_english_source_stays_disabled(source, target):
    with pytest.raises(ValueError, match="r1_english_source_required"):
        requires_translation(source, target)


def test_planner_and_compiler_use_the_same_policy():
    root = Path(__file__).resolve().parents[1]
    graph = (root / "services/shared/python/desifaces_shared/v3/director_graph.py").read_text()
    compiler = (root / "services/svc-director/app/app/experience_compiler.py").read_text()
    assert graph.count("requires_translation(") >= 5
    assert "needs_localization = requires_translation(source_locale, target_locale)" in compiler
    assert "resolve_approved_translation_route(" in graph
    assert "resolve_approved_translation_route(" in compiler
    assert "localization_source_fingerprint_required" in graph
    assert 'payload.get("target_locale") != "hi-IN"' in graph
