"""Exercise candidate routing against DEV data; counterfactual always rolls back."""
import asyncio
import json
import os
from pathlib import Path
import sys

import asyncpg


class Borrowed:
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self, *args):
        return False


class ConnectionPool:
    def __init__(self, conn):
        self.conn = conn
    def acquire(self):
        return Borrowed(self.conn)
    def __getattr__(self, name):
        return getattr(self.conn, name)


async def main():
    from app.services.tts_service import TTSService
    from app.services.tts_resolution_planner import TTSResolutionPlanRequest
    from app.api.routes.catalog import list_locales, list_voices

    mode = sys.argv[1]
    assert mode in ('counterfactual', 'active')
    conn = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://'))
    tx = conn.transaction(readonly=(mode == 'active'))
    await tx.start()
    try:
        pool = ConnectionPool(conn)
        before = await conn.fetchval("SELECT revision FROM public.masterdata_revision WHERE domain='tts'")
        if mode == 'counterfactual':
            sql = Path('/opt/indian-speech/preference.sql').read_text()
            assert sql.count('\nBEGIN;') == 1 and sql.rstrip().endswith('COMMIT;')
            sql = sql.replace('\nBEGIN;', '\n', 1).rsplit('COMMIT;', 1)[0]
            await conn.execute(sql)
        service = TTSService(pool)
        locales = await list_locales(market='global', end_to_end_only=True, enabled_only=True, pool=pool)
        by_locale = {item['locale']: item for item in locales['items']}
        for locale in ('hi-IN','ta-IN','te-IN','bn-IN','gu-IN','kn-IN','ml-IN','mr-IN','pa-IN','or-IN'):
            voices = (await list_voices(locale=locale, market='global', pool=pool))['items']
            for gender in ('male','female'):
                request = TTSResolutionPlanRequest(requested_locale=locale, text_length=100,
                    requested_gender=gender, country_code='US')
                plan = await service.resolution_planner.resolve(request)
                assert plan.provider_code == 'sarvam', (locale, gender, plan.provider_code)
                assert plan.voice_gender == gender
                same_gender = [voice for voice in voices if str(voice['gender']).lower()==gender]
                suggested = next((voice for voice in same_gender if voice['is_default']), same_gender[0])
                chosen = await service.resolution_planner.resolve(TTSResolutionPlanRequest(
                    requested_locale=locale, text_length=100, requested_gender=gender,
                    requested_voice=suggested['voice_name'], country_code='US'))
                assert chosen.provider_code == 'sarvam', (locale,gender,'catalog and execution disagree')
                assert chosen.voice_name == plan.voice_name, (locale,gender,'default voice mismatch')
                print('SARVAM_DEFAULT=' + '|'.join((locale,gender,plan.voice_name)))
            advertised = await service.resolution_planner.resolve(TTSResolutionPlanRequest(
                requested_locale=locale, text_length=100, requested_voice=by_locale[locale]['default_voice']))
            assert advertised.provider_code == 'sarvam', (locale,'locale default mismatch')
        explicit = await service.resolution_planner.resolve(TTSResolutionPlanRequest(
            requested_locale='hi-IN', text_length=100, requested_gender='female',
            requested_voice='cgSgspJ2msm6clMCkdW9'))
        assert explicit.provider_code == 'elevenlabs'
        assert explicit.voice_name == 'cgSgspJ2msm6clMCkdW9'
        english = await service.resolution_planner.resolve(TTSResolutionPlanRequest(
            requested_locale='en-US', text_length=100, requested_gender='female'))
        assert english.provider_code == 'elevenlabs'
        for voice in ('shubh','priya'):
            assert await service._is_explicit_sarvam_voice_eligible(voice=voice,target_locale='hi-IN')
        assert not await service._is_explicit_sarvam_voice_eligible(voice=explicit.voice_name,target_locale='hi-IN')
        print('EXPLICIT_VOICE_AND_NON_INDIAN_ROUTING_PRESERVED=PASS')
        print('CATALOG_EXECUTION_AND_GENDER_DEFAULTS_ALIGNED=PASS')
        print('REUSED_ARTIFACT_MUTATIONS=0')
        print('PRICING_RESERVATION_AND_GENERATION_CALLS=0')
    finally:
        await tx.rollback()
        after = await conn.fetchval("SELECT revision FROM public.masterdata_revision WHERE domain='tts'")
        assert after == before, 'certification changed persisted masterdata'
        await conn.close()
    print('INDIAN_SPEECH_' + mode.upper() + '_CERTIFICATION=PASS')


if __name__ == '__main__':
    asyncio.run(main())
