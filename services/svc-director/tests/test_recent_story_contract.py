from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
main = (ROOT / "services/svc-director/app/app/main.py").read_text()
store = (ROOT / "services/svc-director/app/app/run_store.py").read_text()


def test_recent_story_route_is_user_scoped_and_uses_canonical_story_id_navigation():
    assert '@app.get("/api/director/stories/recent"' in main
    assert 'owner_user_id=auth.user_id' in main
    assert 'account_id=auth.account_id' in main
    assert 'continue_path=f"/app/multi-person?story_id=' in main
    assert '?story=' not in main.split('async def recent_stories', 1)[1].split('@app.get("/api/director/stories/{story_id}/workspace"', 1)[0]
    assert 'brief_json' not in main.split('async def recent_stories', 1)[1].split('@app.get("/api/director/stories/{story_id}/workspace"', 1)[0]


def test_recent_story_contract_exposes_bounded_live_studio_projection():
    model = main.split('class RecentStoryView', 1)[1].split('def _coerce_interrupt_value', 1)[0]
    for field in ('workflow_id:', 'workflow_state:', 'current_stage:', 'attention_state:'):
        assert field in model

    block = store.split('async def list_recent', 1)[1].split('async def queue_resume', 1)[0]
    assert 'distinct on (r.story_id)' in block
    assert 'r.account_id=$1' in block
    assert 'r.owner_user_id=$2' in block
    assert 'wf.account_id=$1' in block
    assert 'wf.owner_user_id=$2' in block
    assert "s.state='awaiting_review'" in block
    assert "s.state='failed'" in block
    assert "s.state='generating'" in block
    assert 'attention_state' in block
    assert 'brief_json->>' in block
    assert 'brief_json,' not in block


def test_assistant_context_remains_authorized_and_bounded():
    block = main.split('async def get_story_assistant_context', 1)[1]
    assert 'auth: DirectorAuthContext = Depends(get_director_auth)' in block
    assert 'account_id=auth.account_id' in block
    assert 'load_story_studio_projection' in block
    assert 'build_creation_context' in block
    assert 'allowed_assistant_actions=' in block
