"""Remove only the canonical audio origin-project rejection from live source."""
import ast
import hashlib
import sys
from pathlib import Path

EXPECTED_LIVE = '33808cdc60303a2dc7b8f72b90664391aa814ea785b903ce25041886687bc547'


def patch(source):
    tree = ast.parse(source)
    found = []
    for fn in tree.body:
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == 'get_canonical_audio_output':
            for node in ast.walk(fn):
                if isinstance(node,ast.If) and len(node.body)==1 and isinstance(node.body[0],ast.Raise):
                    segment=ast.get_source_segment(source,node)
                    if 'audio_canonical_media_project_mismatch' in segment:
                        assert ast.unparse(node.test) == "existing['project_id'] and UUID(str(existing['project_id'])) != project_id"
                        assert not node.orelse
                        found.append(node)
    assert len(found)==1
    node=found[0]
    lines=source.splitlines(keepends=True)
    lines[node.lineno-1:node.end_lineno]=[
        '            # The original project is provenance. Owned audio may be reused in\n',
        '            # another project in the same account without recreating the audio.\n',
    ]
    result=''.join(lines)
    ast.parse(result)
    assert 'audio_canonical_media_account_mismatch' in result
    return result


if __name__=='__main__':
    src,dst=map(Path,sys.argv[1:])
    assert hashlib.sha256(src.read_bytes()).hexdigest()==EXPECTED_LIVE, 'live Audio source drift'
    dst.write_text(patch(src.read_text()))
    print('AUDIO_REUSE_PATCH_BOUNDARY=PASS')
