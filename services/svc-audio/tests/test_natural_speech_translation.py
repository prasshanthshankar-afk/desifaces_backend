"""Provider-mocked translation tests: no keys, generation, or billing required."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'shared' / 'llm'))
import gender_translation as translation


class Response:
    headers = {}
    text = ''
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
    def json(self):
        return self.body


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs['json']))
        return next(self.responses)


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, responses, text='Welcome to desifaces', tone='informal', dialogue_context=None):
        self.client = Client(responses)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'unit-test-not-a-key'}), \
             patch.object(translation.httpx, 'AsyncClient', return_value=self.client), \
             patch.object(translation.asyncio, 'sleep', return_value=None):
            return await translation.translate_with_gender(text=text, source_language='en',
                target_language='hi-IN', speaker_gender='female', tone=tone, dialogue_context=dialogue_context)

    async def test_complete_native_script_preserves_text_and_locale(self):
        expected = 'desifaces में आपका स्वागत है।'
        result = await self.call([Response({'status':'completed', 'output_text': expected})])
        self.assertEqual(result.text, expected)
        body = self.client.calls[0][1]
        metadata = json.loads(body['input'])
        self.assertEqual(metadata['target_language'], 'hi-IN')
        self.assertEqual(metadata['speaker_gender'], 'female')
        self.assertEqual(metadata['tone'], 'informal')
        self.assertEqual(len(self.client.calls), 1)

    async def test_incomplete_response_is_rejected_before_synthesis(self):
        with self.assertRaisesRegex(translation.GenderTranslationError, 'translation_output_incomplete'):
            await self.call([Response({'status':'incomplete', 'output_text':'अधूरा वाक्य'})])
        self.assertEqual(len(self.client.calls), 1)

    async def test_long_script_does_not_keep_fixed_500_token_limit(self):
        await self.call([Response({'status':'completed', 'output_text':'पूर्ण पाठ।'})], text='Long spoken sentence. '*300)
        budget = self.client.calls[0][1]['max_output_tokens']
        self.assertGreater(budget, 500)
        self.assertLessEqual(budget, 16384)

    async def test_chat_fallback_rejects_length_truncation(self):
        failures = [Response({}, 500) for _ in range(3)]
        failures.append(Response({'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}))
        with self.assertRaisesRegex(translation.GenderTranslationError, 'translation_output_incomplete'):
            await self.call(failures)

    async def test_invalid_tone_uses_neutral_without_changing_gender(self):
        await self.call([Response({'status':'completed','output_text':'नमस्ते।'})], tone='invalid')
        metadata = json.loads(self.client.calls[0][1]['input'])
        self.assertEqual(metadata['tone'], 'neutral')
        self.assertEqual(metadata['speaker_gender'], 'female')

    async def test_neighboring_dialogue_is_context_only_and_preserves_short_answer(self):
        expected = 'हाँ, मैंने देखी हैं।'
        result = await self.call([Response({'status':'completed','output_text':expected})],
            text='I have!', dialogue_context='Previous approved dialogue: Have you seen the announcements?')
        data = json.loads(self.client.calls[0][1]['input'])
        self.assertEqual(data['text'], 'I have!')
        self.assertIn('Have you seen', data['dialogue_context'])
        self.assertEqual(result.text, expected)
        self.assertNotIn('announcements', result.text)

    async def test_empty_context_keeps_legacy_request_shape(self):
        await self.call([Response({'status':'completed','output_text':'नमस्ते।'})])
        self.assertNotIn('dialogue_context', json.loads(self.client.calls[0][1]['input']))

    async def test_context_is_bounded_before_model_request(self):
        await self.call([Response({'status':'completed','output_text':'हाँ।'})], dialogue_context='x'*5000)
        self.assertEqual(len(json.loads(self.client.calls[0][1]['input'])['dialogue_context']),1400)


if __name__ == '__main__':
    unittest.main()

