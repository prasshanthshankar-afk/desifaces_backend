import unittest

from app.services.tts_provider_adapter import (
    TTSProviderAdapterError,
)
from app.services.tts_provider_registry import (
    TTSProviderAdapterRegistry,
)


class RegistryTests(unittest.TestCase):
    def test_default_keys(self):
        keys = set(
            TTSProviderAdapterRegistry
            ._default_factories()
            .keys()
        )

        self.assertEqual(
            keys,
            {"azure", "elevenlabs", "sarvam"},
        )

    def test_injected_factory_and_normalization(self):
        sentinel = object()

        registry = TTSProviderAdapterRegistry(
            factories={
                "azure": lambda: sentinel,
            }
        )

        self.assertIs(
            registry.create(" Azure "),
            sentinel,
        )

    def test_missing_key_fails_closed(self):
        registry = TTSProviderAdapterRegistry(
            factories={}
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "missing_adapter_key",
        ):
            registry.create("")

    def test_unknown_key_fails_closed(self):
        registry = TTSProviderAdapterRegistry(
            factories={}
        )

        with self.assertRaisesRegex(
            TTSProviderAdapterError,
            "unknown_adapter_key",
        ):
            registry.create("unknown")


if __name__ == "__main__":
    unittest.main()
