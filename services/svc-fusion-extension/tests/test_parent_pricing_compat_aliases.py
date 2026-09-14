from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "services" / "svc-fusion-extension" / "app" / "app"


def test_parent_pricing_compat_aliases_delegate_to_canonical_handler():
    compat = (APP / "api" / "routes" / "pricing_compat.py").read_text()
    main = (APP / "main.py").read_text()

    assert '"/api/longform/jobs/pricing/preview"' in compat
    assert '"/api/fusion/jobs/pricing/preview"' in compat
    assert '"/jobs/pricing/preview"' in compat
    assert "return await preview_longform(" in compat
    assert "pricing_compat_router" in main
    assert "app.include_router(pricing_compat_router)" in main
