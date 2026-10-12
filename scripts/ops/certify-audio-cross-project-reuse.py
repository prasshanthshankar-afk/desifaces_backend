"""Exercise the canonical route against live rows with all writes suppressed."""
import asyncio
import asyncpg
from app.config import settings
from app.api.routes.v3_audio_output import get_canonical_audio_output


class ReadonlyConnection:
    def __init__(self,conn,record):
        self.conn,self.record=conn,record
        self.adoption_seen=False

    def __getattr__(self,name):
        return getattr(self.conn,name)

    async def execute(self,sql,*args):
        normalized=' '.join(sql.split())
        assert normalized == 'update public.media_assets set account_id=coalesce(account_id,$2), project_id=coalesce(project_id,$3), updated_at=now() where id=$1', 'unexpected route write'
        assert args == (self.record['media_id'],self.record['account_id'],self.record['destination_project'])
        self.adoption_seen=True
        return 'UPDATE 0'

    async def __aenter__(self):
        return self

    async def __aexit__(self,*args):
        pass


class ReadonlyPool:
    def __init__(self,proxy):
        self.proxy=proxy

    def acquire(self):
        return self.proxy


async def main():
    c=await asyncpg.connect(settings.DATABASE_URL.replace('postgresql+asyncpg://','postgresql://'),timeout=10,server_settings={'statement_timeout':'15000'})
    try:
        async with c.transaction(readonly=True):
            row=await c.fetchrow('''
                select m.id as media_id,m.user_id,m.account_id,m.project_id as origin_project,
                  p.project_id as destination_project,j.id as job_id
                from public.media_assets m
                join public.studio_jobs j on j.id::text=m.meta_json->>'source_studio_job_id'
                  and j.user_id=m.user_id and j.status='succeeded' and j.studio_type='audio'
                join public.v3_projects p on p.account_id=m.account_id and p.owner_user_id=m.user_id
                  and p.project_id<>m.project_id and p.lifecycle_state='active'
                where m.kind='audio' and m.lifecycle_state='active' and m.deleted_at is null
                  and m.meta_json->>'source_audio_artifact_id' is not null
                order by m.created_at desc,p.updated_at desc limit 1
            ''')
            assert row, 'no canonical audio cross-project fixture available'
            proxy=ReadonlyConnection(c,row)
            result=await get_canonical_audio_output(job_id=row['job_id'],project_id=row['destination_project'],user_id=str(row['user_id']),pool=ReadonlyPool(proxy))
            assert result.media_id==row['media_id'] and result.audio_url
            assert proxy.adoption_seen
            origin=await c.fetchval('select project_id from public.media_assets where id=$1',row['media_id'])
            assert origin==row['origin_project']
            print('LIVE_AUDIO_CANONICAL_CROSS_PROJECT_REUSE=PASS')
            print('EXISTING_AUDIO_ID_AND_ORIGIN_PROJECT_PRESERVED=PASS')
            print('AUDIO_AUDIT_DATABASE_WRITES=NONE')
    finally:
        await c.close()


asyncio.run(main())
