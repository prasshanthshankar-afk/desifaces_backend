"""Transplant main's taxonomy functions while preserving every other live byte."""
import ast
import hashlib
import json
from pathlib import Path
import sys

TARGETS = {
    'dashboard_service.py': {'_attach_canonical_asset_lineage', '_fetch_library_view_rows', '_normalize_library_item', '_library_conversation_mode'},
    'final_video_visibility.py': {'_fetch_canonical_v3_final_items'},
    'dashboard.py': {'dashboard_home','dashboard_library'},
}


def functions(source):
    return {node.name: node for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def segment(source, node):
    return ''.join(source.splitlines(keepends=True)[node.lineno-1:node.end_lineno])


def protected_source(source, targets, allowed_imports=()):
    lines=source.splitlines(keepends=True)
    for node in sorted(functions(source).values(), key=lambda node: node.lineno, reverse=True):
        if node.name in targets:
            del lines[node.lineno-1:node.end_lineno]
    result=''.join(lines)
    for statement in allowed_imports:
        result=result.replace(statement+'\n','')
    return result


def patch(live, main, targets, allowed_imports=()):
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
    for statement in allowed_imports:
        required=ast.parse(statement).body[0]
        existing={alias.asname or alias.name for node in ast.parse(result).body if isinstance(node,ast.ImportFrom) and node.module==required.module for alias in node.names}
        if not {alias.name for alias in required.names}.issubset(existing):
            result=result.rstrip('\n')+'\n\n'+statement+'\n'
    compile(result,'patched','exec')
    # Ignore only added boundary blank lines. All imports and non-target bytes match.
    assert protected_source(result,targets,allowed_imports).rstrip('\n')==protected_source(live,targets,allowed_imports).rstrip('\n'), 'protected code changed'
    for name in targets:
        assert ast.dump(functions(result)[name])==ast.dump(approved[name]), 'main function mismatch: '+name
    return result


def main():
    live_dir,main_dir,out_dir=map(Path,sys.argv[1:])
    out_dir.mkdir(parents=True,exist_ok=True)
    manifest={}
    for filename,targets in TARGETS.items():
        path=live_dir/filename
        live=path.read_text() if path.exists() else ''
        approved=(main_dir/filename).read_text()
        imports=('from app.services.final_video_visibility import enrich_dashboard_home_with_v3_finals, enrich_dashboard_library_with_v3_finals',) if filename=='dashboard.py' else ()
        added=not path.exists()
        assert not added or filename=='final_video_visibility.py','unexpected missing production module'
        result=approved if added else patch(live,approved,targets,imports)
        (out_dir/filename).write_text(result)
        manifest[filename]={'added_main_module':added,'live_sha256':hashlib.sha256(live.encode()).hexdigest(),'patched_sha256':hashlib.sha256(result.encode()).hexdigest(),'main_function_names':sorted(targets),'protected_source_sha256':hashlib.sha256(protected_source(live,targets,imports).rstrip('\n').encode()).hexdigest()}
    (out_dir/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print('DASHBOARD_NON_TAXONOMY_BYTES_PRESERVED=PASS')
    print('DASHBOARD_TAXONOMY_MAIN_FUNCTIONS=PASS')


if __name__=='__main__':main()
