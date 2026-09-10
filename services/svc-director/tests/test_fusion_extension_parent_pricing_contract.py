from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DIRECTOR = ROOT / "services/svc-director/app/app/fusion_execution_parent_pricing.py"
EXT_MAIN = ROOT / "services/svc-fusion-extension/app/app/main.py"
EXT_PRICING = ROOT / "services/svc-fusion-extension/app/app/api/routes/v3_scene_pricing.py"


def test_director_and_fusion_extension_share_parent_scene_pricing_contract():
    director = DIRECTOR.read_text()
    ext_main = EXT_MAIN.read_text()
    ext_pricing = EXT_PRICING.read_text()

    assert '"/api/longform/v3/scene-pricing/preview"' in director
    assert '"/api/longform/v3/scene-pricing/reserve"' in director
    assert '"/api/longform/v3/scene-pricing/commit"' in director
    assert '"/api/longform/v3/scene-pricing/release"' in director

    assert "from app.api.routes.v3_scene_pricing import router as v3_scene_pricing_router" in ext_main
    assert "app.include_router(v3_scene_pricing_router)" in ext_main
    assert 'APIRouter(prefix="/api/longform/v3/scene-pricing"' in ext_pricing
    for suffix in ("/preview", "/reserve", "/commit", "/release"):
        assert f'@router.post("{suffix}"' in ext_pricing


def test_parent_scene_pricing_remains_one_logical_scene_quote():
    director = DIRECTOR.read_text()
    ext_pricing = EXT_PRICING.read_text()

    assert '"pricing_suppressed": True' in director
    assert '"billing_mode": "internal"' in director
    assert '_PRICING_KEY = "fusion_parent_pricing"' in ext_pricing
    assert '_SERVICE_ACTION = "fusion.video.generate"' in ext_pricing
    assert '_LEAF_SKU_CODE = "FUSION_TALK_MIN"' in ext_pricing
