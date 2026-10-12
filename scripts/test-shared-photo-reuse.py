"""Execute the actual route lookup SQL against owner/account/project fixtures."""
import ast
import os
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTE = Path(os.environ.get('SHARED_PHOTO_ROUTE', str(ROOT / 'services/svc-director/app/app/shared_scene_routes.py')))


def lookups(source):
    result = {}
    for fn in ast.parse(source).body:
        if not isinstance(fn, ast.AsyncFunctionDef) or fn.name not in {'set_shared_scene_draft', 'set_shared_scene_conversation'}:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'fetchrow' and node.args and isinstance(node.args[0], ast.Constant):
                sql = node.args[0].value
                if isinstance(sql, str) and 'from public.media_assets' in sql:
                    assert len(node.args) == 4
                    result[fn.name] = sql.replace('public.media_assets', 'media_assets').replace('for update', '')
    assert len(result) == 2
    return result


class SavedPhotoReuseTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('create table media_assets(id,user_id,account_id,project_id,kind,lifecycle_state,meta_json)')
        self.queries = lookups(ROUTE.read_text())

    def tearDown(self):
        self.db.close()

    def check_media(self, owner='owner', account='account', project='old-project', kind='face_image', state='active', accepted=True):
        self.db.execute('insert into media_assets values(?,?,?,?,?,?,?)', ('photo',owner,account,project,kind,state,'{}'))
        for name, sql in self.queries.items():
            with self.subTest(route=name):
                row = self.db.execute(sql, {'1':'photo','2':'owner','3':'account'}).fetchone()
                self.assertEqual(row is not None, accepted)

    def test_owned_photo_from_another_project_is_reusable(self):
        self.check_media()

    def test_existing_project_is_preserved(self):
        self.check_media()
        self.assertEqual(self.db.execute('select project_id from media_assets').fetchone()[0], 'old-project')
        self.assertIn('project_id=coalesce(project_id,$3)', ROUTE.read_text())

    def test_legacy_null_lineage_is_accepted(self):
        self.check_media(account=None, project=None)

    def test_other_user_is_rejected(self):
        self.check_media(owner='other-owner', accepted=False)

    def test_other_account_is_rejected(self):
        self.check_media(account='other-account', accepted=False)

    def test_inactive_photo_is_rejected(self):
        self.check_media(state='deleted', accepted=False)

    def test_non_image_is_rejected(self):
        self.check_media(kind='audio', accepted=False)

    def test_unknown_photo_is_rejected(self):
        for sql in self.queries.values():
            self.assertIsNone(self.db.execute(sql, {'1':'missing','2':'owner','3':'account'}).fetchone())

    def test_safety_and_mapping_gates_remain(self):
        source = ROUTE.read_text()
        for gate in ['shared_scene_media_validation_required','shared_scene_media_validation_failed','shared_scene_media_speaker_count_validation_mismatch','shared_scene_target_not_scene_participant','shared_scene_speaker_target_mismatch']:
            self.assertIn(gate, source)


if __name__ == '__main__':
    unittest.main()
