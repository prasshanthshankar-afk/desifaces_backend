"""Development proof: real group-photo reuse handlers, real DB, rolled back."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import asyncpg
import httpx
from app.config import settings
from app.db import open_business_pool
from app.security import DirectorAuthContext
from app.shared_scene_routes import SharedSceneDraftIn, SharedSceneConversationIn, set_shared_scene_draft, set_shared_scene_conversation
from app.shared_scene_state_routes import load_shared_scene_state


class BorrowedConnection:
    def __init__(self,conn):
        self.conn=conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self,*args):
        pass


async def forbid_network(*args,**kwargs):
    raise AssertionError('Reuse must not call pricing, credit reservation or generation over HTTP')


async def financial_snapshot(conn,user_id):
    columns=await conn.fetch('''
        select table_name,column_name from information_schema.columns
        where table_schema='public' and column_name in ('user_id','owner_user_id')
          and table_name in (
            select table_name from information_schema.tables
            where table_schema='public' and table_type='BASE TABLE'
              and (table_name ~ '(credit|wallet|ledger|reservation)' or table_name in ('studio_jobs','longform_jobs'))
          ) order by table_name,column_name
    ''')
    result={}
    for row in columns:
        table,column=row['table_name'],row['column_name']
        assert table.replace('_','').isalnum() and column in {'user_id','owner_user_id'}
        rows=await conn.fetch(f'SELECT row_to_json(t)::text AS value FROM public."{table}" t WHERE "{column}"=$1',user_id)
        encoded='\n'.join(sorted(r['value'] for r in rows)).encode()
        result[table]={'rows':len(rows),'sha256':hashlib.sha256(encoded).hexdigest()}
    assert 'pricing_credit_reservations' in result and 'studio_jobs' in result, 'billing/job proof tables unavailable'
    assert any('credit' in name and name!='pricing_credit_reservations' for name in result), 'wallet/ledger proof unavailable'
    return result


async def main():
    application_pool=await open_business_pool()
    c=await application_pool.acquire()
    transaction=c.transaction(isolation='repeatable_read')
    await transaction.start()
    original_http=httpx.AsyncClient.request
    httpx.AsyncClient.request=forbid_network
    try:
        row=await c.fetchrow('''
            select w.workflow_id,w.owner_user_id,w.account_id,w.project_id,s.stage_run_id,s.scene_id,m.id as media_id,m.project_id as origin_project
            from public.v3_studio_workflows w
            join public.v3_studio_stage_runs s on s.workflow_id=w.workflow_id and s.stage_type='fusion' and s.scope_type='scene'
            join public.media_assets m on m.user_id=w.owner_user_id and m.account_id=w.account_id and m.project_id<>w.project_id
              and m.kind in ('image','source_image','face_image','face_source_image') and m.lifecycle_state='active' and m.deleted_at is null
              and m.meta_json->'shared_scene_validation'->>'allow'='true'
              and m.meta_json->'shared_scene_validation'->>'expected_speakers'='2'
            where w.metadata_json->>'conversation_mode'='shared_scene'
              and w.metadata_json->>'shared_scene_people_approved'='true'
              and s.state in ('pending','ready','failed','rejected')
              and (select count(distinct speaker_participant_id) from public.v3_dialogue_turns where scene_id=s.scene_id and turn_kind='speech')=2
            order by w.updated_at desc,m.created_at desc limit 1
        ''')
        assert row, 'owned cross-project development group-photo fixture missing'
        speakers=await c.fetch("select distinct speaker_participant_id from public.v3_dialogue_turns where scene_id=$1 and turn_kind='speech' order by speaker_participant_id",row['scene_id'])
        targets=[{'participant_id':str(s['speaker_participant_id']),'point':{'x':0.3 if i==0 else 0.7,'y':0.4}} for i,s in enumerate(speakers)]
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(business_pool=SimpleNamespace(acquire=lambda:BorrowedConnection(c)))))
        auth=DirectorAuthContext(user_id=row['owner_user_id'],account_id=row['account_id'],claims={})
        before=await financial_snapshot(c,auth.user_id)
        origin=await c.fetchval('select project_id from public.media_assets where id=$1',row['media_id'])
        draft=await set_shared_scene_draft(row['workflow_id'],row['stage_run_id'],SharedSceneDraftIn(shared_scene_media_id=row['media_id'],image_width=1536,image_height=1024,speaker_targets=targets),request,auth)
        assert draft['persisted'] and draft['mapped_speaker_count']==2
        assert draft['shared_scene_draft_media_id']==str(row['media_id'])
        approved=await set_shared_scene_conversation(row['workflow_id'],row['stage_run_id'],SharedSceneConversationIn(shared_scene_media_id=row['media_id'],image_width=1536,image_height=1024,speaker_targets=targets),request,auth)
        assert approved['shared_scene_media_id']==str(row['media_id'])
        bound=await c.fetchval("select exists(select 1 from public.v3_studio_stage_inputs where stage_run_id=$1 and media_id=$2 and input_role='approved_shared_scene_image')",row['stage_run_id'],row['media_id'])
        assert bound
        metadata=await c.fetchval('select metadata_json from public.v3_studio_stage_runs where stage_run_id=$1',row['stage_run_id'])
        if isinstance(metadata,str):metadata=json.loads(metadata)
        assert set(metadata['speaker_targets'])=={str(s['speaker_participant_id']) for s in speakers}
        state=await load_shared_scene_state(c,workflow_id=row['workflow_id'],account_id=auth.account_id)
        assert state['group_photo']['approved_media_id']==str(row['media_id'])
        assert state['phase'] in {'audio','video','final'},state['phase']
        assert await c.fetchval('select project_id from public.media_assets where id=$1',row['media_id'])==origin
        after=await financial_snapshot(c,auth.user_id)
        assert before==after, 'reuse changed wallet/ledger/reservation/job records'
        print('ACTUAL_GROUP_PHOTO_DRAFT_AND_APPROVAL_ROUTES=PASS')
        print('REUSE_NEXT_PHASE='+state['phase'])
        print('SPEAKER_IDS_AND_ORIGINAL_PHOTO_LINEAGE=PASS')
        print('REUSE_CREDIT_RESERVATION_DELTA=0')
        print('REUSE_WALLET_AND_LEDGER_DELTA=0')
        print('REUSE_NEW_GENERATION_JOB_DELTA=0')
        print('REUSE_EXTERNAL_PRICING_AND_GENERATION_CALLS=0')
        print('FINANCIAL_TABLES_VERIFIED='+','.join(sorted(before)))
    finally:
        httpx.AsyncClient.request=original_http
        await transaction.rollback()
        await application_pool.release(c)
        await application_pool.close()
        print('DEVELOPMENT_PROOF_TRANSACTION=ROLLED_BACK')


asyncio.run(main())
