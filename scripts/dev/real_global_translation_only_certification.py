"""Translation-only real-provider DEV certification; no Audio jobs or DB writes.

Invoke with a read-only bind-mount of candidate services into the existing Audio
worker image, with API credentials supplied from the running DEV worker.
"""
import asyncio
import json
import sys
from pathlib import Path

import app.services

app.services.__path__ = ["/candidate_services", *list(app.services.__path__)]
from app.services.dev_provider_translation import translate_approved

TURNS = [
    ("Speaker 1", "I’m excited about the venture. I just keep wondering how we’ll get noticed on social media without a full content team."),
    ("Speaker 2", "That worried me too. A strong idea isn’t enough if our content doesn’t look polished or explain what makes us different."),
    ("Speaker 1", "And we need something we can use consistently, not a different message every time we post."),
    ("Speaker 2", "That’s where desifaces helped. We created high-quality content that made our message feel clear, credible, and ready to share."),
    ("Speaker 1", "So now we can focus on building the venture while the story stays consistent across our social channels."),
    ("Speaker 2", "Exactly—and we can communicate with a global audience in a way that still feels true to what we’re creating."),
]
ROUTES = [
    ("or-IN", "sarvam", "mayura:v1", "en-IN", "od-IN"),
    ("es-MX", "azure_translator", "text-translation-v3", "en", "es"),
    ("ar-EG", "azure_translator", "text-translation-v3", "en", "ar"),
]
SCRIPT = "\n".join(f"{name}: {line}" for name, line in TURNS)


class ReadOnlyRoutePool:
    def __init__(self, locale, capability):
        self.locale, self.capability = locale, capability

    async def fetch(self, query, source, target):
        assert "public.tts_locales" in query and target == self.locale
        assert source == "en-IN"
        return [
            {"locale": "en-IN", "language_code": "en", "meta_json": {}},
            {"locale": target, "language_code": target.split("-")[0],
             "meta_json": {"translation_routing_v1": {
                 "enabled": True, "capabilities": [self.capability]}}},
        ]


async def main():
    outcomes = {}
    for locale, provider, model, source_code, target_code in ROUTES:
        route = {
            "source_locale": "en-IN", "target_locale": locale,
            "provider": provider, "model": model,
            "provider_source_code": source_code, "provider_target_code": target_code,
            "approved": True, "enabled": True,
        }
        results = []
        print(f"===== LOCALE={locale} PROVIDER={provider} =====", flush=True)
        for i, (speaker, original) in enumerate(TURNS):
            try:
                translated, used_provider, used_model = await translate_approved(
                    ReadOnlyRoutePool(locale, route), text=original,
                    source_locale="en-IN", target_locale=locale,
                    speaker_gender="female" if i % 2 == 0 else "male",
                    original_context={
                        "full_audio_script": SCRIPT,
                        "previous_turn": TURNS[i - 1][1] if i else "",
                        "next_turn": TURNS[i + 1][1] if i + 1 < len(TURNS) else "",
                        "speaker_relationship": "Business collaborators",
                        "scene_context": "Business venture and social media marketing. Brand name: desifaces.",
                        "emotion": "conversational",
                    },
                )
                assert used_provider == provider and used_model == model
                results.append({"turn": i + 1, "status": "MODEL_APPROVED",
                                "source": original, "translation": translated})
                print(f"TURN={i+1} MODEL_APPROVED TEXT={translated}", flush=True)
            except Exception as exc:
                results.append({"turn": i + 1, "status": "REVIEW_REQUIRED",
                                "source": original, "reason": str(exc)[:160]})
                print(f"TURN={i+1} REVIEW_REQUIRED REASON={type(exc).__name__}:{str(exc)[:160]}", flush=True)
        outcomes[locale] = results
        print(f"LOCALE={locale} MODEL_APPROVED={sum(r['status']=='MODEL_APPROVED' for r in results)}/6", flush=True)

    # Keep results on stdout; leave filesystem unchanged.
    print("BEGIN_REVIEWABLE_JSON")
    print(json.dumps(outcomes, ensure_ascii=False, indent=2))
    print("END_REVIEWABLE_JSON")
    print("TTS_CALLS=0")
    print("DATABASE_WRITES=0")
    print("OMNIHUMAN_CALLS=0")
    print("MODEL_APPROVAL_IS_NOT_NATIVE_SPEAKER_CERTIFICATION")


if __name__ == "__main__":
    asyncio.run(main())
