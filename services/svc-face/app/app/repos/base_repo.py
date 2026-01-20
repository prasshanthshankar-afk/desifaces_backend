from __future__ import annotations

import asyncpg
import json
import logging
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

JsonLike = Union[Dict[str, Any], List[Any]]


# =============================================================================
# Public helpers (IMPORTABLE)
# =============================================================================
def coerce_json_value(value: Any, *, default: Any) -> Any:
    """
    Converts DB/config 'json-ish' values into real JSON-compatible Python objects.

    Handles:
    - dict/list -> returns as-is
    - valid JSON string -> parsed
    - Postgres array literal string ("{a,b,c}") -> ["a","b","c"]
    - CSV-ish string ("a, b, c") -> ["a","b","c"]
    - plain string -> {"raw": "..."} OR ["..."] depending on default shape
    - None -> default
    """
    if value is None:
        return default

    if isinstance(value, (dict, list)):
        return value

    if isinstance(value, (int, float, bool)):
        return value

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default

        # Try JSON parse first
        try:
            parsed = json.loads(s)
            return parsed
        except Exception:
            pass

        # Postgres array literal heuristic: {a,b,c}
        # This shows up when older schema stores arrays into text/json-ish fields.
        if s.startswith("{") and s.endswith("}") and len(s) < 2000:
            inner = s[1:-1].strip()
            if inner == "":
                return [] if isinstance(default, list) else default
            items = [it.strip().strip('"').strip("'") for it in inner.split(",") if it.strip()]
            if items:
                # Preserve expected shape
                if isinstance(default, list):
                    return items
                if isinstance(default, dict):
                    return {"items": items}
                return items

        # CSV-ish heuristic
        if "," in s and len(s) < 500:
            parts = [p.strip() for p in s.split(",") if p.strip()]
            if parts:
                return parts

        # Fallback: preserve shape
        if isinstance(default, list):
            return [s]
        if isinstance(default, dict):
            return {"raw": s}
        return s

    return default


# =============================================================================
# Converter
# =============================================================================
class DatabaseTypeConverter:
    """PostgreSQL -> Python conversion helpers (mismatch-tolerant)."""

    @staticmethod
    def convert_uuid_to_string(value: Any) -> Optional[str]:
        if value is None:
            return None
        # asyncpg returns uuid.UUID objects; they have 'hex'
        if hasattr(value, "hex"):
            return str(value)
        if isinstance(value, str):
            return value
        raise ValueError(f"Cannot convert UUID value: {value} (type: {type(value)})")

    @staticmethod
    def convert_text_array_to_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]

        if isinstance(value, str):
            s = value.strip()
            if not s:
                return []

            # Postgres array format: {a,b,c}
            if s.startswith("{") and s.endswith("}"):
                inner = s[1:-1].strip()
                if not inner:
                    return []
                # naive split (good enough for your current usage)
                items = inner.split(",")
                return [it.strip().strip('"').strip("'") for it in items if it.strip()]

            # If it looks like CSV, treat it as CSV
            if "," in s and len(s) < 500:
                return [p.strip() for p in s.split(",") if p.strip()]

            return [s]

        return []

    @staticmethod
    def convert_jsonish(value: Any, *, default: Any) -> Any:
        """
        Generic JSON-ish conversion using coerce_json_value (keeps list vs dict).
        """
        return coerce_json_value(value, default=default)


# =============================================================================
# Base Repo
# =============================================================================
class BaseRepository:
    """
    Base repository with safe type conversion.

    IMPORTANT:
      - convert_db_row() must not throw on mismatch (prevents worker crashes)
      - prepare_jsonb_param() returns a JSON string, safe for $::jsonb usage
    """

    # Fields that should be treated as JSON dicts
    JSON_DICT_FIELDS = {
        "display_name",
        "description",
        "technical_specs",
        "safe_zones",
        "brand_colors",
        "technical_constraints",
        "api_requirements",
        "attributes_json",
        "meta_json",
        "payload_json",
        "traditional_attire",
        "cultural_markers",
        "attire_style",
        "creative_variations",
        "creative_variations_json",
        "generation_params",
        "resolved",
        # note: safe_zone_insets is often json-ish
        "safe_zone_insets",
    }

    # Fields that should be treated as JSON lists
    JSON_LIST_FIELDS = {
        "content_guidelines",          # platform_requirements sometimes behaves list-ish
        "mood_descriptors",            # your CSV string showed up here
        "background_prompts",
        "prompt_modifiers",
        "recommended_prompt_suffix",   # some schemas store as list-ish
    }

    # UUID-like fields
    UUID_FIELDS = {
        "id",
        "user_id",
        "job_id",
        "face_profile_id",
        "primary_image_asset_id",
        "output_asset_id",
        "asset_id",
        "profile_id",
    }

    # TEXT[] fields
    TEXT_ARRAY_FIELDS = {
        "recommended_platforms",
        "recommended_formats",
        "industry_focus",
        "use_case_compatibility",
        "professional_contexts",
        "typical_skin_tones",
        "background_prompts",  # sometimes stored text[] in older schemas
        "regions",
        "target_audience",
    }

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.converter = DatabaseTypeConverter()

    def convert_db_row(self, row: Optional[asyncpg.Record]) -> Dict[str, Any]:
        if not row:
            return {}

        data = dict(row)
        converted: Dict[str, Any] = {}

        for field_name, field_value in data.items():
            try:
                # UUID fields
                if field_name in self.UUID_FIELDS:
                    converted[field_name] = self.converter.convert_uuid_to_string(field_value)
                    continue

                # JSON list fields (heal CSV strings -> list)
                if field_name in self.JSON_LIST_FIELDS:
                    converted[field_name] = self.converter.convert_jsonish(field_value, default=[])
                    continue

                # JSON dict fields
                if field_name in self.JSON_DICT_FIELDS:
                    converted[field_name] = self.converter.convert_jsonish(field_value, default={})
                    continue

                # TEXT[] fields
                if field_name in self.TEXT_ARRAY_FIELDS:
                    converted[field_name] = self.converter.convert_text_array_to_list(field_value)
                    continue

                # default: keep as-is
                converted[field_name] = field_value

            except Exception as e:
                # Never crash row conversion — keep raw
                logger.warning(
                    "convert_db_row field conversion failed",
                    extra={"field": field_name, "error": str(e), "type": str(type(field_value))},
                )
                converted[field_name] = field_value

        return converted

    def convert_db_rows(self, rows: List[asyncpg.Record]) -> List[Dict[str, Any]]:
        return [self.convert_db_row(row) for row in rows]

    async def execute_query(self, query: str, *params) -> Optional[asyncpg.Record]:
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchrow(query, *params)
        except Exception as e:
            logger.error("Query failed", extra={"query": query, "params": params, "error": str(e)})
            raise

    async def execute_queries(self, query: str, *params) -> List[asyncpg.Record]:
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetch(query, *params)
        except Exception as e:
            logger.error("Multi-query failed", extra={"query": query, "params": params, "error": str(e)})
            raise

    async def execute_command(self, command: str, *params) -> str:
        try:
            async with self.pool.acquire() as conn:
                return await conn.execute(command, *params)
        except Exception as e:
            logger.error("Command failed", extra={"command": command, "params": params, "error": str(e)})
            raise

    async def fetch_scalar(self, query: str, *params) -> Any:
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchval(query, *params)
        except Exception as e:
            logger.error("Scalar query failed", extra={"query": query, "params": params, "error": str(e)})
            raise

    def prepare_jsonb_param(self, value: Any) -> str:
        """
        Prepare JSONB parameter for database insertion.

        Returns a JSON string that is always valid JSON,
        safe for queries that do `$1::jsonb`.
        """
        # Preserve list vs dict when possible
        if isinstance(value, list):
            safe = coerce_json_value(value, default=[])
            return json.dumps(safe, default=str)

        if isinstance(value, dict):
            safe = coerce_json_value(value, default={})
            return json.dumps(safe, default=str)

        # Try to parse string as JSON; otherwise coerce based on dict default
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return json.dumps({}, default=str)
            try:
                parsed = json.loads(s)
                return json.dumps(parsed, default=str)
            except Exception:
                # If it looks CSV-ish or {a,b,c}, store as list; else store as {"raw": "..."}
                coerced_list = coerce_json_value(s, default=[])
                if isinstance(coerced_list, list) and coerced_list and coerced_list != [s]:
                    return json.dumps(coerced_list, default=str)
                coerced = coerce_json_value(s, default={})
                return json.dumps(coerced, default=str)

        if value is None:
            return json.dumps({}, default=str)

        # primitives / unknown objects
        return json.dumps(value, default=str)

    def prepare_uuid_param(self, value: Union[str, None]) -> Optional[str]:
        if value is None:
            return None
        return str(value)