#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

ASSISTANT="${PIKU_CONTAINER:-df-svc-assistant}"
docker inspect "$ASSISTANT" >/dev/null 2>&1 || { echo "FAIL: missing $ASSISTANT"; exit 2; }
[[ "$(docker inspect -f '{{.State.Running}}' "$ASSISTANT")" == "true" ]] || { echo "FAIL: $ASSISTANT not running"; exit 2; }

echo "============================================================"
echo " desifaces DEV — PIKU ACCOUNT CONTEXT E2E CERTIFICATION"
echo "============================================================"
echo "assistant_container=$ASSISTANT"
echo "scope=AUTHENTICATED_READ_CONTEXT_PLUS_TRANSIENT_ASSISTANT_CHAT"
echo "billing_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"

docker exec -i "$ASSISTANT" python - <<'PY'
import asyncio
import json
import os
import re
import time
from uuid import UUID

import asyncpg
import httpx
import jwt

ASSISTANT_BASE="http://127.0.0.1:8012"
DASHBOARD_BASE=(os.getenv("DF_DASHBOARD_BASE_URL") or "http://svc-dashboard:8005").rstrip("/")
PRICING_BASE=(os.getenv("DF_PRICING_BASE_URL") or "http://svc-pricing:8009").rstrip("/")

def norm_num(v):
    if v is None:
        return None
    try:
        f=float(v)
        return int(f) if f.is_integer() else f
    except Exception:
        return v

def assert_eq(label, a, b):
    if norm_num(a) != norm_num(b):
        raise AssertionError(f"{label}: assistant={a!r} source={b!r}")
    print(f"{label}=PASS value={norm_num(a)}")

async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    transient_sessions=[]
    try:
        # Choose a real DEV user that exists in core.users and has the most
        # recently updated pricing account. No user id/token is printed.
        row=await conn.fetchrow("""
            select u.id
            from core.users u
            join public.pricing_credit_accounts p on p.user_id=u.id
            order by p.updated_at desc
            limit 1
        """)
        if not row:
            raise SystemExit("FAIL: no DEV user with pricing account found")
        user_id=UUID(str(row["id"]))

        now=int(time.time())
        token=jwt.encode(
            {
                "sub":str(user_id),
                "aud":os.getenv("JWT_AUDIENCE") or "desifaces_clients",
                "iss":os.getenv("JWT_ISSUER") or "desifaces",
                "iat":now,
                "exp":now+600,
            },
            os.environ["JWT_SECRET"],
            algorithm=os.getenv("JWT_ALG") or "HS256",
        )
        headers={"Authorization":f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=20.0) as client:
            # Authoritative upstream reads using the same user token.
            pr=await client.get(
                f"{PRICING_BASE}/api/pricing/me/spending/summary",
                params={"period":"month"},
                headers=headers,
            )
            pr.raise_for_status()
            pricing=pr.json()

            lib=await client.get(
                f"{DASHBOARD_BASE}/api/dashboard/library",
                params={"type":"all","limit":50,"offset":0},
                headers=headers,
            )
            lib.raise_for_status()
            library=lib.json()

            home=await client.get(
                f"{DASHBOARD_BASE}/api/dashboard/home",
                headers=headers,
            )
            home.raise_for_status()
            dashboard_home=home.json()

            # Same privacy-projected context piku receives.
            ctxr=await client.get(
                f"{ASSISTANT_BASE}/api/assistant/context",
                params={"surface":"web","screen":"dashboard"},
                headers=headers,
            )
            ctxr.raise_for_status()
            ctx=ctxr.json()

            print("ASSISTANT_CONTEXT_HTTP=PASS")
            if ctx.get("context_scope") != "live_user_application_state":
                raise AssertionError("unexpected assistant context scope")
            print("ACCOUNT_WIDE_CONTEXT_SCOPE=PASS")

            # Spending parity.
            src_credits=pricing.get("credits") if isinstance(pricing.get("credits"),dict) else {}
            got_credits=(ctx.get("spending") or {}).get("credits") or {}
            for key in ("consumed","refunded","purchased","available","reserved"):
                assert_eq(f"SPENDING_{key.upper()}_PARITY",got_credits.get(key),src_credits.get(key))

            src_money=pricing.get("money") if isinstance(pricing.get("money"),dict) else {}
            got_money=(ctx.get("spending") or {}).get("money") or {}
            for key in ("paid","credit_purchases","subscriptions","invoices","refunds"):
                assert_eq(f"MONEY_{key.upper()}_PARITY",got_money.get(key),src_money.get(key))
            if str(got_money.get("currency") or "") != str(src_money.get("currency") or ""):
                raise AssertionError("money currency mismatch")
            print("MONEY_CURRENCY_PARITY=PASS")

            # Saved Work parity at the count boundary. Context intentionally
            # projects/redacts item details but must preserve count truth.
            src_items=list(library.get("items") or []) if isinstance(library,dict) else []
            got_saved=ctx.get("saved_work") if isinstance(ctx.get("saved_work"),dict) else {}
            src_total=library.get("total") if isinstance(library,dict) else None
            got_total=got_saved.get("total")
            if src_total is not None:
                assert_eq("SAVED_WORK_TOTAL_PARITY",got_total,src_total)
            assert_eq("SAVED_WORK_VISIBLE_COUNT_PARITY",got_saved.get("visible_item_count"),len(src_items[:50]))

            # Dashboard pricing context must be present for balance/runway answers.
            if not isinstance(ctx.get("pricing"),dict):
                raise AssertionError("pricing context missing")
            print("DASHBOARD_PRICING_CONTEXT=PASS")

            # Privacy projection: user/account ids and signed URLs must not leak
            # into the model-facing context.
            raw_ctx=json.dumps(ctx,sort_keys=True)
            forbidden=(
                str(user_id),
                "account_id",
                "gateway_customer_id",
                "stripe_customer",
                "sig=",
                "se=",
                "sp=",
            )
            leaked=[x for x in forbidden if x and x in raw_ctx]
            if leaked:
                raise AssertionError("privacy projection leaked forbidden markers: "+",".join(leaked))
            print("PIKU_CONTEXT_PRIVACY_PROJECTION=PASS")

            async def chat(message):
                r=await client.post(
                    f"{ASSISTANT_BASE}/api/assistant/chat",
                    headers=headers,
                    json={
                        "message":message,
                        "context":{"surface":"web","screen":"dashboard"},
                    },
                )
                r.raise_for_status()
                out=r.json()
                sid=str(out.get("session_id") or "")
                if sid:
                    transient_sessions.append(sid)
                answer=str(out.get("answer") or "")
                if not answer:
                    raise AssertionError("empty assistant answer")
                return answer,out

            # Account spending answer must contain authoritative month usage.
            spend_answer,_=await chat("What have I spent this month?")
            consumed=norm_num(src_credits.get("consumed"))
            paid=norm_num(src_money.get("paid"))
            if consumed is not None and str(consumed) not in spend_answer.replace(",",""):
                raise AssertionError(f"spending answer missing consumed credits={consumed}: {spend_answer}")
            if paid is not None and str(paid) not in spend_answer.replace(",",""):
                raise AssertionError(f"spending answer missing paid amount={paid}: {spend_answer}")
            print("PIKU_ACCOUNT_SPENDING_ANSWER=PASS")

            # Saved Work answer must use the projected live count.
            saved_answer,_=await chat("What is in my saved work?")
            expected_saved=norm_num(got_total if got_total is not None else got_saved.get("visible_item_count"))
            if expected_saved is not None and str(expected_saved) not in saved_answer.replace(",",""):
                raise AssertionError(f"saved-work answer missing count={expected_saved}: {saved_answer}")
            print("PIKU_SAVED_WORK_ANSWER=PASS")

            # Credit balance answer must match dashboard pricing context where available.
            credit_answer,_=await chat("How many credits do I have?")
            pricing_ctx=ctx.get("pricing") if isinstance(ctx.get("pricing"),dict) else {}
            pc=pricing_ctx.get("credits") if isinstance(pricing_ctx.get("credits"),dict) else {}
            possible=[
                pc.get("available_credits"),
                pc.get("total_available"),
                pc.get("available"),
            ]
            expected_available=next((norm_num(x) for x in possible if x is not None),None)
            if expected_available is None:
                # Fallback to spending account balance; the assistant should not invent.
                expected_available=norm_num(src_credits.get("available"))
            if expected_available is not None and str(expected_available) not in credit_answer.replace(",",""):
                raise AssertionError(f"credit answer missing available={expected_available}: {credit_answer}")
            print("PIKU_CREDIT_BALANCE_ANSWER=PASS")

            print("PIKU_ACCOUNT_CONTEXT_E2E=PASS")

    finally:
        # Remove transient assistant chat sessions generated by certification.
        # This touches only assistant session/message state; no billing/generation state.
        if transient_sessions:
            now=int(time.time())
            token=jwt.encode(
                {
                    "sub":str(user_id),
                    "aud":os.getenv("JWT_AUDIENCE") or "desifaces_clients",
                    "iss":os.getenv("JWT_ISSUER") or "desifaces",
                    "iat":now,
                    "exp":now+600,
                },
                os.environ["JWT_SECRET"],
                algorithm=os.getenv("JWT_ALG") or "HS256",
            )
            headers={"Authorization":f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=20.0) as client:
                for sid in transient_sessions:
                    try:
                        r=await client.delete(f"{ASSISTANT_BASE}/api/assistant/sessions/{sid}",headers=headers)
                        if r.status_code not in (200,204,404):
                            print(f"WARNING: transient assistant session cleanup HTTP {r.status_code}")
                    except Exception:
                        print("WARNING: transient assistant session cleanup failed")
        await conn.close()

asyncio.run(main())
PY

echo "assistant_container=$ASSISTANT"
echo "billing_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"
