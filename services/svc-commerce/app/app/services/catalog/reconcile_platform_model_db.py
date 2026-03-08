from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncpg
from azure.storage.blob import BlobServiceClient


def _parse_az_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("az://"):
        raise ValueError(f"Not an az:// URI: {uri}")
    rest = uri[len("az://") :]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid az:// URI: {uri}")
    return parts[0], parts[1]


def _get_blob_service_client() -> BlobServiceClient:
    conn = (
        os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        or os.environ.get("AZURE_BLOB_CONNECTION_STRING")
        or os.environ.get("AZURE_STORAGE_CONN_STR")
    )
    if conn:
        return BlobServiceClient.from_connection_string(conn)

    account_url = (
        os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
        or os.environ.get("AZURE_BLOB_ACCOUNT_URL")
    )
    credential = (
        os.environ.get("AZURE_STORAGE_KEY")
        or os.environ.get("AZURE_STORAGE_SAS_TOKEN")
        or os.environ.get("AZURE_BLOB_KEY")
        or os.environ.get("AZURE_BLOB_SAS_TOKEN")
    )
    if account_url and credential:
        return BlobServiceClient(account_url=account_url, credential=credential)

    raise RuntimeError("Azure credentials not found")


def _download_manifest(manifest_uri: str) -> Dict[str, Any]:
    if manifest_uri.startswith("az://"):
        bsc = _get_blob_service_client()
        container, blob_name = _parse_az_uri(manifest_uri)
        data = bsc.get_blob_client(container=container, blob=blob_name).download_blob().readall().decode("utf-8")
        return json.loads(data)

    with open(manifest_uri, "r", encoding="utf-8") as f:
        return json.load(f)


def _approved_asset_urls(manifest: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for model in manifest.get("models", []) or []:
        for asset in model.get("assets", []) or []:
            url = str(asset.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _blob_exists(bsc: BlobServiceClient, az_uri: str) -> bool:
    if not az_uri.startswith("az://"):
        return False
    try:
        container, blob_name = _parse_az_uri(az_uri)
        return bsc.get_blob_client(container=container, blob=blob_name).exists()
    except Exception:
        return False


async def _table_exists(conn: asyncpg.Connection, table_name: str) -> bool:
    row = await conn.fetchrow(
        """
        select exists (
          select 1
          from information_schema.tables
          where table_schema='public' and table_name=$1
        ) as ok
        """,
        table_name,
    )
    return bool(row["ok"])


async def _find_text_like_columns(conn: asyncpg.Connection) -> List[asyncpg.Record]:
    return await conn.fetch(
        """
        select table_name, column_name, data_type
        from information_schema.columns
        where table_schema='public'
          and data_type in ('text', 'character varying', 'json', 'jsonb')
        order by table_name, column_name
        """
    )


async def _audit_unknown_tables(
    conn: asyncpg.Connection,
    *,
    approved_prefix: str,
) -> List[Dict[str, Any]]:
    rows = await _find_text_like_columns(conn)
    hits: List[Dict[str, Any]] = []

    for r in rows:
        table_name = str(r["table_name"])
        column_name = str(r["column_name"])

        # skip known platform tables because they are handled explicitly
        if table_name in {"platform_models", "platform_model_assets"}:
            continue

        sql = f"""
        select count(*)::int as cnt
        from public.{table_name}
        where {column_name}::text like $1
        """
        try:
            rec = await conn.fetchrow(sql, f"%{approved_prefix}%")
            cnt = int(rec["cnt"] or 0)
            if cnt > 0:
                hits.append(
                    {
                        "table_name": table_name,
                        "column_name": column_name,
                        "count": cnt,
                    }
                )
        except Exception:
            # ignore columns that cannot be safely cast or queried this way
            continue

    return hits


async def _reconcile_platform_tables(
    conn: asyncpg.Connection,
    *,
    approved_urls: Sequence[str],
    apply: bool,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "platform_model_assets_table_exists": await _table_exists(conn, "platform_model_assets"),
        "platform_models_table_exists": await _table_exists(conn, "platform_models"),
        "stale_assets": [],
        "deactivated_asset_count": 0,
        "deactivated_model_count": 0,
    }

    if not report["platform_model_assets_table_exists"]:
        return report

    rows = await conn.fetch(
        """
        select id, platform_model_id, asset_url, is_active
        from public.platform_model_assets
        """
    )

    stale_ids: List[Any] = []
    for r in rows:
        asset_url = str(r["asset_url"] or "").strip()
        if not asset_url:
            stale_ids.append(r["id"])
            report["stale_assets"].append(
                {"id": str(r["id"]), "platform_model_id": str(r["platform_model_id"]), "reason": "empty_asset_url"}
            )
            continue

        if asset_url not in approved_urls:
            stale_ids.append(r["id"])
            report["stale_assets"].append(
                {
                    "id": str(r["id"]),
                    "platform_model_id": str(r["platform_model_id"]),
                    "asset_url": asset_url,
                    "reason": "not_in_approved_manifest",
                }
            )

    if apply and stale_ids:
        await conn.execute(
            """
            update public.platform_model_assets
            set is_active=false
            where id = any($1::uuid[])
            """,
            stale_ids,
        )
        report["deactivated_asset_count"] = len(stale_ids)

    if report["platform_models_table_exists"]:
        if apply:
            rec = await conn.fetchrow(
                """
                with dead as (
                  select pm.id
                  from public.platform_models pm
                  left join public.platform_model_assets pma
                    on pma.platform_model_id = pm.id
                   and coalesce(pma.is_active, true) = true
                  group by pm.id
                  having count(pma.id) = 0
                )
                update public.platform_models pm
                   set is_active=false
                 where pm.id in (select id from dead)
                returning count(*) over() as n
                """
            )
            report["deactivated_model_count"] = int(rec["n"] or 0) if rec else 0

    return report


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Reconcile DB platform-model references against approved Azure manifest.")
    ap.add_argument(
        "--manifest-uri",
        default=os.environ.get("COMMERCE_PLATFORM_MODELS_MANIFEST", "az://commerce-training/pools/platform_models/v1/manifest.json"),
    )
    ap.add_argument("--db-dsn", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--apply", action="store_true", help="Actually deactivate stale rows in platform tables if they exist")
    args = ap.parse_args()

    if not args.db_dsn:
        raise SystemExit("DATABASE_URL or --db-dsn is required")

    manifest = _download_manifest(args.manifest_uri)
    approved_urls = _approved_asset_urls(manifest)

    approved_prefix = ""
    if args.manifest_uri.startswith("az://"):
        container, blob_name = _parse_az_uri(args.manifest_uri)
        base_prefix = os.path.dirname(blob_name)
        approved_prefix = f"az://{container}/{base_prefix.rsplit('/', 1)[0] if '/' in base_prefix else base_prefix}"

    conn = await asyncpg.connect(args.db_dsn)
    try:
        platform_report = await _reconcile_platform_tables(
            conn,
            approved_urls=approved_urls,
            apply=args.apply,
        )
        unknown_table_hits = await _audit_unknown_tables(
            conn,
            approved_prefix=approved_prefix or "az://commerce-training/pools/platform_models/",
        )

        out = {
            "manifest_uri": args.manifest_uri,
            "approved_asset_count": len(approved_urls),
            "apply": args.apply,
            "platform_report": platform_report,
            "unknown_table_hits": unknown_table_hits,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_main())