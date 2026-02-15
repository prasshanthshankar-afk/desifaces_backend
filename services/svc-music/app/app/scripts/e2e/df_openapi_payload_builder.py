from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")


def _load_openapi(path: str) -> Dict[str, Any]:
    return json.load(open(path, "r", encoding="utf-8"))


def _get_op(doc: Dict[str, Any], path: str, method: str) -> Dict[str, Any]:
    return doc["paths"][path][method.lower()]


def _resolve_ref(doc: Dict[str, Any], ref: str) -> Dict[str, Any]:
    # ref like "#/components/schemas/Foo"
    parts = ref.lstrip("#/").split("/")
    cur: Any = doc
    for p in parts:
        cur = cur[p]
    if not isinstance(cur, dict):
        return {}
    return cur


def _merge_allof(doc: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    if "allOf" not in schema:
        return schema
    props: Dict[str, Any] = {}
    required: List[str] = []
    for s in schema.get("allOf") or []:
        s2 = _resolve_schema(doc, s)
        props.update(s2.get("properties") or {})
        required.extend(list(s2.get("required") or []))
    out = dict(schema)
    out.pop("allOf", None)
    out["properties"] = props
    out["required"] = sorted(set(required))
    return out


def _resolve_schema(doc: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return _resolve_schema(doc, _resolve_ref(doc, schema["$ref"]))
    schema = _merge_allof(doc, schema)
    return schema


def _req_schema(doc: Dict[str, Any], path: str, method: str) -> Dict[str, Any]:
    op = _get_op(doc, path, method)
    rb = op.get("requestBody") or {}
    content = (rb.get("content") or {}).get("application/json") or {}
    sch = content.get("schema") or {}
    return _resolve_schema(doc, sch)


def _enum_values(schema: Dict[str, Any]) -> Optional[List[Any]]:
    ev = schema.get("enum")
    return ev if isinstance(ev, list) and ev else None


def _build_min_value(schema: Dict[str, Any]) -> Any:
    schema = schema or {}
    enum = _enum_values(schema)
    if enum:
        return enum[0]
    t = schema.get("type")
    if t == "string":
        return "string"
    if t == "integer":
        return 0
    if t == "number":
        return 0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object":
        return {}
    return None


def _items_enum(schema: Dict[str, Any]) -> Optional[List[str]]:
    if schema.get("type") != "array":
        return None
    items = schema.get("items")
    if not isinstance(items, dict):
        return None
    ev = items.get("enum")
    if isinstance(ev, list) and ev:
        return [str(x) for x in ev]
    return None


def _filter_known_fields(schema: Dict[str, Any], desired: Dict[str, Any]) -> Dict[str, Any]:
    props = schema.get("properties") or {}
    out: Dict[str, Any] = {}
    for k, v in desired.items():
        if k in props:
            out[k] = v
    # ensure required fields exist
    for rk in schema.get("required") or []:
        if rk not in out:
            out[rk] = _build_min_value(props.get(rk) or {})
    return out


def _pick_outputs(schema: Dict[str, Any]) -> Optional[List[str]]:
    props = schema.get("properties") or {}
    if "requested_outputs" not in props:
        return None
    ev = _items_enum(props["requested_outputs"])
    if not ev:
        return ["full_mix", "timed_lyrics_json"]  # best-effort default
    want = ["full_mix", "timed_lyrics_json", "music_video", "video", "clip_manifest"]
    chosen = [x for x in want if x in ev]
    return chosen if chosen else [ev[0]]


def build_create_project_payload(doc: Dict[str, Any], *, title: str, mode: str, language_hint: str, duet_layout: str) -> Dict[str, Any]:
    schema = _req_schema(doc, "/api/music/projects", "post")
    desired = {
        "title": title,
        "mode": mode,
        "language_hint": language_hint,
        "duet_layout": duet_layout,
    }
    return _filter_known_fields(schema, desired)


def build_generate_payload(
    doc: Dict[str, Any],
    *,
    quality: str,
    seed: Any,
    provider_hints: Dict[str, Any],
    mode_hint: Optional[str] = None,
    uploaded_audio_url: Optional[str] = None,
    uploaded_audio_duration_ms: Optional[int] = None,
) -> Dict[str, Any]:
    schema = _req_schema(doc, "/api/music/projects/{project_id}/generate", "post")
    desired: Dict[str, Any] = {
        "quality": quality,
        "seed": seed,
        "provider_hints": provider_hints,
    }
    outs = _pick_outputs(schema)
    if outs is not None:
        desired["requested_outputs"] = outs
    if mode_hint is not None:
        desired["mode"] = mode_hint
    if uploaded_audio_url:
        desired["uploaded_audio_url"] = uploaded_audio_url
        desired["audio_master_url"] = uploaded_audio_url
    if uploaded_audio_duration_ms is not None:
        desired["uploaded_audio_duration_ms"] = uploaded_audio_duration_ms
        desired["audio_master_duration_ms"] = uploaded_audio_duration_ms

    return _filter_known_fields(schema, desired)


def _find_uuid_anywhere(x: Any) -> Optional[str]:
    if isinstance(x, dict):
        for v in x.values():
            u = _find_uuid_anywhere(v)
            if u:
                return u
    if isinstance(x, list):
        for v in x:
            u = _find_uuid_anywhere(v)
            if u:
                return u
    if isinstance(x, str):
        m = UUID_RE.search(x)
        if m:
            return m.group(0)
    return None


def extract_project_id(resp: Dict[str, Any]) -> Optional[str]:
    for k in ("project_id", "id"):
        v = resp.get(k)
        if isinstance(v, str) and UUID_RE.fullmatch(v):
            return v
    return _find_uuid_anywhere(resp)


def extract_job_id(resp: Dict[str, Any]) -> Optional[str]:
    for k in ("job_id", "id"):
        v = resp.get(k)
        if isinstance(v, str) and UUID_RE.fullmatch(v):
            return v
    return _find_uuid_anywhere(resp)


def main() -> None:
    openapi_path = os.environ.get("OPENAPI_JSON") or "/tmp/svc-music-openapi.json"
    doc = _load_openapi(openapi_path)

    cmd = sys.argv[1]
    if cmd == "create_project":
        title = sys.argv[2]
        mode = sys.argv[3]
        lang = sys.argv[4]
        duet = sys.argv[5]
        print(json.dumps(build_create_project_payload(doc, title=title, mode=mode, language_hint=lang, duet_layout=duet)))
        return

    if cmd == "generate":
        quality = sys.argv[2]
        seed = sys.argv[3]
        mode_hint = sys.argv[4] if sys.argv[4] != "null" else None
        provider_hints = json.loads(sys.argv[5])
        uploaded_audio_url = sys.argv[6] if sys.argv[6] != "null" else None
        uploaded_audio_dur = int(sys.argv[7]) if sys.argv[7] != "null" else None

        payload = build_generate_payload(
            doc,
            quality=quality,
            seed=seed,
            provider_hints=provider_hints,
            mode_hint=mode_hint,
            uploaded_audio_url=uploaded_audio_url,
            uploaded_audio_duration_ms=uploaded_audio_dur,
        )
        print(json.dumps(payload))
        return

    print("unknown cmd", cmd, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()