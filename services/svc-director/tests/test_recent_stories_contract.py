from pathlib import Path

path = (
    Path(__file__).parents[1]
    / "app"
    / "app"
    / "main.py"
)

text = path.read_text(encoding="utf-8")

recent = '@app.get("/api/director/stories/recent"'
workspace = '@app.get("/api/director/stories/{story_id}/workspace"'

assert recent in text
assert workspace in text
assert text.index(recent) < text.index(workspace)

assert "s.account_id = $1" in text
assert "p.owner_user_id = $2" in text
assert "r.owner_user_id = $2" in text
assert "w.owner_user_id = $2" in text

assert 'f"/app/multi-person?story={story_id}"' in text

print("DIRECTOR_RECENT_STORIES_SOURCE_CONTRACT=PASS")
