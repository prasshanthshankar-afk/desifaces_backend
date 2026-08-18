BEGIN;

-- ============================================================================
-- desifaces-v3 canonical account-context invariant
--
-- V3-C3 evidence found core.users rows with no pricing_billing_accounts rows.
-- The original billing-account migration (2026_03_11) correctly backfilled
-- users that already had pricing_credit_accounts at migration time, but later
-- user/free-credit bootstrap paths could create credit accounts without a
-- billing_account_id.  V3 canonical contracts require a durable AccountRef for
-- ownership/billing context, so this migration closes that lifecycle gap.
--
-- This migration is intentionally idempotent and additive.  It does not alter
-- balances, reserved credits, subscriptions, entitlements, jobs, or media.
-- ============================================================================

CREATE OR REPLACE FUNCTION public.df_v3_resolve_or_create_user_account(p_user_id uuid)
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
    v_account_id uuid;
BEGIN
    IF p_user_id IS NULL THEN
        RAISE EXCEPTION 'df_v3_account_context_requires_user_id';
    END IF;

    -- 1) Preserve an already active/default account membership when present.
    SELECT ba.id
      INTO v_account_id
      FROM public.pricing_billing_account_members bam
      JOIN public.pricing_billing_accounts ba
        ON ba.id = bam.billing_account_id
     WHERE bam.user_id = p_user_id
       AND bam.status = 'active'
       AND ba.status = 'active'
     ORDER BY
       bam.is_default DESC,
       CASE bam.role
         WHEN 'owner' THEN 0
         WHEN 'finance_admin' THEN 1
         WHEN 'member' THEN 2
         WHEN 'viewer' THEN 3
         ELSE 4
       END,
       bam.created_at ASC
     LIMIT 1;

    IF v_account_id IS NOT NULL THEN
        RETURN v_account_id;
    END IF;

    -- 2) Preserve an already-linked active credit account.
    SELECT ba.id
      INTO v_account_id
      FROM public.pricing_credit_accounts pca
      JOIN public.pricing_billing_accounts ba
        ON ba.id = pca.billing_account_id
     WHERE pca.user_id = p_user_id
       AND ba.status = 'active'
     LIMIT 1;

    IF v_account_id IS NOT NULL THEN
        RETURN v_account_id;
    END IF;

    -- 3) Reuse the canonical personal account code when it already exists.
    SELECT ba.id
      INTO v_account_id
      FROM public.pricing_billing_accounts ba
     WHERE ba.account_code = 'user:' || p_user_id::text
       AND ba.status = 'active'
     LIMIT 1;

    -- 4) Otherwise create the user's durable personal account.
    IF v_account_id IS NULL THEN
        INSERT INTO public.pricing_billing_accounts (
            account_code,
            account_type,
            display_name,
            status,
            billing_mode,
            default_currency,
            meta_json
        )
        VALUES (
            'user:' || p_user_id::text,
            'individual',
            'User ' || p_user_id::text,
            'active',
            'prepaid',
            'USD',
            jsonb_build_object(
                'bootstrap_source', 'v3_canonical_account_context',
                'migration', '2026_08_18_v3_canonical_account_context'
            )
        )
        ON CONFLICT (account_code) DO NOTHING;

        SELECT ba.id
          INTO v_account_id
          FROM public.pricing_billing_accounts ba
         WHERE ba.account_code = 'user:' || p_user_id::text
           AND ba.status = 'active'
         LIMIT 1;
    END IF;

    IF v_account_id IS NULL THEN
        RAISE EXCEPTION 'df_v3_active_account_context_unavailable:%', p_user_id;
    END IF;

    -- Personal account owner/default membership.  Do not invent a second account
    -- when an active membership was already found above.
    INSERT INTO public.pricing_billing_account_members (
        billing_account_id,
        user_id,
        role,
        is_default,
        status,
        meta_json
    )
    VALUES (
        v_account_id,
        p_user_id,
        'owner',
        true,
        'active',
        jsonb_build_object(
            'bootstrap_source', 'v3_canonical_account_context',
            'migration', '2026_08_18_v3_canonical_account_context'
        )
    )
    ON CONFLICT (billing_account_id, user_id) DO UPDATE
       SET status = 'active',
           is_default = true,
           updated_at = now();

    RETURN v_account_id;
END;
$$;

-- Backfill canonical account context for every currently registered V3 user.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT id FROM core.users LOOP
        PERFORM public.df_v3_resolve_or_create_user_account(r.id);
    END LOOP;
END;
$$;

-- Link the user-scoped pricing persistence that already exposes
-- billing_account_id.  Only NULL ownership is filled; existing ownership wins.
UPDATE public.pricing_credit_accounts pca
   SET billing_account_id = public.df_v3_resolve_or_create_user_account(pca.user_id),
       updated_at = now()
 WHERE pca.billing_account_id IS NULL;

UPDATE public.pricing_credit_reservations pcr
   SET billing_account_id = public.df_v3_resolve_or_create_user_account(pcr.user_id)
 WHERE pcr.billing_account_id IS NULL;

UPDATE public.pricing_credit_ledger_events ple
   SET billing_account_id = public.df_v3_resolve_or_create_user_account(ple.user_id)
 WHERE ple.billing_account_id IS NULL
   AND ple.user_id IS NOT NULL;

UPDATE public.pricing_user_entitlements pue
   SET billing_account_id = public.df_v3_resolve_or_create_user_account(pue.user_id)
 WHERE pue.billing_account_id IS NULL;

-- pricing_credit_lots was introduced after the original billing-account schema.
-- Link it when the table/column is available without making this migration
-- dependent on a particular historical rollout order.
DO $$
BEGIN
    IF to_regclass('public.pricing_credit_lots') IS NOT NULL
       AND EXISTS (
           SELECT 1
             FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'pricing_credit_lots'
              AND column_name = 'billing_account_id'
       ) THEN
        EXECUTE $sql$
            UPDATE public.pricing_credit_lots pcl
               SET billing_account_id = public.df_v3_resolve_or_create_user_account(pcl.user_id),
                   updated_at = now()
             WHERE pcl.billing_account_id IS NULL
               AND pcl.user_id IS NOT NULL
        $sql$;
    END IF;
END;
$$;

-- Future-user invariant: every V3 core user receives a durable personal account
-- immediately after registration.  This is intentionally AFTER INSERT so the
-- user row exists before membership/account ownership is established.
CREATE OR REPLACE FUNCTION public.df_v3_core_user_account_context_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM public.df_v3_resolve_or_create_user_account(NEW.id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_df_v3_core_user_account_context ON core.users;
CREATE TRIGGER trg_df_v3_core_user_account_context
AFTER INSERT ON core.users
FOR EACH ROW
EXECUTE FUNCTION public.df_v3_core_user_account_context_trigger();

-- Future credit-account invariant: a credit wallet cannot remain detached from
-- canonical account ownership.  Existing explicit account ownership is always
-- preserved.
CREATE OR REPLACE FUNCTION public.df_v3_credit_account_context_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.billing_account_id IS NULL THEN
        NEW.billing_account_id := public.df_v3_resolve_or_create_user_account(NEW.user_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_df_v3_credit_account_context ON public.pricing_credit_accounts;
CREATE TRIGGER trg_df_v3_credit_account_context
BEFORE INSERT OR UPDATE OF billing_account_id
ON public.pricing_credit_accounts
FOR EACH ROW
EXECUTE FUNCTION public.df_v3_credit_account_context_trigger();

COMMIT;
