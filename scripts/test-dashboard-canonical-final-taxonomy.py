"""Canonical final enrichment must keep signed media and conversation lineage."""
import ast, asyncio, __future__, json, os, sys, types, unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

SOURCE=Path(__file__).parents[1]/'services/svc-dashboard/app/app/services/final_video_visibility.py'
nodes=[n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
namespace={'os':os,'datetime':datetime,'timezone':timezone,'urlparse':urlparse}
exec(compile(ast.Module(body=nodes,type_ignores=[]),'final','exec',flags=__future__.annotations.compiler_flag),namespace)

class Connection:
    def __init__(self):self.queries=[]
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def fetch(self,sql,*args):
        self.queries.append(sql)
        return [{'library_id':'v3-final:media-one','media_asset_id':'media-one','studio':'video','preview_url':'video-output/final.mp4','download_url':'video-output/final.mp4','thumbnail_url':'face-output/poster.png','reuse_payload_json':{'story_id':'story-one','v3_workflow_id':'workflow-one'}}]
class Pool:
    def __init__(self):self.conn=Connection()
    def acquire(self):return self.conn

class TestCanonical(unittest.TestCase):
    def run_query(self,fail=False):
        settings=types.ModuleType('app.settings');settings.settings=types.SimpleNamespace(AZURE_STORAGE_CONNECTION_STRING='stub')
        signer=types.ModuleType('app.services.blob_sas_service');signer.AzureBlobSasSigner=types.SimpleNamespace(from_connection_string=lambda _:None)
        dashboard=types.ModuleType('app.services.dashboard_service')
        async def attach(conn,items):
            if fail:raise RuntimeError('transient taxonomy lookup')
            for item in items:item.update(asset_class='group_video',conversation_kind='group_photo_conversation')
            return items
        def normalize(item,_):
            return {**item,'preview_url':'https://media.example/final.mp4?sig=signed','thumbnail_url':'https://media.example/poster.png?sig=signed','reuse_payload':{**item['reuse_payload_json'],'video_url':'video-output/final.mp4'}}
        dashboard._attach_canonical_asset_lineage=attach;dashboard._normalize_library_item=normalize
        pool=Pool()
        with patch.dict(sys.modules,{'app.settings':settings,'app.services.blob_sas_service':signer,'app.services.dashboard_service':dashboard}):
            result=asyncio.run(namespace['_fetch_canonical_v3_final_items'](pool,'user-one'))
        return result,pool
    def test_canonical_final_classification_keeps_signed_reuse_url(self):
        result,pool=self.run_query();item=result[0]
        self.assertEqual(item['asset_class'],'group_video')
        self.assertEqual(item['reuse_payload']['story_id'],'story-one')
        self.assertEqual(item['preview_url'],item['reuse_payload']['video_url'])
        self.assertTrue(item['canonical_final'])
        sql=pool.conn.queries[0]
        self.assertIn("w.owner_user_id = $1::uuid",sql)
        self.assertIn("lower(coalesce(w.state, '')) = 'completed'",sql)
        self.assertIn('m.deleted_at is null',sql)
    def test_classifier_outage_does_not_hide_existing_final(self):
        with self.assertLogs(namespace['_fetch_canonical_v3_final_items'].__module__,level='ERROR'):
            result,_=self.run_query(True)
        self.assertEqual(len(result),1)
        self.assertTrue(result[0]['canonical_final'])
        self.assertTrue(result[0]['preview_url'].startswith('https://'))

if __name__=='__main__':unittest.main()
