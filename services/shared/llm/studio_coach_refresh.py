from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

try:
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore

from pydantic import BaseModel, Field, ValidationError


Tone = Literal["neutral", "success", "warning", "premium"]
Studio = Literal["face", "audio", "fusion"]

ALLOWED_TONES = {"neutral", "success", "warning", "premium"}
ALLOWED_STUDIOS = {"face", "audio", "fusion"}
DEFAULT_AUDIO_MODES = ["tts"]
DEFAULT_LOCALES = ["en"]


class GeneratedCoachTip(BaseModel):
    studio: Studio
    mode: Optional[str] = "tts"
    locale: str = "en"
    title: str = Field(..., min_length=4, max_length=80)
    body: str = Field(..., min_length=20, max_length=260)
    tone: Tone = "neutral"
    priority: int = Field(default=10, ge=0, le=100)
    targeting_json: Dict[str, Any] = Field(default_factory=dict)
    tags_json: Dict[str, Any] = Field(default_factory=dict)


class RefreshResult(BaseModel):
    run_id: Optional[str] = None
    studio: str
    mode: str
    locale: str
    status: str
    created_count: int = 0
    updated_count: int = 0
    rejected_count: int = 0
    active_count: int = 0
    message: Optional[str] = None


@dataclass(frozen=True)
class RefreshConfig:
    enabled: bool
    auto_activate: bool
    max_tips_per_context: int
    llm_model: str
    provider: str
    service_name: str = "studio_coach_refresh_worker"

    @staticmethod
    def from_env() -> "RefreshConfig":
        return RefreshConfig(
            enabled=_env_bool("DF_STUDIO_COACH_REFRESH_ENABLED", False),
            auto_activate=_env_bool("DF_STUDIO_COACH_AUTO_ACTIVATE", False),
            max_tips_per_context=max(1, min(_env_int("DF_STUDIO_COACH_REFRESH_MAX_TIPS", 8), 20)),
            llm_model=os.getenv("DF_STUDIO_COACH_LLM_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini")),
            provider=os.getenv("DF_STUDIO_COACH_LLM_PROVIDER", "openai").strip().lower() or "openai",
        )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _json_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _stable_tip_key(studio: str, mode: str, locale: str, title: str, body: str) -> str:
    blob = json.dumps(
        {
            "studio": studio,
            "mode": mode or "",
            "locale": locale or "en",
            "title": title.strip().lower(),
            "body": body.strip().lower(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha1(blob).hexdigest()[:24]


def _extract_json_object(raw: str) -> Dict[str, Any]:
    text = _clean_text(raw)
    if not text:
        raise ValueError("empty_llm_response")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError("llm_response_missing_json_object")
    obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("llm_response_json_not_object")
    return obj


def _normalize_tip_payload(raw: Dict[str, Any], *, studio: str, mode: str, locale: str) -> GeneratedCoachTip:
    payload = dict(raw)
    payload["studio"] = _clean_text(payload.get("studio")) or studio
    payload["mode"] = _clean_text(payload.get("mode")) or mode
    payload["locale"] = _clean_text(payload.get("locale")) or locale or "en"
    payload["tone"] = _clean_text(payload.get("tone")) or "neutral"
    if payload["tone"] not in ALLOWED_TONES:
        payload["tone"] = "neutral"
    payload["targeting_json"] = _json_dict(payload.get("targeting_json") or payload.get("targeting"))
    payload["tags_json"] = _json_dict(payload.get("tags_json") or payload.get("tags"))

    # DB-column values are duplicated into targeting_json so the serving ranker
    # can use one consistent matcher.
    payload["targeting_json"].setdefault("studio", payload["studio"])
    payload["targeting_json"].setdefault("mode", payload["mode"])
    payload["targeting_json"].setdefault("locale", payload["locale"])
    payload["tags_json"].setdefault("generated_by", "studio_coach_refresh_worker")
    payload["tags_json"].setdefault("generated_at", _utc_now_iso())
    return GeneratedCoachTip(**payload)


def validate_generated_tips(raw_tips: Iterable[Dict[str, Any]], *, studio: str, mode: str, locale: str, max_count: int) -> Tuple[List[GeneratedCoachTip], List[Dict[str, Any]]]:
    accepted: List[GeneratedCoachTip] = []
    rejected: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for raw in raw_tips:
        try:
            tip = _normalize_tip_payload(raw, studio=studio, mode=mode, locale=locale)
            if tip.studio not in ALLOWED_STUDIOS:
                raise ValueError("invalid_studio")
            if tip.mode != mode:
                # Avoid a cross-mode write from a loose model response.
                tip = tip.model_copy(update={"mode": mode})
            key = (tip.title.strip().lower(), tip.body.strip().lower())
            if key in seen:
                raise ValueError("duplicate_in_llm_output")
            seen.add(key)
            accepted.append(tip)
            if len(accepted) >= max_count:
                break
        except (ValidationError, ValueError) as exc:
            rejected.append({"raw": raw, "error": str(exc)})
        except Exception as exc:
            rejected.append({"raw": raw, "error": f"unexpected_validation_error:{exc}"})
    return accepted, rejected


def build_refresh_prompt(*, studio: str, mode: str, locale: str, active_tips: Sequence[Dict[str, Any]], audit_summary: Dict[str, Any], max_tips: int) -> str:
    compact_active = [
        {
            "title": _clean_text(t.get("title"))[:90],
            "body": _clean_text(t.get("body"))[:220],
            "tone": _clean_text(t.get("tone")),
            "priority": t.get("priority"),
            "targeting_json": _json_dict(t.get("targeting_json")),
        }
        for t in active_tips[:30]
    ]
    contract = {
        "tips": [
            {
                "studio": studio,
                "mode": mode,
                "locale": locale,
                "title": "4-80 chars, action oriented",
                "body": "20-260 chars, product-safe practical coaching copy",
                "tone": "neutral|success|warning|premium",
                "priority": "0-100 integer",
                "targeting_json": {"studio": studio, "mode": mode, "locale": locale},
                "tags_json": {"category": "quality|cost_control|localization|workflow|premium"},
            }
        ]
    }
    return (
        "You are generating safe, concise product coaching tips for DesiFaces Studio.\n"
        "Return JSON only. Do not include markdown. Do not make legal, medical, or financial claims. "
        "Do not promise provider-specific results. Avoid duplicate tips.\n"
        f"Studio: {studio}\nMode: {mode}\nLocale: {locale}\n"
        f"Generate up to {max_tips} tips. Prefer practical advice that improves user output quality, reduces wasted credits, or guides locale/style choices.\n"
        f"Existing active tips to avoid duplicating:\n{json.dumps(compact_active, ensure_ascii=False, default=str)}\n"
        f"Recent served/audit summary:\n{json.dumps(audit_summary, ensure_ascii=False, default=str)}\n"
        f"Required JSON schema example:\n{json.dumps(contract, ensure_ascii=False)}"
    )


def _extract_openai_response_text(payload: Dict[str, Any]) -> str:
    """Extract text from OpenAI Responses API payload without openai SDK dependency."""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    chunks: List[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text)
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text)

    if chunks:
        return "\n".join(chunks)
    return ""


def _post_openai_responses_sync(*, prompt: str, config: RefreshConfig) -> Dict[str, Any]:
    """Call OpenAI using stdlib urllib so the worker does not require the openai package."""
    import urllib.error
    import urllib.request

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY_MISSING")

    body = {
        "model": config.llm_model,
        "input": [
            {"role": "system", "content": "Return strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=_env_int("DF_STUDIO_COACH_OPENAI_TIMEOUT_SECONDS", 60)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"OPENAI_HTTP_{exc.code}:{err_body}") from exc
    except Exception as exc:
        raise RuntimeError(f"OPENAI_REQUEST_FAILED:{exc}") from exc

    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"OPENAI_RESPONSE_NOT_JSON:{raw[:800]}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("OPENAI_RESPONSE_NOT_OBJECT")

    text = _extract_openai_response_text(payload)
    if not text.strip():
        raise RuntimeError(f"OPENAI_RESPONSE_EMPTY_TEXT:{json.dumps(payload, default=str)[:1200]}")
    return _extract_json_object(text)


async def call_llm_for_tips(prompt: str, *, config: RefreshConfig) -> Dict[str, Any]:
    if not config.enabled:
        raise RuntimeError("DF_STUDIO_COACH_REFRESH_DISABLED")
    if config.provider != "openai":
        raise RuntimeError(f"UNSUPPORTED_STUDIO_COACH_LLM_PROVIDER:{config.provider}")

    return await asyncio.to_thread(_post_openai_responses_sync, prompt=prompt, config=config)


async def table_columns(conn: Any, table_name: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = $1
        """,
        table_name,
    )
    return {str(r["column_name"]) for r in rows}


async def fetch_active_tips(conn: Any, *, studio: str, mode: str, locale: str) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id::text, studio, mode, locale, title, body, tone, priority, source,
               targeting_json, tags_json, is_active, expires_at, created_at, updated_at
        FROM public.studio_coach_tips
        WHERE studio = $1
          AND (mode IS NULL OR mode = '' OR mode = $2)
          AND (locale IS NULL OR locale = '' OR locale = $3 OR locale = 'en')
          AND is_active = TRUE
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY priority DESC, updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        LIMIT 100
        """,
        studio,
        mode,
        locale,
    )
    return [dict(r) for r in rows]


async def fetch_audit_summary(conn: Any, *, studio: str, mode: str, locale: str) -> Dict[str, Any]:
    cols = await table_columns(conn, "studio_coach_tip_audit")
    if not cols:
        return {"audit_available": False}

    where = ["studio = $1"]
    args: List[Any] = [studio]
    if "mode" in cols:
        args.append(mode)
        where.append(f"(mode IS NULL OR mode = '' OR mode = ${len(args)})")
    if "locale" in cols:
        args.append(locale)
        where.append(f"(locale IS NULL OR locale = '' OR locale = ${len(args)} OR locale = 'en')")

    ts_col = "served_at" if "served_at" in cols else "created_at" if "created_at" in cols else None
    if ts_col:
        where.append(f"{ts_col} > NOW() - INTERVAL '14 days'")

    source_select = "source" if "source" in cols else "NULL::text AS source"
    fallback_select = "fallback_used" if "fallback_used" in cols else "NULL::boolean AS fallback_used"
    sql = f"""
        SELECT {source_select}, {fallback_select}, COUNT(*)::int AS n
        FROM public.studio_coach_tip_audit
        WHERE {' AND '.join(where)}
        GROUP BY 1, 2
        ORDER BY n DESC
        LIMIT 20
    """
    rows = await conn.fetch(sql, *args)
    return {
        "audit_available": True,
        "window_days": 14,
        "counts": [dict(r) for r in rows],
    }


async def create_refresh_run(conn: Any, *, studio: str, mode: str, locale: str, input_json: Dict[str, Any], config: RefreshConfig) -> Optional[str]:
    cols = await table_columns(conn, "studio_coach_refresh_runs")
    if not cols:
        return None

    payload: Dict[str, Any] = {
        "studio": studio,
        "mode": mode,
        "locale": locale,
        "status": "running",
        "source": "llm_refresh",
        "provider": config.provider,
        "model": config.llm_model,
        "input_json": input_json,
        "created_at": None,
        "started_at": None,
    }

    insert_cols: List[str] = []
    placeholders: List[str] = []
    values: List[Any] = []
    for col in ("studio", "mode", "locale", "status", "source", "provider", "model"):
        if col in cols:
            insert_cols.append(col)
            values.append(payload[col])
            placeholders.append(f"${len(values)}")
    for col in ("input_json", "request_json"):
        if col in cols:
            insert_cols.append(col)
            values.append(json.dumps(input_json, default=str))
            placeholders.append(f"${len(values)}::jsonb")
            break
    for col in ("created_at", "started_at"):
        if col in cols:
            insert_cols.append(col)
            placeholders.append("NOW()")

    returning = " RETURNING id::text" if "id" in cols else ""
    row = await conn.fetchrow(
        f"INSERT INTO public.studio_coach_refresh_runs ({', '.join(insert_cols)}) VALUES ({', '.join(placeholders)}){returning}",
        *values,
    )
    return str(row["id"]) if row and "id" in row else None


async def finish_refresh_run(
    conn: Any,
    *,
    run_id: Optional[str],
    studio: str,
    mode: str,
    locale: str,
    status: str,
    output_json: Dict[str, Any],
    created_count: int,
    updated_count: int,
    rejected_count: int,
    message: Optional[str] = None,
) -> None:
    cols = await table_columns(conn, "studio_coach_refresh_runs")
    if not cols:
        return

    assignments: List[str] = []
    values: List[Any] = []
    for col, value in (
        ("status", status),
        ("message", message),
        ("error_message", message if status != "succeeded" else None),
        ("created_count", created_count),
        ("updated_count", updated_count),
        ("rejected_count", rejected_count),
    ):
        if col in cols:
            values.append(value)
            assignments.append(f"{col} = ${len(values)}")
    for col in ("output_json", "response_json"):
        if col in cols:
            values.append(json.dumps(output_json, default=str))
            assignments.append(f"{col} = ${len(values)}::jsonb")
            break
    for col in ("finished_at", "updated_at"):
        if col in cols:
            assignments.append(f"{col} = NOW()")

    if not assignments:
        return
    if run_id and "id" in cols:
        values.append(run_id)
        await conn.execute(f"UPDATE public.studio_coach_refresh_runs SET {', '.join(assignments)} WHERE id = ${len(values)}::uuid", *values)
    else:
        values.extend([studio, mode, locale])
        await conn.execute(
            f"""
            UPDATE public.studio_coach_refresh_runs
            SET {', '.join(assignments)}
            WHERE studio = ${len(values)-2}
              AND mode = ${len(values)-1}
              AND locale = ${len(values)}
              AND status = 'running'
            """,
            *values,
        )


async def upsert_tip(conn: Any, *, tip: GeneratedCoachTip, auto_activate: bool, run_id: Optional[str]) -> str:
    cols = await table_columns(conn, "studio_coach_tips")
    if not cols:
        raise RuntimeError("studio_coach_tips_table_missing")

    stable_id = _stable_tip_key(tip.studio, tip.mode or "", tip.locale, tip.title, tip.body)
    existing = await conn.fetchrow(
        """
        SELECT id::text
        FROM public.studio_coach_tips
        WHERE lower(trim(title)) = lower(trim($1))
          AND lower(trim(body)) = lower(trim($2))
          AND studio = $3
          AND COALESCE(mode, '') = COALESCE($4, '')
          AND COALESCE(locale, 'en') = COALESCE($5, 'en')
        LIMIT 1
        """,
        tip.title,
        tip.body,
        tip.studio,
        tip.mode,
        tip.locale,
    )

    tags = dict(tip.tags_json or {})
    if run_id:
        tags["refresh_run_id"] = run_id
    source_value = "llm_refresh"
    is_active_value = bool(auto_activate)

    if existing:
        update_cols: List[str] = []
        values: List[Any] = []
        for col, value, cast in (
            ("tone", tip.tone, ""),
            ("priority", tip.priority, ""),
            ("source", source_value, ""),
            ("targeting_json", json.dumps(tip.targeting_json, default=str), "::jsonb"),
            ("tags_json", json.dumps(tags, default=str), "::jsonb"),
        ):
            if col in cols:
                values.append(value)
                update_cols.append(f"{col} = ${len(values)}{cast}")
        if "updated_at" in cols:
            update_cols.append("updated_at = NOW()")
        if not update_cols:
            return "updated"
        values.append(existing["id"])
        await conn.execute(f"UPDATE public.studio_coach_tips SET {', '.join(update_cols)} WHERE id = ${len(values)}::uuid", *values)
        return "updated"

    insert_cols: List[str] = []
    placeholders: List[str] = []
    values: List[Any] = []

    # Most schemas use uuid id. Use deterministic uuid only if the column exists;
    # otherwise allow DB default.
    if "id" in cols:
        import uuid
        values.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"desifaces:studio_coach_tip:{stable_id}")))
        insert_cols.append("id")
        placeholders.append(f"${len(values)}::uuid")

    insert_map = [
        ("studio", tip.studio, ""),
        ("mode", tip.mode, ""),
        ("locale", tip.locale, ""),
        ("title", tip.title, ""),
        ("body", tip.body, ""),
        ("tone", tip.tone, ""),
        ("priority", tip.priority, ""),
        ("source", source_value, ""),
        ("targeting_json", json.dumps(tip.targeting_json, default=str), "::jsonb"),
        ("tags_json", json.dumps(tags, default=str), "::jsonb"),
        ("is_active", is_active_value, ""),
    ]
    for col, value, cast in insert_map:
        if col in cols:
            values.append(value)
            insert_cols.append(col)
            placeholders.append(f"${len(values)}{cast}")
    for col in ("created_at", "updated_at"):
        if col in cols:
            insert_cols.append(col)
            placeholders.append("NOW()")
    await conn.execute(f"INSERT INTO public.studio_coach_tips ({', '.join(insert_cols)}) VALUES ({', '.join(placeholders)})", *values)
    return "created"


async def refresh_one_context(pool: Any, *, studio: str, mode: str, locale: str, config: Optional[RefreshConfig] = None) -> RefreshResult:
    config = config or RefreshConfig.from_env()
    run_id: Optional[str] = None
    created = updated = rejected_count = active_count = 0
    input_json: Dict[str, Any] = {}

    async with pool.acquire() as conn:
        try:
            active = await fetch_active_tips(conn, studio=studio, mode=mode, locale=locale)
            audit_summary = await fetch_audit_summary(conn, studio=studio, mode=mode, locale=locale)
            active_count = len(active)
            input_json = {
                "studio": studio,
                "mode": mode,
                "locale": locale,
                "active_count": active_count,
                "audit_summary": audit_summary,
                "auto_activate": config.auto_activate,
                "max_tips": config.max_tips_per_context,
            }
            run_id = await create_refresh_run(conn, studio=studio, mode=mode, locale=locale, input_json=input_json, config=config)

            prompt = build_refresh_prompt(
                studio=studio,
                mode=mode,
                locale=locale,
                active_tips=active,
                audit_summary=audit_summary,
                max_tips=config.max_tips_per_context,
            )
            raw = await call_llm_for_tips(prompt, config=config)
            raw_tips = raw.get("tips") if isinstance(raw, dict) else []
            if not isinstance(raw_tips, list):
                raise RuntimeError("LLM_RESPONSE_TIPS_NOT_LIST")
            accepted, rejected = validate_generated_tips(raw_tips, studio=studio, mode=mode, locale=locale, max_count=config.max_tips_per_context)
            rejected_count = len(rejected)

            for tip in accepted:
                action = await upsert_tip(conn, tip=tip, auto_activate=config.auto_activate, run_id=run_id)
                if action == "created":
                    created += 1
                else:
                    updated += 1

            output_json = {
                "accepted": [t.model_dump(mode="json") for t in accepted],
                "rejected": rejected,
                "raw_llm_keys": list(raw.keys()) if isinstance(raw, dict) else [],
            }
            status = "succeeded"
            await finish_refresh_run(
                conn,
                run_id=run_id,
                studio=studio,
                mode=mode,
                locale=locale,
                status=status,
                output_json=output_json,
                created_count=created,
                updated_count=updated,
                rejected_count=rejected_count,
                message=None if accepted else "No accepted tips generated.",
            )
            return RefreshResult(
                run_id=run_id,
                studio=studio,
                mode=mode,
                locale=locale,
                status=status,
                created_count=created,
                updated_count=updated,
                rejected_count=rejected_count,
                active_count=active_count,
            )
        except Exception as exc:
            message = str(exc)
            await finish_refresh_run(
                conn,
                run_id=run_id,
                studio=studio,
                mode=mode,
                locale=locale,
                status="failed",
                output_json={"error": message, "input_json": input_json},
                created_count=created,
                updated_count=updated,
                rejected_count=rejected_count,
                message=message,
            )
            return RefreshResult(
                run_id=run_id,
                studio=studio,
                mode=mode,
                locale=locale,
                status="failed",
                created_count=created,
                updated_count=updated,
                rejected_count=rejected_count,
                active_count=active_count,
                message=message,
            )


async def refresh_many(pool: Any, *, studios: Sequence[str], modes: Sequence[str], locales: Sequence[str], config: Optional[RefreshConfig] = None) -> List[RefreshResult]:
    results: List[RefreshResult] = []
    for studio in studios:
        for mode in modes:
            for locale in locales:
                results.append(await refresh_one_context(pool, studio=studio, mode=mode, locale=locale, config=config))
    return results
