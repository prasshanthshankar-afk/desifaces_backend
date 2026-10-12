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
            source=(root/filename).read_text()
            self.assertEqual(module.patch(source,source,targets),source)

if __name__=='__main__':unittest.main()
