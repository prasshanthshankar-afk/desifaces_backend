"""Patch only the two media lookups on a source-hash-guarded live Director."""
import ast
import hashlib
import sys
from pathlib import Path

EXPECTED_LIVE = '527f373a33c7c8f5cccb7b1f2d88403953a1a9bac697693674c0bc5750959190'
FUNCTIONS = {'set_shared_scene_draft', 'set_shared_scene_conversation'}


def patch(source):
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    edits = []
    found = set()
    for function in tree.body:
        if not isinstance(function, ast.AsyncFunctionDef) or function.name not in FUNCTIONS:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'fetchrow':
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            sql = node.args[0].value
            if not isinstance(sql, str) or 'from public.media_assets' not in sql:
                continue
            assert len(node.args) == 5
            assert "and (project_id is null or project_id=$4)" in sql
            assert ast.unparse(node.args[4]) == "stage['project_id']"
            assert sql.count('and (project_id is null or project_id=$4)') == 1
            sql_start = node.args[0].lineno - 1
            sql_end = node.args[0].end_lineno
            for index in range(sql_start, sql_end):
                if 'and (project_id is null or project_id=$4)' in lines[index]:
                    edits.append(index)
            assert node.args[4].lineno == node.args[4].end_lineno
            edits.append(node.args[4].lineno - 1)
            assert function.name not in found
            found.add(function.name)
    assert found == FUNCTIONS and len(edits) == 4
    for index in sorted(edits, reverse=True):
        del lines[index]
    result = ''.join(lines)
    result = result.replace(
        '# Existing non-NULL lineage is never overwritten; mismatches are\n'
        '            # rejected by the SELECT predicate above.',
        '# Existing non-NULL lineage is never overwritten. A saved photo\n'
        '            # may be reused by its owner in another project in the same account;\n'
        '            # project_id records its origin, not the current conversation.',
    )
    ast.parse(result)
    return result


if __name__ == '__main__':
    src, dst = map(Path, sys.argv[1:])
    assert hashlib.sha256(src.read_bytes()).hexdigest() == EXPECTED_LIVE, 'live Director source drift'
    dst.write_text(patch(src.read_text()))
    print('SHARED_PHOTO_PATCH_BOUNDARY=PASS')
