"""Read-only certification of the actual failing production media/workflow."""
import ast
import asyncio
from pathlib import Path
from uuid import UUID
import asyncpg
from app.config import settings

PHOTO = UUID('d9358749-c771-481d-b219-1b816d5b558f')
WORKFLOW = UUID('f4bd196c-704a-40e0-8892-d84018b0c7e7')


async def main():
    source = Path('/app/app/shared_scene_routes.py').read_text()
    queries = []
    for fn in ast.parse(source).body:
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name in {'set_shared_scene_draft','set_shared_scene_conversation'}:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'fetchrow' and node.args and isinstance(node.args[0], ast.Constant):
                    sql = node.args[0].value
                    if isinstance(sql, str) and 'from public.media_assets' in sql:
                        assert len(node.args) == 4
                        queries.append(sql.replace('for update',''))
    assert len(queries) == 2
    c = await asyncpg.connect(settings.DATABASE_URL.replace('postgresql+asyncpg://','postgresql://'), timeout=10, server_settings={'statement_timeout':'15000'})
    try:
        async with c.transaction(readonly=True):
            workflow = await c.fetchrow('select owner_user_id,account_id,project_id from public.v3_studio_workflows where workflow_id=$1', WORKFLOW)
            assert workflow, 'reported workflow missing'
            original = await c.fetchrow('select project_id,deleted_at,meta_json from public.media_assets where id=$1', PHOTO)
            assert original and original['deleted_at'] is None
            assert original['project_id'] != workflow['project_id'], 'cross-project reproduction changed'
            for sql in queries:
                row = await c.fetchrow(sql, PHOTO, workflow['owner_user_id'], workflow['account_id'])
                assert row and row['project_id'] == original['project_id'], 'owned saved photo rejected or original lineage changed'
                assert await c.fetchrow(sql, PHOTO, UUID(int=0), workflow['account_id']) is None
                assert await c.fetchrow(sql, PHOTO, workflow['owner_user_id'], UUID(int=0)) is None
            print('REPORTED_PHOTO_DRAFT_AND_APPROVAL_LOOKUPS=PASS')
            print('FOREIGN_USER_AND_ACCOUNT_REJECTED=PASS')
            print('ORIGINAL_PHOTO_PROJECT_PRESERVED=PASS')
    finally:
        await c.close()


asyncio.run(main())
