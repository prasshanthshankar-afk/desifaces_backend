"""Exercise the restored canonical classification and browser reuse metadata."""
import ast
import __future__
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).parents[1] / 'services/svc-dashboard/app/app/services/dashboard_service.py'
nodes = [n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
namespace = {'json':json,'datetime':datetime,'timezone':timezone,'timedelta':timedelta,'Decimal':Decimal,'settings':SimpleNamespace()}
exec(compile(ast.Module(body=nodes,type_ignores=[]),'dashboard','exec',flags=__future__.annotations.compiler_flag),namespace)
namespace['_signed_url_from_parts'] = lambda signer, value, *args: value
namespace['_build_face_library_title'] = lambda item, meta, reuse: item.get('title','Face')
namespace['_is_recent'] = lambda *args: False


class Connection:
    def __init__(self, media=(), jobs=(), groups=()):
        self.media,self.jobs,self.groups=media,jobs,groups
        self.calls=[]
    async def fetch(self, sql, values):
        self.calls.append((sql,values))
        assert not any(token in sql.upper() for token in ['INSERT INTO','UPDATE PUBLIC','DELETE FROM'])
        if 'select distinct on (fjo.job_id)' in sql:return self.groups
        if 'workflow_final_lineage' in sql:return self.media
        return self.jobs


def lineage(media_id,stage_type='audio',mode='shared_scene',group=False,count=2):
    return {'media_id':media_id,'stage_type':stage_type,'workflow_id':'workflow-one','story_id':'story-one',
            'conversation_mode':mode,'speaker_count':count,'is_group_photo':group}


class TaxonomyTest(unittest.TestCase):
    def normalize(self,item):
        return namespace['_normalize_library_item'](item,None)
    def classify(self,items,conn):
        return asyncio.run(namespace['_attach_canonical_asset_lineage'](conn,items))
    def asset(self,studio,identity='asset-one'):
        return {'studio':studio,'library_id':identity,'media_asset_id':identity,'source_job_id':'job-one',
                'preview_url':'https://media.example/asset','reuse_payload_json':{'voice_id':'voice-one','locale':'en-US'}}

    def test_group_audio_keeps_voice_identity_and_workflow(self):
        item=self.asset('audio')
        self.classify([item],Connection(media=[lineage('asset-one')]))
        result=self.normalize(item)
        self.assertEqual(result['asset_class'],'group_audio')
        self.assertEqual(result['conversation_kind'],'group_photo_conversation')
        self.assertEqual(result['reuse_payload']['story_id'],'story-one')
        self.assertEqual(result['reuse_payload']['voice_id'],'voice-one')
        self.assertEqual(result['reuse_payload']['locale'],'en-US')
        self.assertEqual(result['media_asset_id'],'asset-one')

    def test_group_photo_sibling_variants_are_images_not_single_faces(self):
        items=[self.asset('face','first'),self.asset('face','second')]
        conn=Connection(groups=[{'source_job_id':'job-one','workflow_id':'workflow-one','story_id':'story-one','speaker_count':2}])
        self.classify(items,conn)
        for item in items:
            result=self.normalize(item)
            self.assertEqual(result['asset_class'],'group_photo')
            self.assertEqual(result['reuse_payload']['intended_reuse'],'group_photo_conversation')
            self.assertEqual(result['participant_count'],2)
            self.assertEqual(result['media_asset_id'],item['media_asset_id'])

    def test_group_creation_metadata_works_before_workflow_selection(self):
        item=self.asset('face');item['metadata_json']={'asset_class':'group_photo','participant_count':2}
        self.classify([item],Connection())
        result=self.normalize(item)
        self.assertEqual(result['asset_class'],'group_photo')
        self.assertEqual(result['participant_count'],2)

    def test_saved_generation_contract_classifies_unselected_group_variants(self):
        item=self.asset('face');item['metadata_json']={'job_payload':{'subject_composition_code':'two_people','subjects':[{'relationship_role':'conversation participant'},{'relationship_role':'conversation participant'}]}}
        self.classify([item],Connection())
        self.assertEqual(self.normalize(item)['asset_class'],'group_photo')

    def test_final_workflow_video_does_not_require_stage_output_link(self):
        item=self.asset('video');item.pop('source_job_id')
        conn=Connection(media=[lineage('asset-one','story_final')])
        self.classify([item],conn)
        result=self.normalize(item)
        self.assertEqual(result['asset_class'],'group_video')
        self.assertEqual(result['reuse_payload']['story_id'],'story-one')
        self.assertEqual(result['preview_url'],item['preview_url'])
        self.assertIn('w.final_media_id::text',conn.calls[0][0])

    def test_separate_face_conversation_remains_a_distinct_category(self):
        for studio,stage_type,expected in [('audio','audio','multi_person_audio'),('video','fusion','multi_person_video'),('face','face','multi_person_face')]:
            with self.subTest(studio=studio):
                item=self.asset(studio)
                self.classify([item],Connection(media=[lineage('asset-one',stage_type,'ordered_speaker_shots')]))
                result=self.normalize(item)
                self.assertEqual(result['asset_class'],expected)
                self.assertEqual(result['conversation_kind'],'multi_person_conversation')

    def test_unrelated_single_person_assets_are_not_guessed_from_titles(self):
        for studio in ['face','audio','video']:
            item=self.asset(studio);item['title']='Group conversation family voices'
            self.classify([item],Connection())
            result=self.normalize(item)
            self.assertIsNone(result['asset_class'])
            self.assertIsNone(result['workflow_kind'])
            self.assertEqual(result['media_asset_id'],'asset-one')

    def test_conversation_mode_can_be_resolved_from_provider_attempt(self):
        item=self.asset('audio');item.pop('media_asset_id')
        record=lineage('ignored');record['source_job_id']='job-one'
        self.classify([item],Connection(jobs=[record]))
        self.assertEqual(self.normalize(item)['asset_class'],'group_audio')


if __name__=='__main__':unittest.main()
