-- Applied manually via Supabase SQL Editor on 2026-10-23 (project: lexile_ruby).
-- Recorded here so supabase_schema.sql consumers know the live state.
-- Backend and epub_worker use the service_role key, which keeps EXECUTE.

-- 1. Revoke public EXECUTE on SECURITY DEFINER functions
REVOKE EXECUTE ON FUNCTION public.consume_quota(uuid, date, integer, integer) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.claim_epub_task(text) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.consume_quota(uuid, date, integer, integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_epub_task(text) TO service_role;

-- 2. Pin search_path
ALTER FUNCTION public.consume_quota(uuid, date, integer, integer) SET search_path = public, pg_temp;
ALTER FUNCTION public.claim_epub_task(text) SET search_path = public, pg_temp;
ALTER FUNCTION public.handle_new_user() SET search_path = public, pg_temp;

-- 3. Drop unneeded table privileges (authenticated keeps SELECT, gated by RLS)
REVOKE TRUNCATE, TRIGGER, REFERENCES ON public.user_profiles, public.daily_usage, public.epub_tasks FROM anon, authenticated;
REVOKE SELECT ON public.user_profiles, public.daily_usage, public.epub_tasks FROM anon;

-- Rollback (functions only):
-- GRANT EXECUTE ON FUNCTION public.consume_quota(uuid, date, integer, integer), public.claim_epub_task(text) TO anon, authenticated;
