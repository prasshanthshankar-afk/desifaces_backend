# DEV101 worldwide translation draft routing (DEV experiment)

## Scope and evidence, 2026-11-23

DEV `public.tts_locales` inventory: 228 enabled locale records; 91 distinct enabled language codes; 208 entries with `translate_supported`; 139 selectable entries with both translation and TTS enabled. Live provider-language discovery returned 156 Azure translation codes and 195 Google Cloud NMT codes. Explicit existing `translator_lang` values matched Azure for 139/139 eligible targets and Google for 135/139. These are **candidate mappings, not end-to-end validated provider routes**. The `wuu` language code had no exact match from either catalog, but no eligible target locale was uncovered.

Only two DB routes are approved and enabled: `en-IN -> hi-IN` and `en-US -> hi-IN`, both through Sarvam `mayura:v1` using provider input `en-IN` and output `hi-IN`. Keep them unchanged.

## Behavior

- Experimental candidate discovery is *disabled by default*, even in DEV.
- An authenticated, existing, QA-cohort user on the attested DEV VM can request a draft through `/api/audio/dev101/director-translation-preview` only when `DF_DEV_GLOBAL_TRANSLATION_ENABLED=true`.
- The normal approved DB route runs first. **Only** `no_approved_translation_direction` invokes candidate discovery in DEV when explicitly enabled; an approved provider failure never silently switches providers.
- Candidate selection reads existing source and target locale rows. Source and target must both be enabled and translation-capable; the target must additionally be selectable and TTS-capable. Provider codes come from existing `translator_lang`, never inferred from region or language base.
- Candidate languages must be present in live Azure v3 or Google NMT language discovery. Azure is preferred when both source and target codes exist and credentials are configured; otherwise Google is considered. No provider is called with guessed locale codes.
- Text translation runs using the existing provider adapter, then the existing OpenAI contextual speech and quality checks. It remains a *pending human review* draft (`approved=false`) with `experimental_translation_route=true` for observability.
- A request for two locales sharing the same provider language code fails closed (`regional_rewrite_not_certified`), as automatic translation between regional dialects is not supported by this experimental path.
- DEV experiment does not write to the catalog, approve provider routes, generate TTS/video, change billing, or migrate any database.
- Production never uses this code path even if the DEV feature flag is accidentally configured there. Production preview remains disabled by default and cohort-restricted.

## Testing and promotion

Run the Audio test image without external network access:

```sh
python -m unittest discover -s /app/tests -p 'test_dev_global_translation.py' -v
python -m unittest discover -s /app/tests -p 'test_dev101_translation_preview.py' -v
```

These are **offline** unit tests, not live translation quality certification. After they pass, deploy an isolated DEV Audio canary, enable `DF_DEV_GLOBAL_TRANSLATION_ENABLED=true` only for the attested DEV cohort, and test diverse language pairs against real providers. Explicit human review and voice/lip-sync end-to-end QA remain necessary for each prioritized region and source/target pair. Do not bulk-mark provider candidates as `approved=true`.

Use a separate controlled production release and only certify approved routes, rate limits, cost quotas, audit events, and public access after production security review.
