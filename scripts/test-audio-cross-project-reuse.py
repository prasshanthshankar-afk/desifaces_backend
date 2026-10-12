"""Run actual canonical-output/read-url route bodies against a local database."""
import ast
import json
import os
import re
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
ROUTE = Path(os.environ.get('AUDIO_REUSE_ROUTE', str(ROOT/'services/svc-audio/app/app/api/routes/v3_audio_output.py')))


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class Connection:
    def __init__(self, db):
        self.db = db

    def query(self, sql, args):
        sql = re.sub(r'\$(\d+)', r':p\1', sql.replace('public.', '').replace('::uuid','').replace('::jsonb',''))
        return self.db.execute(sql, {'p'+str(i+1): str(v) if isinstance(v,UUID) else v for i,v in enumerate(args)})

    async def fetchval(self, sql, *args):
        row = self.query(sql,args).fetchone()
        return row[0] if row else None

    async def fetchrow(self, sql, *args):
        row = self.query(sql,args).fetchone()
        return dict(row) if row else None

    async def execute(self, sql, *args):
        self.query(sql,args)

    async def __aenter__(self):
        return self

    async def __aexit__(self,*args):
        pass


class AudioReuseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user, self.account, self.old_project, self.new_project, self.job, self.artifact, self.media = [uuid4() for _ in range(7)]
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_function('now',0,lambda:'updated')
        self.db.executescript('''
            create table v3_projects(project_id,account_id);
            create table studio_jobs(id,status,user_id,studio_type);
            create table artifacts(id,job_id,kind,url,content_type,bytes,sha256,meta_json,created_at);
            create table media_assets(id,user_id,kind,content_type,bytes,account_id,project_id,meta_json,lifecycle_state,storage_ref,created_at,updated_at);
        ''')
        self.db.execute('insert into v3_projects values(?,?)',(str(self.new_project),str(self.account)))
        self.db.execute('insert into studio_jobs values(?,?,?,?)',(str(self.job),'succeeded',str(self.user),'audio'))
        self.db.execute('insert into artifacts values(?,?,?,?,?,?,?,?,?)',(str(self.artifact),str(self.job),'audio','https://example.blob.core.windows.net/audio/voice.wav','audio/wav',100,None,json.dumps({'storage_container':'audio','storage_path':'voice.wav'}),'created'))
        self.db.execute('insert into media_assets values(?,?,?,?,?,?,?,?,?,?,?,?)',(str(self.media),str(self.user),'audio','audio/wav',100,str(self.account),str(self.old_project),json.dumps({'source_audio_artifact_id':str(self.artifact)}),'active','azure://audio/voice.wav','created','created'))
        conn = Connection(self.db)
        self.pool = SimpleNamespace(acquire=lambda:conn)
        async def resolve(conn,user):
            return SimpleNamespace(account_id=self.account)
        namespace = {'UUID':UUID,'HTTPException':HTTPException,'json':json,'urlparse':urlparse,'settings':SimpleNamespace(AUDIO_OUTPUT_CONTAINER='audio'),'_resolve_account_or_401':resolve,'_as_dict':lambda x:json.loads(x) if isinstance(x,str) else dict(x or {}),'_sign_audio_blob':lambda **kw:'fresh-signed-url','CanonicalAudioOutputView':lambda **kw:kw,'AudioReadUrlView':lambda **kw:kw,'AzureStorageService':lambda:SimpleNamespace(generate_read_url=lambda ref:'fresh-signed-url')}
        tree = ast.parse(ROUTE.read_text())
        functions = [n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name in {'get_canonical_audio_output','get_audio_media_read_url'}]
        assert len(functions)==2
        for fn in functions:
            fn.decorator_list=[]
            fn.returns=None
            fn.args.defaults=[]
            for arg in fn.args.args:
                arg.annotation=None
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),str(ROUTE),'exec'),namespace)
        self.canonical = namespace['get_canonical_audio_output']
        self.read_url = namespace['get_audio_media_read_url']

    def tearDown(self):
        self.db.close()

    async def call(self):
        return await self.canonical(self.job,self.new_project,str(self.user),self.pool)

    async def test_cross_project_reuses_same_audio_identity_without_new_asset(self):
        result = await self.call()
        self.assertEqual(result['media_id'],self.media)
        self.assertEqual(result['audio_url'],'fresh-signed-url')
        self.assertEqual(self.db.execute('select count(*) from media_assets').fetchone()[0],1)
        self.assertEqual(self.db.execute('select project_id from media_assets').fetchone()[0],str(self.old_project))
        self.assertEqual((await self.call())['media_id'],self.media)

    async def test_foreign_account_media_rejected(self):
        self.db.execute('update media_assets set account_id=?',(str(uuid4()),))
        with self.assertRaises(HTTPException) as error:
            await self.call()
        self.assertEqual(error.exception.detail,'audio_canonical_media_account_mismatch')

    async def test_foreign_project_account_rejected(self):
        self.db.execute('update v3_projects set account_id=?',(str(uuid4()),))
        with self.assertRaises(HTTPException) as error:
            await self.call()
        self.assertEqual(error.exception.detail,'project_not_found')

    async def test_foreign_user_job_rejected(self):
        self.db.execute('update studio_jobs set user_id=?',(str(uuid4()),))
        with self.assertRaises(HTTPException) as error:
            await self.call()
        self.assertEqual(error.exception.detail,'audio_job_not_found')

    async def test_legacy_lineage_adopts_only_missing_values(self):
        self.db.execute('update media_assets set account_id=null,project_id=null')
        await self.call()
        row=self.db.execute('select account_id,project_id from media_assets').fetchone()
        self.assertEqual(tuple(row),(str(self.account),str(self.new_project)))

    async def test_read_url_reuses_owned_audio_across_projects(self):
        result=await self.read_url(self.media,str(self.user),self.pool)
        self.assertEqual(result['read_url'],'fresh-signed-url')

    async def test_read_url_foreign_user_rejected(self):
        with self.assertRaises(HTTPException):
            await self.read_url(self.media,str(uuid4()),self.pool)

    async def test_read_url_foreign_account_rejected(self):
        self.db.execute('update media_assets set account_id=?',(str(uuid4()),))
        with self.assertRaises(HTTPException):
            await self.read_url(self.media,str(self.user),self.pool)

    async def test_read_url_inactive_audio_rejected(self):
        self.db.execute("update media_assets set lifecycle_state='deleted'")
        with self.assertRaises(HTTPException):
            await self.read_url(self.media,str(self.user),self.pool)


if __name__ == '__main__':
    unittest.main()
