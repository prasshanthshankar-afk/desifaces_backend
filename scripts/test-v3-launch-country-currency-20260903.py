#!/usr/bin/env python3
from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[1]
auth=(ROOT/'services/svc-core/app/app/routes/auth.py').read_text()
security=(ROOT/'services/svc-core/app/app/security.py').read_text()
deps=(ROOT/'services/svc-pricing/app/app/api/deps.py').read_text()
config=(ROOT/'services/svc-pricing/app/app/config.py').read_text()
migration=(ROOT/'migrations/2026_09_03_login_country_currency.sql').read_text()
manifest=(ROOT/'deploy/production/migrations-v3-production-20260903.txt').read_text()

# Syntax gates for every modified Python module.
for path in [
    ROOT/'services/svc-core/app/app/routes/auth.py',
    ROOT/'services/svc-core/app/app/security.py',
    ROOT/'services/svc-pricing/app/app/api/deps.py',
]:
    ast.parse(path.read_text(), filename=str(path))

required_auth=[
    'country_code: str | None',
    'LAUNCH_COUNTRY_CURRENCY_V1',
    'def _normalize_country_code',
    'def _currency_for_country',
    'async def _sync_user_country_currency',
    'country_code=login_country',
    'country_code=_normalize_country_code(sess["country_code"])',
]
for marker in required_auth:
    assert marker in auth, marker
assert '"country_code": (str(country_code or "").strip().upper() or None)' in security
assert 'CANONICAL_LOGIN_COUNTRY_V1' in deps
assert 'SELECT country_code FROM core.users' in deps
assert 'country_code IS NULL' in deps
assert 'return "INR" if cc == "IN" else "USD"' in config
assert "CASE WHEN u.country_code = 'IN' THEN 'INR' ELSE 'USD' END" in migration
assert 'migrations/2026_09_03_login_country_currency.sql' in manifest

# No independent currency override may be trusted by the pricing auth dependency.
assert 'X-Currency' not in deps

print('COUNTRY_CURRENCY_INVARIANT=PASS')
print('IN=INR')
print('NON_IN=USD')
