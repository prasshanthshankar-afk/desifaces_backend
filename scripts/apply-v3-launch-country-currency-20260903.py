#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH = ROOT / "services/svc-core/app/app/routes/auth.py"
SECURITY = ROOT / "services/svc-core/app/app/security.py"
DEPS = ROOT / "services/svc-pricing/app/app/api/deps.py"


def once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"FAIL {label}: expected exactly 1 match, found {count}")
    return text.replace(old, new, 1)


auth = AUTH.read_text()
if "LAUNCH_COUNTRY_CURRENCY_V1" not in auth:
    auth = once(
        auth,
        "class RegisterRequest(BaseModel):\n    email: EmailStr\n    password: str = Field(min_length=8, max_length=256)\n    full_name: str = Field(default=\"\", max_length=200)\n",
        "class RegisterRequest(BaseModel):\n    email: EmailStr\n    password: str = Field(min_length=8, max_length=256)\n    full_name: str = Field(default=\"\", max_length=200)\n    country_code: str | None = Field(default=None, min_length=2, max_length=2)  # LAUNCH_COUNTRY_CURRENCY_V1\n",
        "register country contract",
    )
    auth = once(
        auth,
        "class LoginRequest(BaseModel):\n    email: EmailStr\n    password: str = Field(min_length=1, max_length=256)\n    device_id: str | None = Field(default=None, max_length=200)\n    client_type: str | None = Field(default=None)  # 'web'|'ios'|'android'\n",
        "class LoginRequest(BaseModel):\n    email: EmailStr\n    password: str = Field(min_length=1, max_length=256)\n    device_id: str | None = Field(default=None, max_length=200)\n    client_type: str | None = Field(default=None)  # 'web'|'ios'|'android'\n    country_code: str | None = Field(default=None, min_length=2, max_length=2)\n",
        "login country contract",
    )
    auth = once(
        auth,
        "class AuthUser(BaseModel):\n    id: str\n    email: EmailStr\n    full_name: str = \"\"\n    tier: str | None = None\n    is_active: bool = True\n    roles: list[str] = Field(default_factory=list)\n",
        "class AuthUser(BaseModel):\n    id: str\n    email: EmailStr\n    full_name: str = \"\"\n    tier: str | None = None\n    is_active: bool = True\n    roles: list[str] = Field(default_factory=list)\n    country_code: str | None = None\n",
        "auth user country contract",
    )
    auth = once(
        auth,
        "def _normalize_device_id(v: str | None) -> str | None:\n    s = (v or \"\").strip()\n    return s if s else None\n\n\nasync def _fetch_roles",
        "def _normalize_device_id(v: str | None) -> str | None:\n    s = (v or \"\").strip()\n    return s if s else None\n\n\ndef _normalize_country_code(v: str | None) -> str | None:\n    s = str(v or \"\").strip().upper()\n    if not s:\n        return None\n    if len(s) != 2 or not s.isalpha():\n        raise HTTPException(status_code=400, detail=\"invalid_country_code\")\n    return s\n\n\ndef _currency_for_country(country_code: str | None) -> str:\n    return \"INR\" if _normalize_country_code(country_code) == \"IN\" else \"USD\"\n\n\nasync def _sync_user_country_currency(conn, user_id: str, country_code: str | None) -> str | None:\n    cc = _normalize_country_code(country_code)\n    if not cc:\n        return None\n    await conn.execute(\n        \"UPDATE core.users SET country_code=$2, updated_at=now() WHERE id=$1::uuid\",\n        user_id, cc,\n    )\n    # Billing accounts may not exist until pricing bootstrap. This update is\n    # deliberately repeatable and is called again after bootstrap.\n    try:\n        await conn.execute(\n            \"\"\"\n            UPDATE public.pricing_billing_accounts ba\n            SET default_currency=$2, updated_at=now()\n            FROM public.pricing_billing_account_members bam\n            WHERE bam.billing_account_id=ba.id\n              AND bam.user_id=$1::uuid\n              AND bam.status='active'\n              AND ba.default_currency IS DISTINCT FROM $2\n            \"\"\",\n            user_id, _currency_for_country(cc),\n        )\n    except Exception:\n        logger.exception(\"auth_country_billing_currency_sync_failed\", extra={\"user_id\": user_id, \"country_code\": cc})\n    return cc\n\n\nasync def _fetch_roles",
        "country helpers",
    )
    auth = once(
        auth,
        "SELECT id::text AS id, email, full_name, tier, is_active\n        FROM core.users",
        "SELECT id::text AS id, email, full_name, tier, is_active, country_code\n        FROM core.users",
        "me select country",
    )
    auth = once(
        auth,
        "        is_active=bool(row[\"is_active\"]),\n        roles=roles,\n    )\n\n\n\ndef _decode_access_claims",
        "        is_active=bool(row[\"is_active\"]),\n        roles=roles,\n        country_code=_normalize_country_code(row[\"country_code\"]),\n    )\n\n\n\ndef _decode_access_claims",
        "me response country",
    )
    auth = once(
        auth,
        "    client_type: str | None,\n) -> TokenResponse:\n",
        "    client_type: str | None,\n    country_code: str | None = None,\n) -> TokenResponse:\n",
        "issue token signature country",
    )
    auth = once(
        auth,
        "        tier=tier,\n        roles=roles,\n    )\n    refresh = mint_refresh_token()",
        "        tier=tier,\n        roles=roles,\n        country_code=_normalize_country_code(country_code),\n    )\n    refresh = mint_refresh_token()",
        "mint token country",
    )
    auth = once(
        auth,
        "            is_active=bool(is_active),\n            roles=roles,\n        ),\n    )\n\n\nasync def _authenticate_bearer_user",
        "            is_active=bool(is_active),\n            roles=roles,\n            country_code=_normalize_country_code(country_code),\n        ),\n    )\n\n\nasync def _authenticate_bearer_user",
        "token response country",
    )
    auth = once(
        auth,
        "            INSERT INTO core.users(email, password_hash, full_name, is_active)\n            VALUES ($1, $2, $3, false)\n            RETURNING id::text AS id, email, full_name, tier, is_active\n",
        "            INSERT INTO core.users(email, password_hash, full_name, is_active, country_code)\n            VALUES ($1, $2, $3, false, $4)\n            RETURNING id::text AS id, email, full_name, tier, is_active, country_code\n",
        "register insert country",
    )
    auth = once(
        auth,
        "            email,\n            pw_hash,\n            req.full_name.strip(),\n        )\n",
        "            email,\n            pw_hash,\n            req.full_name.strip(),\n            _normalize_country_code(req.country_code),\n        )\n",
        "register insert args",
    )
    # Verify-email lookup should carry the country selected at registration.
    auth = once(
        auth,
        "            SELECT id::text AS id, email, full_name, tier, is_active\n            FROM core.users\n            WHERE id = $1::uuid\n",
        "            SELECT id::text AS id, email, full_name, tier, is_active, country_code\n            FROM core.users\n            WHERE id = $1::uuid\n",
        "register verify select country",
    )
    auth = once(
        auth,
        "            device_id=req.device_id,\n            client_type=req.client_type,\n        )\n\n        bootstrap_user_id = user_id",
        "            device_id=req.device_id,\n            client_type=req.client_type,\n            country_code=user[\"country_code\"],\n        )\n\n        bootstrap_user_id = user_id",
        "register token country",
    )
    auth = once(
        auth,
        "            SELECT id::text AS id, email, full_name, password_hash, tier, is_active\n            FROM core.users\n            WHERE lower(email)=lower($1)\n",
        "            SELECT id::text AS id, email, full_name, password_hash, tier, is_active, country_code\n            FROM core.users\n            WHERE lower(email)=lower($1)\n",
        "login select country",
    )
    auth = once(
        auth,
        "        response = await _issue_login_tokens(\n            conn,\n            user_id=user[\"id\"],",
        "        login_country = _normalize_country_code(req.country_code) or _normalize_country_code(user[\"country_code\"])\n        if login_country:\n            await _sync_user_country_currency(conn, user[\"id\"], login_country)\n\n        response = await _issue_login_tokens(\n            conn,\n            user_id=user[\"id\"],",
        "login sync country",
    )
    auth = once(
        auth,
        "            device_id=req.device_id,\n            client_type=req.client_type,\n        )\n\n        bootstrap_user_id = user[\"id\"]",
        "            device_id=req.device_id,\n            client_type=req.client_type,\n            country_code=login_country,\n        )\n\n        bootstrap_user_id = user[\"id\"]",
        "login token country",
    )
    # After pricing bootstrap, align a newly created billing account as well.
    auth = once(
        auth,
        "    await _best_effort_bootstrap_pricing_for_user(\n        user_id=bootstrap_user_id,\n        email=bootstrap_email,\n        tier=bootstrap_tier,\n        source=\"svc_core_login\",\n    )\n\n    return response",
        "    await _best_effort_bootstrap_pricing_for_user(\n        user_id=bootstrap_user_id,\n        email=bootstrap_email,\n        tier=bootstrap_tier,\n        source=\"svc_core_login\",\n    )\n    if login_country:\n        async with (await get_pool()).acquire() as conn:\n            await _sync_user_country_currency(conn, bootstrap_user_id, login_country)\n\n    return response",
        "post bootstrap login country sync",
    )
    auth = once(
        auth,
        "                   u.tier,\n                   u.is_active\n            FROM core.sessions s",
        "                   u.tier,\n                   u.is_active,\n                   u.country_code\n            FROM core.sessions s",
        "refresh select country",
    )
    # This mint is the refresh-route mint; the issue-login mint already gained country above.
    marker = "        access = mint_access_jwt(\n            user_id=sess[\"user_id\"],\n            email=sess[\"email\"],\n            tier=sess[\"tier\"],\n            roles=roles,\n        )"
    auth = once(
        auth,
        marker,
        "        access = mint_access_jwt(\n            user_id=sess[\"user_id\"],\n            email=sess[\"email\"],\n            tier=sess[\"tier\"],\n            roles=roles,\n            country_code=_normalize_country_code(sess[\"country_code\"]),\n        )",
        "refresh mint country",
    )
    auth = once(
        auth,
        "                is_active=bool(sess[\"is_active\"]),\n                roles=roles,\n            ),\n        )\n\n\n@router.post(\"/logout\"",
        "                is_active=bool(sess[\"is_active\"]),\n                roles=roles,\n                country_code=_normalize_country_code(sess[\"country_code\"]),\n            ),\n        )\n\n\n@router.post(\"/logout\"",
        "refresh response country",
    )
    AUTH.write_text(auth)

security = SECURITY.read_text()
if "country_code: str | None = None" not in security:
    security = once(
        security,
        "def mint_access_jwt(*, user_id: str, email: str, tier: str, roles: List[str]) -> str:\n",
        "def mint_access_jwt(*, user_id: str, email: str, tier: str, roles: List[str], country_code: str | None = None) -> str:\n",
        "jwt signature country",
    )
    security = once(
        security,
        "        \"roles\": roles,\n    }\n",
        "        \"roles\": roles,\n        \"country_code\": (str(country_code or \"\").strip().upper() or None),\n    }\n",
        "jwt payload country",
    )
    SECURITY.write_text(security)

deps = DEPS.read_text()
if "CANONICAL_LOGIN_COUNTRY_V1" not in deps:
    deps = once(
        deps,
        "    cc = (x_country_code or \"\").strip().upper()\n    if cc and len(cc) != 2:\n        # Allow empty; if provided, must look like ISO2\n        raise HTTPException(status_code=400, detail=\"invalid X-Country-Code\")\n\n    return AuthContext(user_id=uid, bearer_token=bearer, country_code=cc)\n",
        "    header_cc = (x_country_code or \"\").strip().upper()\n    if header_cc and (len(header_cc) != 2 or not header_cc.isalpha()):\n        raise HTTPException(status_code=400, detail=\"invalid X-Country-Code\")\n\n    # CANONICAL_LOGIN_COUNTRY_V1: once authentication succeeds, country/currency\n    # comes from core.users. A header may bootstrap older accounts only when the\n    # canonical DB value is still empty; it cannot override an existing value.\n    pool = await ensure_db_pool()\n    async with pool.acquire() as conn:\n        db_cc = await conn.fetchval(\n            \"SELECT country_code FROM core.users WHERE id=$1::uuid\", uid\n        )\n        cc = str(db_cc or \"\").strip().upper()\n        if not cc and header_cc:\n            cc = header_cc\n            await conn.execute(\n                \"UPDATE core.users SET country_code=$2, updated_at=now() WHERE id=$1::uuid AND country_code IS NULL\",\n                uid, cc,\n            )\n        if cc and (len(cc) != 2 or not cc.isalpha()):\n            raise HTTPException(status_code=500, detail=\"invalid_canonical_country_code\")\n\n    return AuthContext(user_id=uid, bearer_token=bearer, country_code=cc)\n",
        "canonical pricing country",
    )
    DEPS.write_text(deps)

print("LAUNCH_COUNTRY_CURRENCY_PATCH=APPLIED")
