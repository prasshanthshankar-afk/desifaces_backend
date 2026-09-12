from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
main = (ROOT / "services/svc-director/app/app/main.py").read_text()
store = (ROOT / "services/svc-director/app/app/run_store.py").read_text()


def test_recent_story_route_is_user_scoped_and_internal():
    assert '@app.get("/api/director/stories/recent"' in main
    assert 'owner_user_id=auth.user_id' in main
    assert 'account_id=auth.account_id' in main
    assert 'continue_path=f"/app/multi-person?story=' in main
    assert 'brief_json' not in main.split('async def recent_stories', 1)[1].split('@app.get("/api/director/stories/{story_id}/workspace"', 1)[0]


def test_store_filters_account_and_owner_and_never_returns_raw_brief():
    block = store.split('async def list_recent', 1)[1].split('async def queue_resume', 1)[0]
    assert 'where account_id=$1 and owner_user_id=$2 and story_id is not null' in block
    assert 'brief_json->>' in block
    assert 'select run_id,thread_id,story_id,state,created_at,updated_at' in block
    assert 'brief_json,' not in block
    assert 'order by coalesce(updated_at, created_at) desc' in block
