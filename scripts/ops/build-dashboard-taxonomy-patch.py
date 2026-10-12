"""Transplant main's taxonomy functions while preserving every other live byte."""
import ast
import hashlib
import json
from pathlib import Path
import sys

TARGETS = {
    'dashboard_service.py': {'_attach_canonical_asset_lineage', '_fetch_library_view_rows', '_normalize_library_item', '_library_conversation_mode'},
    'final_video_visibility.py': {'_fetch_canonical_v3_final_items'},
}


def functions(source):
    return {node.name: node for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def segment(source, node):
    return ''.join(source.splitlines(keepends=True)[node.lineno-1:node.end_lineno])


def protected_source(source, targets):
    lines=source.splitlines(keepends=True)
    for node in sorted(functions(source).values(), key=lambda node: node.lineno, reverse=True):
        if node.name in targets:
            del lines[node.lineno-1:node.end_lineno]
    return ''.join(lines)


def patch(live, main, targets):
    current, approved=functions(live),functions(main)
    assert targets.issubset(approved), 'approved source missing functions'
    lines=live.splitlines(keepends=True)
    for name,node in sorted(current.items(), key=lambda entry:entry[1].lineno, reverse=True):
        if name in targets:
            lines[node.lineno-1:node.end_lineno]=[segment(main,approved[name])]
    result=''.join(lines)
    # A missing helper is inserted at a fixed boundary, avoiding unrelated imports.
    additions=[segment(main,approved[name]) for name in sorted(targets-current.keys())]
    if additions:
        result=result.rstrip('\n')+'\n\n'+ '\n\n'.join(additions)+'\n'
    compile(result,'patched','exec')
    # Ignore only added boundary blank lines. All imports and non-target bytes match.
    assert protected_source(result,targets).rstrip('\n')==protected_source(live,targets).rstrip('\n'), 'protected code changed'
    for name in targets:
        assert ast.dump(functions(result)[name])==ast.dump(approved[name]), 'main function mismatch: '+name
    return result


def main():
    live_dir,main_dir,out_dir=map(Path,sys.argv[1:])
    out_dir.mkdir(parents=True,exist_ok=True)
    manifest={}
    for filename,targets in TARGETS.items():
        live=(live_dir/filename).read_text();approved=(main_dir/filename).read_text()
        result=patch(live,approved,targets)
        (out_dir/filename).write_text(result)
        manifest[filename]={'live_sha256':hashlib.sha256(live.encode()).hexdigest(),'patched_sha256':hashlib.sha256(result.encode()).hexdigest(),'main_function_names':sorted(targets),'protected_source_sha256':hashlib.sha256(protected_source(live,targets).rstrip('\n').encode()).hexdigest()}
    (out_dir/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print('DASHBOARD_NON_TAXONOMY_BYTES_PRESERVED=PASS')
    print('DASHBOARD_TAXONOMY_MAIN_FUNCTIONS=PASS')


if __name__=='__main__':main()
