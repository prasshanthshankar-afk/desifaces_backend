"""Offline tests for DEV-only directional translation policy."""
import importlib.util
import sys
from pathlib import Path
import pytest

PATH = Path(__file__).resolve().parents[1] / "services/shared/llm/dev_translation_selection.py"
spec = importlib.util.spec_from_file_location("dev_translation_selection", PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

CAP = mod.TranslationCapability("sarvam", "mayura:v1", "en-IN", "hi-IN", True, True, 1)
BASE = dict(dev_runtime=True, feature_enabled=True, authenticated_user_id="test-user",
            allowlisted_user_ids=frozenset({"test-user"}), source_locale="en-IN",
            target_locale="hi-IN", capabilities=[CAP])


def test_verified_direction_selected():
    d = mod.choose_dev_translation(**BASE)
    assert (d.provider, d.model, d.source_locale, d.target_locale) == (
        "sarvam", "mayura:v1", "en-IN", "hi-IN")


@pytest.mark.parametrize("updates,reason", [
    ({"dev_runtime": False}, "dev_translation_feature_unavailable"),
    ({"feature_enabled": False}, "dev_translation_feature_unavailable"),
    ({"authenticated_user_id": "other"}, "user_not_allowlisted"),
    ({"allowlisted_user_ids": frozenset()}, "user_not_allowlisted"),
    ({"target_locale": "ta-IN"}, "no_verified_translation_capability"),
    ({"capabilities": []}, "no_verified_translation_capability"),
    ({"capabilities": [mod.TranslationCapability("sarvam","mayura:v1","en-IN","hi-IN",False,True)]}, "no_verified_translation_capability"),
    ({"capabilities": [mod.TranslationCapability("sarvam","mayura:v1","en-IN","hi-IN",True,False)]}, "no_verified_translation_capability"),
])
def test_fail_closed(updates, reason):
    kwargs = dict(BASE)
    kwargs.update(updates)
    with pytest.raises(mod.TranslationSelectionError, match=reason):
        mod.choose_dev_translation(**kwargs)


def test_priority_and_direction_not_hardcoded():
    caps = [
        mod.TranslationCapability("any-provider", "model-b", "en-US", "fr-FR", True, True, 20),
        mod.TranslationCapability("provider-a", "model-a", "en-US", "fr-FR", True, True, 5),
    ]
    kwargs = dict(BASE, source_locale="en-US", target_locale="fr-FR", capabilities=caps)
    d = mod.choose_dev_translation(**kwargs)
    assert (d.provider, d.model) == ("provider-a", "model-a")
