import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('patch',Path(__file__).parent/'ops/build-dashboard-taxonomy-patch.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class PatchTest(unittest.TestCase):
    def test_existing_helpers_pricing_and_imports_preserved_exactly(self):
        live='import os\n\n# live pricing rollout\ndef pricing():\n    return 47\n\ndef classify():\n    return None\n\n# preserve this comment\n'
        main='import math\n\ndef pricing():\n    return 99\n\ndef classify():\n    return "group_audio"\n\n'
        out=module.patch(live,main,{'classify'})
        self.assertIn('import os',out);self.assertNotIn('import math',out)
        self.assertIn('return 47',out);self.assertNotIn('return 99',out)
        self.assertIn('# preserve this comment',out)
    def test_missing_helper_added_without_replacing_other_functions(self):
        out=module.patch('def old():\n    return 1\n','def helper():\n    return 2\n',{'helper'})
        self.assertIn('def old()',out);self.assertIn('def helper()',out)
    def test_missing_approved_function_rejected(self):
        with self.assertRaises(AssertionError):module.patch('','',{'required'})
    def test_real_modules_keep_all_non_target_code(self):
        root=Path(__file__).parents[1]/'services/svc-dashboard/app/app/services'
        for filename,targets in module.TARGETS.items():
            source=((root/filename) if filename!='dashboard.py' else root.parent/'api/routes/dashboard.py').read_text()
            self.assertEqual(module.patch(source,source,targets),source)

    def test_older_production_router_keeps_auth_header_and_refresh(self):
        root=Path(__file__).parents[1]
        live=(root/'scripts/fixtures/production-dashboard-router-20261012.py').read_text()
        main=(root/'services/svc-dashboard/app/app/api/routes/dashboard.py').read_text()
        approved_import='from app.services.final_video_visibility import enrich_dashboard_home_with_v3_finals, enrich_dashboard_library_with_v3_finals'
        result=module.patch(live,main,{'dashboard_home','dashboard_library'},(approved_import,))
        for name in ['dashboard_header','dashboard_refresh']:
            self.assertEqual(module.segment(live,module.functions(live)[name]),module.segment(result,module.functions(result)[name]))
        self.assertEqual(module.protected_source(live,{'dashboard_home','dashboard_library'}).rstrip('\n'),module.protected_source(result,{'dashboard_home','dashboard_library'},(approved_import,)).rstrip('\n'))
        self.assertIn('get_current_user_id',result)
        self.assertIn('enrich_dashboard_library_with_v3_finals',result)
    def test_missing_final_module_is_added_from_main_only(self):
        import tempfile,json
        root=Path(__file__).parents[1]/'services/svc-dashboard/app/app'
        with tempfile.TemporaryDirectory() as folder:
            live=Path(folder)/'live';main=Path(folder)/'main';out=Path(folder)/'out';live.mkdir();main.mkdir()
            for filename in module.TARGETS:
                source=(root/'api/routes/dashboard.py' if filename=='dashboard.py' else root/'services'/filename).read_text()
                (main/filename).write_text(source)
                if filename!='final_video_visibility.py':(live/filename).write_text(source)
            import sys
            from unittest.mock import patch
            with patch.object(sys,'argv',['patch',str(live),str(main),str(out)]):module.main()
            self.assertEqual((out/'final_video_visibility.py').read_text(),(main/'final_video_visibility.py').read_text())
            self.assertTrue(json.loads((out/'manifest.json').read_text())['final_video_visibility.py']['added_main_module'])

if __name__=='__main__':unittest.main()
