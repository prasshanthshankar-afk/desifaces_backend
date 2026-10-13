"""Run actual context loading/compiler bodies with controlled DB boundaries."""
import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'app/app/audio_execution.py'
tree = ast.parse(SOURCE.read_text())
names = {'AudioStageContext','load_audio_stage_context','compile_context_audio_input','_as_dict','_clean','_explicit_gender'}
module = ast.Module(body=[node for node in tree.body if getattr(node,'name',None) in names], type_ignores=[])
ns = {'dataclass':dataclass, 'UUID':UUID, 'Any':object,
      'ParticipantAudioBridgeError':RuntimeError,
      'evaluate_text_policy':lambda *args, **kwargs:SimpleNamespace(allow=True)}
exec(compile(module,str(SOURCE),'exec'),ns)


class Connection:
    def __init__(self, row, neighbors):
        self.row,self.neighbors,self.calls=row,neighbors,[]
    async def fetchrow(self, sql, *args):
        self.calls.append((sql,args));return self.row
    async def fetch(self, sql, *args):
        self.calls.append((sql,args));return self.neighbors


def fixture():
    row={key:uuid4() for key in ('workflow_id','stage_run_id','account_id','owner_user_id','project_id','story_id','dialogue_turn_id','scene_id','speaker_participant_id')}
    row.update(stage_type='audio',scope_type='dialogue_turn',state='ready',current_stage='audio',
      stage_metadata={'conversation_settings':{'translate':True,'target_locale':'hi-IN','source_language':'en-US'}},
      sequence_no=2,text_value='I have!',turn_locale='en-US',emotion_code=None,delivery_json={'rate':1.0},
      display_name='Maya',default_locale='hi-IN',voice_profile_ref='priya',voice_locale='hi-IN',
      persona_json={'gender':'female'},participant_metadata={},story_locale='en-US')
    return row


class DialogueContextTests(unittest.IsolatedAsyncioTestCase):
    async def load(self, row, neighbors):
        self.conn=Connection(row,neighbors)
        return await ns['load_audio_stage_context'](self.conn,account_id=row['account_id'],workflow_id=row['workflow_id'],stage_run_id=row['stage_run_id'])

    async def test_neighboring_question_reaches_current_turn_without_becoming_spoken_text(self):
        row=fixture()
        context=await self.load(row,[{'sequence_no':1,'display_name':'Arjun','text_value':'Have you seen the Navaratri announcements?'}])
        payload=ns['compile_context_audio_input'](context)
        self.assertEqual(payload['text'],'I have!')
        self.assertIn('Have you seen',payload['context'])
        self.assertEqual(payload['voice_id'],'priya')
        self.assertEqual(payload['speaker_gender'],'female')
        self.assertEqual(payload['target_locale'],'hi-IN')
        sql,args=self.conn.calls[1]
        self.assertIn('neighbor.workflow_id=$4',sql)
        self.assertIn('dt.scene_id=$1',sql)
        self.assertIn('limit 3',sql)
        self.assertEqual(args[-1],row['workflow_id'])

    async def test_neighboring_turns_are_ordered_and_labeled(self):
        row=fixture()
        context=await self.load(row,[{'sequence_no':3,'display_name':'Arjun','text_value':'Me too.'},
            {'sequence_no':1,'display_name':'Arjun','text_value':'Have you seen it?'}])
        self.assertLess(context.translation_context.index('Previous'),context.translation_context.index('Following'))

    async def test_long_neighbors_stay_within_existing_request_limit(self):
        row=fixture()
        neighbors=[{'sequence_no':i,'display_name':'Speaker'*100,'text_value':'x'*5000} for i in (1,3,4)]
        context=await self.load(row,neighbors)
        payload=ns['compile_context_audio_input'](context)
        self.assertLessEqual(len(payload['context']),2000)
        self.assertEqual(payload['text'],row['text_value'])

    async def test_nontranslated_audio_keeps_context_metadata_without_neighbors(self):
        row=fixture();row['stage_metadata']['conversation_settings']['translate']=False
        context=await self.load(row,[{'sequence_no':1,'display_name':'Arjun','text_value':'PRIVATE_NEIGHBOR'}])
        payload=ns['compile_context_audio_input'](context)
        self.assertNotIn('PRIVATE_NEIGHBOR',payload['context'])
        self.assertEqual(payload['text'],'I have!')

    async def test_unowned_stage_is_rejected_before_neighbor_query(self):
        row=fixture();conn=Connection(None,[])
        with self.assertRaisesRegex(RuntimeError,'audio_stage_not_found_or_account_mismatch'):
            await ns['load_audio_stage_context'](conn,account_id=row['account_id'],workflow_id=row['workflow_id'],stage_run_id=row['stage_run_id'])
        self.assertEqual(len(conn.calls),1)


if __name__=='__main__':unittest.main()
