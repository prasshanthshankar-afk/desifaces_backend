import asyncio

import pytest
from fastapi import HTTPException

from app.api.routes import face_jobs


class FakeFaceConfigRepo:
    def __init__(self, pool):
        self.pool = pool

    async def list_regions(self, active_only=True):
        return [
            {
                "code": "tamil_nadu",
                "display_name": {"en": "Tamil Nadu"},
                "sub_region": "South",
                "is_active": True,
            },
            {
                "code": "geo_jp_13",
                "display_name": {"en": "Tokyo"},
                "sub_region": None,
                "is_active": True,
            },
        ]

    async def execute_queries(self, query, *args):
        if "geography_type = 'country'" in query:
            return [
                {
                    "code": "geo_country_jp",
                    "display_name": {"en": "Japan"},
                    "country_code": "JP",
                    "geography_type": "country",
                    "is_active": True,
                }
            ]

        if "geography_type = 'subdivision'" in query:
            assert args == ("JP",)
            return [
                {
                    "code": "geo_jp_13",
                    "display_name": {"en": "Tokyo"},
                    "country_code": "JP",
                    "subdivision_code": "JP-13",
                    "geography_type": "subdivision",
                    "is_active": True,
                }
            ]

        raise AssertionError(f"Unexpected query: {query}")

    @staticmethod
    def convert_db_row(row):
        return dict(row)


async def fake_get_pool():
    return object()


def install_fakes(monkeypatch):
    monkeypatch.setattr(face_jobs, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        face_jobs,
        "CreatorPlatformConfigRepo",
        FakeFaceConfigRepo,
    )


def test_legacy_regions_exclude_global_geography(monkeypatch):
    install_fakes(monkeypatch)

    items = asyncio.run(face_jobs.get_available_regions())

    assert [item.code for item in items] == ["tamil_nadu"]


def test_country_catalog_exposes_iso_country_code(monkeypatch):
    install_fakes(monkeypatch)

    items = asyncio.run(face_jobs.get_available_countries())

    assert items == [
        {
            "code": "JP",
            "country_code": "JP",
            "display_name": "Japan",
            "is_active": True,
        }
    ]


def test_subdivision_catalog_preserves_generation_region_code(monkeypatch):
    install_fakes(monkeypatch)

    items = asyncio.run(
        face_jobs.get_available_subdivisions(
            country_code="jp",
        )
    )

    assert items[0]["code"] == "geo_jp_13"
    assert items[0]["region_code"] == "geo_jp_13"
    assert items[0]["country_code"] == "JP"
    assert items[0]["subdivision_code"] == "JP-13"
    assert items[0]["display_name"] == "Tokyo"


def test_invalid_face_country_code_rejected():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            face_jobs.get_available_subdivisions(
                country_code="JPN",
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_country_code"
