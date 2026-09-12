from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[3]
MAIN_PATH = ROOT / "services/svc-director/app/app/main.py"
main = MAIN_PATH.read_text(encoding="utf-8")


def test_recent_story_route_is_user_scoped_and_enriched():
    ast.parse(main)
    assert '@app.get("/api/director/stories/recent", response_model=list[RecentStoryOut])' in main
    block = main.split('async def get_recent_stories', 1)[1].split(
        '@app.get("/api/director/stories/{story_id}/workspace"', 1
    )[0]

    assert "s.account_id = $1" in block
    assert "p.owner_user_id = $2" in block
    assert "r.owner_user_id = $2" in block
    assert "w.owner_user_id = $2" in block
    assert "p.lifecycle_state::text = 'active'" in block
    assert "sw.workflow_id" in block
    assert "workflow_state" in block
    assert "current_stage" in block
    assert "attention_state" in block
    assert "has_review" in block
    assert "has_failed" in block
    assert 'continue_path=f"/app/multi-person?story={story_id}"' in block
    assert "brief_json" not in block


def test_recent_route_precedes_story_id_route():
    recent = '@app.get("/api/director/stories/recent"'
    workspace = '@app.get("/api/director/stories/{story_id}/workspace"'
    assert recent in main
    assert workspace in main
    assert main.index(recent) < main.index(workspace)
