from pathlib import Path
import ast

ROOT = Path(__file__).parents[1]
ROUTE = ROOT / "app" / "app" / "api" / "routes" / "v3_audio_output.py"
STORAGE = ROOT / "app" / "app" / "services" / "azure_storage_service.py"

route_text = ROUTE.read_text(encoding="utf-8")
storage_text = STORAGE.read_text(encoding="utf-8")

# Syntax gate without importing runtime configuration/provider dependencies.
ast.parse(route_text)
ast.parse(storage_text)

# The public mobile/Web read-url contract must resolve the durable media identity,
# not a generation-time SAS URL stored in metadata.
assert '@router.get("/assets/{media_id}/read-url"' in route_text
assert "select id,account_id,storage_ref,meta_json" in route_text
assert 'storage_ref = str(row["storage_ref"] or "").strip()' in route_text
assert "AzureStorageService().generate_read_url(storage_ref)" in route_text
assert "audio_read_url_unavailable" in route_text

# Fresh signing must support historical durable representations encountered in V3.
assert "def _resolve_read_coordinates" in storage_text
assert 'raw.startswith("az://") or raw.startswith("azure://")' in storage_text
assert 'raw.startswith("https://") or raw.startswith("http://")' in storage_text
assert "def generate_read_url" in storage_text
assert "BlobSasPermissions(read=True)" in storage_text

# Guard against regressing the read path to a persisted, expiring source URL.
read_handler = route_text.split(
    '@router.get("/assets/{media_id}/read-url"', 1
)[1]
assert "source_audio_url" not in read_handler
