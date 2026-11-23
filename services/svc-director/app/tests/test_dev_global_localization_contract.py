import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "services/shared/python/desifaces_shared/v3/localization_contract.py"

spec = importlib.util.spec_from_file_location(
    "dev101_localization_contract", MODULE
)
assert spec is not None and spec.loader is not None

contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)

LocalizationContractError = contract.LocalizationContractError
source_fingerprint = contract.source_fingerprint
validate_approved_localization = contract.validate_approved_localization


class GlobalLocalizationContractTests(unittest.TestCase):
    def setUp(self):
        self.source = [{
            "turn_id": "1:1",
            "speaker": "speaker1",
            "source_text": "Hello",
        }]
        self.fingerprint = source_fingerprint("en-US", self.source)

    def decision(self, locale, text):
        return {
            "approved": True,
            "target_locale": locale,
            "source_fingerprint": self.fingerprint,
            "turns": [{
                **self.source[0],
                "text": text,
            }],
        }

    def validate(self, locale, decision, enabled=True):
        return validate_approved_localization(
            source_locale="en-US",
            target_locale=locale,
            canonical_turns=self.source,
            decision=decision,
            is_pair_enabled=lambda source, target: enabled,
        )

    def test_multiple_language_families(self):
        cases = {
            "hi-IN": "नमस्ते",
            "es-MX": "Hola",
            "ar-SA": "مرحبًا",
            "ja-JP": "こんにちは",
            "zh-Hant-TW": "你好",
            "pt-BR": "Olá",
            "sw-KE": "Habari",
        }

        for locale, translated in cases.items():
            with self.subTest(locale=locale):
                result = self.validate(
                    locale, self.decision(locale, translated)
                )
                self.assertEqual(result["turns"][0]["text"], translated)
                self.assertEqual(result["target_locale"], locale)

    def test_rejected_approval(self):
        decision = self.decision("hi-IN", "नमस्ते")
        decision["approved"] = False
        with self.assertRaises(LocalizationContractError):
            self.validate("hi-IN", decision)

    def test_stale_source(self):
        decision = self.decision("hi-IN", "नमस्ते")
        decision["source_fingerprint"] = "stale"
        with self.assertRaises(LocalizationContractError):
            self.validate("hi-IN", decision)

    def test_wrong_speaker(self):
        decision = self.decision("hi-IN", "नमस्ते")
        decision["turns"][0]["speaker"] = "wrong"
        with self.assertRaises(LocalizationContractError):
            self.validate("hi-IN", decision)

    def test_unsupported_provider_route(self):
        with self.assertRaises(LocalizationContractError):
            self.validate(
                "hi-IN",
                self.decision("hi-IN", "नमस्ते"),
                enabled=False,
            )

    def test_wrong_locale(self):
        with self.assertRaises(LocalizationContractError):
            self.validate(
                "es-MX",
                self.decision("hi-IN", "नमस्ते"),
            )

    def test_empty_approved_text(self):
        with self.assertRaises(LocalizationContractError):
            self.validate(
                "hi-IN",
                self.decision("hi-IN", ""),
            )

    def test_canonical_source_not_mutated(self):
        original = [dict(x) for x in self.source]
        self.validate("hi-IN", self.decision("hi-IN", "नमस्ते"))
        self.assertEqual(self.source, original)


if __name__ == "__main__":
    unittest.main()
