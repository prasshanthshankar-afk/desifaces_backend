import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MODULE = (
    ROOT
    / "services/shared/python/desifaces_shared/v3/localization_contract.py"
)

spec = importlib.util.spec_from_file_location(
    "dev101_adapter_contract", MODULE
)
assert spec is not None and spec.loader is not None
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


class GlobalDirectorAdapterTests(unittest.TestCase):

    def setUp(self):
        self.plan = {
            "scenes": [
                {
                    "sequence": 2,
                    "dialogue": [{
                        "sequence": 1,
                        "kind": "speech",
                        "speaker_ref": "s2",
                        "text": "Goodbye",
                    }],
                },
                {
                    "sequence": 1,
                    "dialogue": [
                        {
                            "sequence": 2,
                            "kind": "action",
                            "speaker_ref": None,
                            "text": "Walks away",
                        },
                        {
                            "sequence": 1,
                            "kind": "speech",
                            "speaker_ref": "s1",
                            "text": "Hello",
                        },
                    ],
                },
            ]
        }

        self.canonical = contract.canonical_turns_from_plan(
            self.plan
        )

    def brief(self, locale):
        return {
            "locale": "en-US",
            "constraints": {
                "requested_output_locale": locale,
            },
        }

    def approval(self, locale, texts):
        fingerprint = contract.source_fingerprint(
            "en-US", self.canonical
        )

        return {
            "approved": True,
            "target_locale": locale,
            "source_fingerprint": fingerprint,
            "localized_turns": [
                {**source, "text": text}
                for source, text in zip(self.canonical, texts)
            ],
        }

    def validate(self, locale, payload, enabled=True):
        return contract.validate_director_resume(
            brief=self.brief(locale),
            plan=self.plan,
            resume_payload=payload,
            is_pair_enabled=lambda s, t: enabled,
        )

    def test_scene_order_and_action_exclusion(self):
        self.assertEqual(
            [x["turn_id"] for x in self.canonical],
            ["1:1", "2:1"],
        )
        self.assertEqual(len(self.canonical), 2)

    def test_global_language_families(self):
        cases = {
            "hi-IN": ["नमस्ते", "अलविदा"],
            "es-MX": ["Hola", "Adiós"],
            "ar-SA": ["مرحبًا", "وداعًا"],
            "ja-JP": ["こんにちは", "さようなら"],
            "fr-CA": ["Bonjour", "Au revoir"],
            "sw-KE": ["Habari", "Kwaheri"],
        }

        for locale, texts in cases.items():
            with self.subTest(locale=locale):
                result = self.validate(
                    locale, self.approval(locale, texts)
                )
                self.assertEqual(
                    [t["text"] for t in result["turns"]],
                    texts,
                )

    def test_stale_source_rejected(self):
        payload = self.approval(
            "hi-IN", ["नमस्ते", "अलविदा"]
        )
        payload["source_fingerprint"] = "stale"

        with self.assertRaises(
            contract.LocalizationContractError
        ):
            self.validate("hi-IN", payload)

    def test_wrong_speaker_rejected(self):
        payload = self.approval(
            "es-MX", ["Hola", "Adiós"]
        )
        payload["localized_turns"][0]["speaker"] = "wrong"

        with self.assertRaises(
            contract.LocalizationContractError
        ):
            self.validate("es-MX", payload)

    def test_wrong_turn_order_rejected(self):
        payload = self.approval(
            "hi-IN", ["नमस्ते", "अलविदा"]
        )
        payload["localized_turns"].reverse()

        with self.assertRaises(
            contract.LocalizationContractError
        ):
            self.validate("hi-IN", payload)

    def test_disabled_pair_rejected(self):
        with self.assertRaises(
            contract.LocalizationContractError
        ):
            self.validate(
                "fr-CA",
                self.approval("fr-CA", ["Bonjour", "Au revoir"]),
                enabled=False,
            )

    def test_compiler_context_preserved(self):
        original = {"refs": ["existing"]}
        payload = self.approval(
            "hi-IN", ["नमस्ते", "अलविदा"]
        )

        context = contract.build_verified_director_compiler_context(
            brief=self.brief("hi-IN"),
            plan=self.plan,
            resume_payload=payload,
            is_pair_enabled=lambda s, t: True,
            retrieved_context=original,
        )

        self.assertEqual(original, {"refs": ["existing"]})
        self.assertEqual(context["refs"], ["existing"])
        self.assertEqual(
            context["_approved_hitl_localization"]["turns"][0]["text"],
            "नमस्ते",
        )

    def test_rejected_approval_cannot_reach_compiler(self):
        payload = self.approval(
            "ar-SA", ["مرحبًا", "وداعًا"]
        )
        payload["approved"] = False

        with self.assertRaises(
            contract.LocalizationContractError
        ):
            contract.build_verified_director_compiler_context(
                brief=self.brief("ar-SA"),
                plan=self.plan,
                resume_payload=payload,
                is_pair_enabled=lambda s, t: True,
                retrieved_context={},
            )


if __name__ == "__main__":
    unittest.main()
