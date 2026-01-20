from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .base_repo import BaseRepository
from ..domain.creator_platform_models import (
    ImageFormatDB,
    UseCaseDB,
    AgeRangeDB,
    RegionDB,
    SkinToneDB,
)

from app.repos.base_repo import coerce_json_value

logger = logging.getLogger(__name__)


class CreatorPlatformConfigRepo(BaseRepository):
    """Repository for creator platform configuration tables (DB-aligned & mismatch-safe)."""

    # -------------------------
    # Introspection helpers
    # -------------------------
    async def _table_exists(self, table_name: str, schema: str = "public") -> bool:
        q = """
        SELECT EXISTS(
          SELECT 1
          FROM information_schema.tables
          WHERE table_schema = $1 AND table_name = $2
        )
        """
        try:
            return bool(await self.fetch_scalar(q, schema, table_name))
        except Exception:
            return False

    async def _column_exists(self, table_name: str, column_name: str, schema: str = "public") -> bool:
        q = """
        SELECT EXISTS(
          SELECT 1
          FROM information_schema.columns
          WHERE table_schema = $1 AND table_name = $2 AND column_name = $3
        )
        """
        try:
            return bool(await self.fetch_scalar(q, schema, table_name, column_name))
        except Exception:
            return False

    async def _select_existing_cols(
        self,
        table: str,
        wanted: Sequence[str],
        schema: str = "public",
    ) -> List[str]:
        """Return only columns that exist in the current DB."""
        cols: List[str] = []
        for c in wanted:
            if await self._column_exists(table, c, schema=schema):
                cols.append(c)
        return cols

    # -------------------------
    # Helpers: safe select by platform_code
    # -------------------------
    async def _safe_select_by_platform_code(
        self,
        table: str,
        platform_code: Optional[str],
        columns: Sequence[str],
        active_only: bool = True,
        schema: str = "public",
    ) -> Optional[Dict[str, Any]]:
        """
        Same as _safe_select_by_code but uses platform_code column (platform_requirements table).
        Returns None if table missing or code missing.
        """
        if not platform_code:
            return None
        if not await self._table_exists(table, schema=schema):
            return None

        select_cols = await self._select_existing_cols(table, columns, schema=schema)

        # Ensure platform_code is selected when available
        if "platform_code" not in select_cols and await self._column_exists(table, "platform_code", schema=schema):
            select_cols.insert(0, "platform_code")

        if not select_cols:
            return None

        where = ["platform_code = $1"]
        if active_only and await self._column_exists(table, "is_active", schema=schema):
            where.append("is_active = true")

        q = f"SELECT {', '.join(select_cols)} FROM {table} WHERE " + " AND ".join(where) + " LIMIT 1"
        try:
            row = await self.execute_query(q, platform_code)
            d = self.convert_db_row(row) if row else None
            if d and "code" not in d:
                # optional compatibility: many callers expect 'code'
                d["code"] = d.get("platform_code")
            return d
        except Exception as e:
            logger.warning("Optional select failed", extra={"table": table, "error": str(e)})
            return None

    # -------------------------
    # Helpers: safe select by code
    # -------------------------
    async def _safe_select_by_code(
        self,
        table: str,
        code: Optional[str],
        columns: Sequence[str],
        active_only: bool = True,
        schema: str = "public",
    ) -> Optional[Dict[str, Any]]:
        """
        Safe “get by code” for optional tables:
        - returns None if table missing or code missing
        - selects only existing columns
        - filters on is_active if column exists and active_only True
        """
        if not code:
            return None
        if not await self._table_exists(table, schema=schema):
            return None

        select_cols = await self._select_existing_cols(table, columns, schema=schema)

        # Ensure code is selected when available
        if "code" not in select_cols and await self._column_exists(table, "code", schema=schema):
            select_cols.insert(0, "code")

        if not select_cols:
            return None

        where = ["code = $1"]
        if active_only and await self._column_exists(table, "is_active", schema=schema):
            where.append("is_active = true")

        q = f"SELECT {', '.join(select_cols)} FROM {table} WHERE " + " AND ".join(where) + " LIMIT 1"
        try:
            row = await self.execute_query(q, code)
            return self.convert_db_row(row) if row else None
        except Exception as e:
            logger.warning("Optional select failed", extra={"table": table, "error": str(e)})
            return None

    # -------------------------
    # Helpers: instantiate models safely
    # -------------------------
    @staticmethod
    def _safe_model(model_cls, data: Dict[str, Any]):
        """
        Try to create your DB model. If schema/model mismatch exists,
        return raw dict instead of crashing (prevents worker deaths).
        """
        try:
            return model_cls(**data)
        except Exception as e:
            logger.warning(
                "Model hydrate failed; returning dict",
                extra={"model": getattr(model_cls, "__name__", str(model_cls)), "error": str(e)},
            )
            return data

    @staticmethod
    def _coerce_row_json_fields(row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Heal json/jsonb-ish columns that might contain invalid JSON strings.
        This prevents 'Invalid JSONB value: ...' warnings from cascading into failures.
        """
        if not row:
            return row

        # Common jsonb columns in your config tables (only coerce if present)
        # Dict-shaped fields
        for k in ("display_name", "description", "technical_specs", "safe_zones", "meta_json"):
            if k in row:
                row[k] = coerce_json_value(row.get(k), default={})

        # List-shaped fields
        for k in ("recommended_platforms", "recommended_formats", "target_audience", "industry_focus",
                  "mood_descriptors", "background_prompts", "prompt_modifiers", "regions",
                  "use_case_compatibility", "content_guidelines"):
            if k in row:
                row[k] = coerce_json_value(row.get(k), default=[])

        # Some tables store prompt_base as text; keep as-is
        return row

    # ============================================================================
    # IMAGE FORMATS (face_generation_image_formats)
    # ============================================================================
    async def get_image_formats(self, platform_category: Optional[str] = None) -> List[Any]:
        table = "face_generation_image_formats"
        wanted = (
            "id", "code", "display_name", "width", "height", "aspect_ratio",
            "platform_category", "recommended_platforms", "technical_specs",
            "safe_zones", "is_active", "sort_order", "created_at",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return []

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE is_active = true"
        params: List[Any] = []
        if platform_category and await self._column_exists(table, "platform_category"):
            q += " AND platform_category = $1"
            params.append(platform_category)

        # stable ordering when columns exist
        order_bits: List[str] = []
        if "sort_order" in cols:
            order_bits.append("sort_order")
        if "display_name" in cols:
            order_bits.append("(display_name->>'en')")
        if order_bits:
            q += " ORDER BY " + ", ".join(order_bits)

        rows = await self.execute_queries(q, *params)
        out: List[Any] = []
        for r in rows:
            d = self.convert_db_row(r)
            d = self._coerce_row_json_fields(d)
            out.append(self._safe_model(ImageFormatDB, d))
        return out

    async def get_image_format_by_code(self, code: str) -> Optional[Any]:
        table = "face_generation_image_formats"
        wanted = (
            "id", "code", "display_name", "width", "height", "aspect_ratio",
            "platform_category", "recommended_platforms", "technical_specs",
            "safe_zones", "is_active", "sort_order", "created_at",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return None

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE code = $1 AND is_active = true"
        row = await self.execute_query(q, code)
        if not row:
            return None
        d = self._coerce_row_json_fields(self.convert_db_row(row))
        return self._safe_model(ImageFormatDB, d)

    # ============================================================================
    # USE CASES (face_generation_use_cases)
    # ============================================================================
    async def get_use_cases(self, category: Optional[str] = None) -> List[Any]:
        table = "face_generation_use_cases"
        wanted = (
            "id", "code", "display_name", "category", "description", "prompt_base",
            "lighting_style", "composition_style", "mood_descriptors",
            "background_type", "recommended_formats", "target_audience", "industry_focus",
            "is_active", "sort_order", "created_at",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return []

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE is_active = true"
        params: List[Any] = []
        if category and "category" in cols:
            q += " AND category = $1"
            params.append(category)

        order_bits: List[str] = []
        if "sort_order" in cols:
            order_bits.append("sort_order")
        if "display_name" in cols:
            order_bits.append("(display_name->>'en')")
        if order_bits:
            q += " ORDER BY " + ", ".join(order_bits)

        rows = await self.execute_queries(q, *params)
        out: List[Any] = []
        for r in rows:
            d = self._coerce_row_json_fields(self.convert_db_row(r))
            out.append(self._safe_model(UseCaseDB, d))
        return out

    async def get_use_case_by_code(self, code: str) -> Optional[Any]:
        table = "face_generation_use_cases"
        wanted = (
            "id", "code", "display_name", "category", "description", "prompt_base",
            "lighting_style", "composition_style", "mood_descriptors",
            "background_type", "recommended_formats", "target_audience", "industry_focus",
            "is_active", "sort_order", "created_at",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return None

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE code = $1 AND is_active = true"
        row = await self.execute_query(q, code)
        if not row:
            return None
        d = self._coerce_row_json_fields(self.convert_db_row(row))
        return self._safe_model(UseCaseDB, d)

    # ============================================================================
    # AGE RANGES (face_generation_age_ranges)
    # ============================================================================
    async def get_age_ranges(self) -> List[Any]:
        table = "face_generation_age_ranges"
        wanted = (
            "id", "code", "display_name", "min_age", "max_age", "prompt_descriptor",
            "professional_contexts", "is_active",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return []

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE is_active = true"
        if "display_name" in cols:
            q += " ORDER BY (display_name->>'en')"

        rows = await self.execute_queries(q)
        out: List[Any] = []
        for r in rows:
            d = self._coerce_row_json_fields(self.convert_db_row(r))
            out.append(self._safe_model(AgeRangeDB, d))
        return out

    async def get_age_range_by_code(self, code: str) -> Optional[Any]:
        table = "face_generation_age_ranges"
        wanted = (
            "id", "code", "display_name", "min_age", "max_age", "prompt_descriptor",
            "professional_contexts", "is_active",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return None

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE code = $1 AND is_active = true"
        row = await self.execute_query(q, code)
        if not row:
            return None
        d = self._coerce_row_json_fields(self.convert_db_row(row))
        return self._safe_model(AgeRangeDB, d)

    # ============================================================================
    # REGIONS (face_generation_regions)
    # ============================================================================
    async def get_regions(self) -> List[Any]:
        table = "face_generation_regions"
        wanted = (
            "id", "code", "display_name", "prompt_base",
            "cultural_markers", "is_active", "sort_order", "created_at",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return []

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE is_active = true"
        order_bits: List[str] = []
        if "sort_order" in cols:
            order_bits.append("sort_order")
        if "display_name" in cols:
            order_bits.append("(display_name->>'en')")
        if order_bits:
            q += " ORDER BY " + ", ".join(order_bits)

        rows = await self.execute_queries(q)
        out: List[Any] = []
        for r in rows:
            d = self._coerce_row_json_fields(self.convert_db_row(r))
            out.append(self._safe_model(RegionDB, d))
        return out

    async def get_region_by_code(self, code: Optional[str]) -> Optional[Any]:
        if not code:
            return None
        table = "face_generation_regions"
        wanted = (
            "id", "code", "display_name", "prompt_base",
            "cultural_markers", "is_active", "sort_order", "created_at",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return None

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE code = $1 AND is_active = true"
        row = await self.execute_query(q, code)
        if not row:
            return None
        d = self._coerce_row_json_fields(self.convert_db_row(row))
        return self._safe_model(RegionDB, d)

    # ============================================================================
    # SKIN TONES (face_generation_skin_tones)
    # ============================================================================
    async def get_skin_tones(self) -> List[Any]:
        table = "face_generation_skin_tones"
        wanted = (
            "id", "code", "display_name", "hex_reference",
            "prompt_descriptor", "diversity_weight", "is_active",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return []

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE is_active = true"
        if "display_name" in cols:
            q += " ORDER BY (display_name->>'en')"

        rows = await self.execute_queries(q)
        out: List[Any] = []
        for r in rows:
            d = self._coerce_row_json_fields(self.convert_db_row(r))
            out.append(self._safe_model(SkinToneDB, d))
        return out

    async def get_skin_tone_by_code(self, code: Optional[str]) -> Optional[Any]:
        if not code:
            return None
        table = "face_generation_skin_tones"
        wanted = (
            "id", "code", "display_name", "hex_reference",
            "prompt_descriptor", "diversity_weight", "is_active",
        )
        cols = await self._select_existing_cols(table, wanted)
        if not cols:
            return None

        q = f"SELECT {', '.join(cols)} FROM {table} WHERE code = $1 AND is_active = true"
        row = await self.execute_query(q, code)
        if not row:
            return None
        d = self._coerce_row_json_fields(self.convert_db_row(row))
        return self._safe_model(SkinToneDB, d)

    # ============================================================================
    # STYLES (not deployed) → return []
    # ============================================================================
    async def get_style_by_code(self, code: Optional[str]) -> Optional[Dict[str, Any]]:
        return None

    async def get_styles(self) -> List[Dict[str, Any]]:
        return []

    # ============================================================================
    # OPTIONAL: CONTEXT / CLOTHING / PLATFORM REQUIREMENTS (safe)
    # ============================================================================
    async def get_context_by_code(self, code: Optional[str]) -> Optional[Dict[str, Any]]:
        row = await self._safe_select_by_code(
            table="face_generation_contexts",
            code=code,
            columns=(
                "code",
                "display_name",
                "economic_class",
                "setting_type",
                "attire_style",
                "background_prompts",
                "prompt_modifiers",
                "glamour_level",
                "meta_json",
                "is_active",
            ),
        )
        if not row:
            return None
        row = self._coerce_row_json_fields(row)
        if "prompt_base" not in row:
            row["prompt_base"] = row.get("prompt_modifiers")
        return row

    async def get_clothing_by_code(self, code: Optional[str]) -> Optional[Dict[str, Any]]:
        row = await self._safe_select_by_code(
            table="face_generation_clothing",
            code=code,
            columns=(
                "code",
                "display_name",
                "category",
                "gender_fit",
                "regions",
                "prompt_descriptor",
                "formality_level",
                "meta_json",
                "is_active",
            ),
        )
        if not row:
            return None
        row = self._coerce_row_json_fields(row)
        if "prompt_base" not in row:
            row["prompt_base"] = row.get("prompt_descriptor")
        return row

    async def get_platform_requirements_by_code(self, code: Optional[str]) -> Optional[Dict[str, Any]]:
        # DB table is platform_requirements and key column is platform_code
        row = await self._safe_select_by_platform_code(
            table="platform_requirements",
            platform_code=code,
            columns=(
                "platform_code",
                "display_name",
                "content_guidelines",
                "recommended_prompt_suffix",
                "safe_zone_insets",
                "width",
                "height",
                "aspect_ratio",
                "max_file_size_mb",
                "meta_json",
                "is_active",
                "sort_order",
            ),
        )
        if not row:
            return None
        row = self._coerce_row_json_fields(row)
        # Ensure compatibility field exists
        row["code"] = row.get("code") or row.get("platform_code")
        return row

    # ============================================================================
    # VARIATIONS (face_generation_variations)
    # ============================================================================
    async def get_variations_by_use_case(
        self,
        use_case_code: str,
        professional_level_min: int = 0,
        creativity_level_min: int = 0,
        active_only: bool = True,
    ) -> Dict[str, List[Dict[str, Any]]]:
        table = "face_generation_variations"
        if not await self._table_exists(table):
            return {}

        wanted = (
            "id",
            "variation_type",
            "code",
            "display_name",
            "prompt_modifier",
            "use_case_compatibility",
            "mood_impact",
            "professional_level",
            "creativity_level",
            "is_active",
        )
        cols = await self._select_existing_cols(table, wanted)
        if "variation_type" not in cols or "code" not in cols:
            return {}

        has_compat = "use_case_compatibility" in cols
        has_active = "is_active" in cols
        has_prof = "professional_level" in cols
        has_crea = "creativity_level" in cols

        async def run_query(filter_compat: bool) -> List[Dict[str, Any]]:
            where: List[str] = []
            params: List[Any] = []
            p = 1

            if active_only and has_active:
                where.append("is_active = true")

            if filter_compat and has_compat and use_case_code:
                where.append(f"(use_case_compatibility IS NULL OR ${p} = ANY(use_case_compatibility))")
                params.append(use_case_code)
                p += 1

            if has_prof:
                where.append(f"professional_level >= ${p}")
                params.append(int(professional_level_min))
                p += 1

            if has_crea:
                where.append(f"creativity_level >= ${p}")
                params.append(int(creativity_level_min))
                p += 1

            q = f"SELECT {', '.join(cols)} FROM {table}"
            if where:
                q += " WHERE " + " AND ".join(where)

            order_bits: List[str] = ["variation_type"]
            if has_prof:
                order_bits.append("professional_level DESC")
            if has_crea:
                order_bits.append("creativity_level DESC")
            order_bits.append("code")
            q += " ORDER BY " + ", ".join(order_bits)

            rows = await self.execute_queries(q, *params)
            out_rows: List[Dict[str, Any]] = []
            for r in rows:
                d = self.convert_db_row(r)
                d = self._coerce_row_json_fields(d)
                out_rows.append(d)
            return out_rows

        try:
            rows = await run_query(filter_compat=True)
            if not rows and has_compat:
                rows = await run_query(filter_compat=False)
        except Exception as e:
            logger.warning("get_variations_by_use_case failed", extra={"error": str(e)})
            return {}

        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for d in rows:
            vt = d.get("variation_type") or "other"
            by_type.setdefault(vt, []).append(d)
        return by_type

    # ============================================================================
    # VALIDATION (optional codes ignored if optional tables missing)
    # ============================================================================
    async def validate_creator_request_config(
        self,
        image_format_code: str,
        use_case_code: str,
        age_range_code: str,
        region_code: Optional[str] = None,
        skin_tone_code: Optional[str] = None,
        style_code: Optional[str] = None,
        context_code: Optional[str] = None,
        clothing_style_code: Optional[str] = None,
        platform_code: Optional[str] = None,
    ) -> Dict[str, bool]:
        checks: Dict[str, bool] = {}

        checks["image_format_valid"] = bool(
            await self.fetch_scalar(
                "SELECT EXISTS(SELECT 1 FROM face_generation_image_formats WHERE code = $1 AND is_active = true)",
                image_format_code,
            )
        )
        checks["use_case_valid"] = bool(
            await self.fetch_scalar(
                "SELECT EXISTS(SELECT 1 FROM face_generation_use_cases WHERE code = $1 AND is_active = true)",
                use_case_code,
            )
        )
        checks["age_range_valid"] = bool(
            await self.fetch_scalar(
                "SELECT EXISTS(SELECT 1 FROM face_generation_age_ranges WHERE code = $1 AND is_active = true)",
                age_range_code,
            )
        )

        if region_code:
            checks["region_valid"] = bool(
                await self.fetch_scalar(
                    "SELECT EXISTS(SELECT 1 FROM face_generation_regions WHERE code = $1 AND is_active = true)",
                    region_code,
                )
            )

        if skin_tone_code:
            checks["skin_tone_valid"] = bool(
                await self.fetch_scalar(
                    "SELECT EXISTS(SELECT 1 FROM face_generation_skin_tones WHERE code = $1 AND is_active = true)",
                    skin_tone_code,
                )
            )

        # styles table not deployed in your DB → ignore style_code
        if style_code:
            checks["style_valid"] = True

        if context_code:
            if await self._table_exists("face_generation_contexts"):
                checks["context_valid"] = bool(await self.get_context_by_code(context_code))
            else:
                checks["context_valid"] = True

        if clothing_style_code:
            if await self._table_exists("face_generation_clothing"):
                checks["clothing_style_valid"] = bool(await self.get_clothing_by_code(clothing_style_code))
            else:
                checks["clothing_style_valid"] = True

        if platform_code:
            if await self._table_exists("platform_requirements"):
                checks["platform_valid"] = bool(await self.get_platform_requirements_by_code(platform_code))
            else:
                checks["platform_valid"] = True

        # If no mapping table exists, assume compatible
        checks["format_use_case_compatible"] = True
        return checks

    async def get_complete_config(self) -> Dict[str, Any]:
        """Get all configuration for UI (includes optional sets if available)."""
        payload: Dict[str, Any] = {
            "image_formats": await self.get_image_formats(),
            "use_cases": await self.get_use_cases(),
            "age_ranges": await self.get_age_ranges(),
            "regions": await self.get_regions(),
            "skin_tones": await self.get_skin_tones(),
            "styles": await self.get_styles(),
        }

        if await self._table_exists("face_generation_contexts"):
            try:
                rows = await self.execute_queries(
                    """
                    SELECT code, display_name, prompt_modifiers, glamour_level, setting_type, meta_json
                    FROM face_generation_contexts
                    WHERE is_active = true
                    ORDER BY (display_name->>'en')
                    """
                )
                payload["contexts"] = [self._coerce_row_json_fields(self.convert_db_row(r)) for r in rows]
            except Exception:
                payload["contexts"] = []

        if await self._table_exists("face_generation_clothing"):
            try:
                rows = await self.execute_queries(
                    """
                    SELECT code, display_name, prompt_descriptor, category, gender_fit, formality_level, regions, meta_json
                    FROM face_generation_clothing
                    WHERE is_active = true
                    ORDER BY (display_name->>'en')
                    """
                )
                payload["clothing_styles"] = [self._coerce_row_json_fields(self.convert_db_row(r)) for r in rows]
            except Exception:
                payload["clothing_styles"] = []

        if await self._table_exists("platform_requirements"):
            try:
                rows = await self.execute_queries(
                    """
                    SELECT platform_code, display_name, content_guidelines, recommended_prompt_suffix,
                        safe_zone_insets, width, height, aspect_ratio, max_file_size_mb, meta_json
                    FROM platform_requirements
                    WHERE is_active = true
                    ORDER BY sort_order NULLS LAST, (display_name->>'en'), platform_code
                    """
                )
                payload["platform_requirements"] = [
                    {**self._coerce_row_json_fields(self.convert_db_row(r)),
                     "code": self.convert_db_row(r).get("platform_code")}
                    for r in rows
                ]
            except Exception:
                payload["platform_requirements"] = []

        return payload