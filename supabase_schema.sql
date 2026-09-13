-- =============================================================================
-- ReadLevel — Supabase Schema (Track 1: 账号系统与每日免费额度)
--
-- 本文件是线上 Supabase PostgreSQL 数据库真实结构的唯一权威记录。
-- =============================================================================

-- ─── 1. 建表 ────────────────────────────────────────────────────────────────

-- 用户扩展信息（业务字段，Supabase auth.users 已有基本信息）
CREATE TABLE public.user_profiles (
  user_id                   UUID PRIMARY KEY REFERENCES auth.users(id),
  reading_level             TEXT DEFAULT 'B1',
  is_subscribed             BOOLEAN NOT NULL DEFAULT FALSE,
  subscription_expires_at   TIMESTAMPTZ,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 注意：daily_char_limit 列实际不存在于线上数据库。
-- 当前免费/订阅两档限额（5000/50000 字符）由 backend.py 中
-- profile.get("daily_char_limit", 5000/50000) 硬编码兜底。
-- reading_level 和 subscription_expires_at 是历史遗留列，
-- 暂未接入任何业务逻辑。

-- 每日用量记录
CREATE TABLE public.daily_usage (
  user_id     UUID NOT NULL REFERENCES auth.users(id),
  usage_date  DATE NOT NULL,
  chars_used  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, usage_date)
);

CREATE INDEX idx_daily_usage_lookup ON public.daily_usage (user_id, usage_date);

-- ─── 2. 自动创建 user_profiles 的触发器 ─────────────────────────────────────
-- 当 Supabase Auth 注册新用户时，自动在 user_profiles 插入一行默认记录。

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO public.user_profiles (user_id)
  VALUES (NEW.id);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_new_user();

-- ─── 3. RLS（Row Level Security）─────────────────────────────────────────────
-- 后端通过 service_role key 连接，自动绕过 RLS。
-- RLS 存在的意义：防止有人绕开后端、直接拿前端公开的 anon key
-- 篡改自己的 chars_used / is_subscribed。

ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_usage ENABLE ROW LEVEL SECURITY;

-- user_profiles: anon/authenticated 只能 SELECT 自己的行，禁止一切写入
CREATE POLICY "user_profiles_select_own"
  ON public.user_profiles
  FOR SELECT
  TO authenticated, anon
  USING (auth.uid() = user_id);

-- daily_usage: anon/authenticated 只能 SELECT 自己的行，禁止一切写入
CREATE POLICY "daily_usage_select_own"
  ON public.daily_usage
  FOR SELECT
  TO authenticated, anon
  USING (auth.uid() = user_id);

-- ─── 4. 原子扣减 RPC ────────────────────────────────────────────────────────
-- 将"检查额度 + 扣减"合并为一次原子操作，避免并发请求间的竞态条件。
-- SECURITY DEFINER 使其以函数所有者权限执行，绕过 RLS。

CREATE OR REPLACE FUNCTION public.consume_quota(
  p_user_id UUID,
  p_today DATE,
  p_chars_to_add INTEGER,
  p_limit INTEGER
) RETURNS INTEGER AS $$
DECLARE
  v_new_chars INTEGER;
BEGIN
  -- 1. 确保当天存在记录（首次请求时 INSERT，已存在则跳过）
  INSERT INTO public.daily_usage (user_id, usage_date, chars_used)
  VALUES (p_user_id, p_today, 0)
  ON CONFLICT (user_id, usage_date) DO NOTHING;

  -- 2. 原子性地"检查 + 扣减"：只有 chars_used + len <= limit 时才 UPDATE
  UPDATE public.daily_usage
  SET chars_used = chars_used + p_chars_to_add
  WHERE user_id = p_user_id
    AND usage_date = p_today
    AND chars_used + p_chars_to_add <= p_limit
  RETURNING chars_used INTO v_new_chars;

  -- 3. 返回新值（若未更新则隐式返回 NULL，后端依此判定 429）
  RETURN v_new_chars;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ─── 5. GRANT 权限 ──────────────────────────────────────────────────────────
-- service_role 绕过 RLS 但仍需底层 GRANT 才能访问表。
-- authenticated/anon 需要 SELECT GRANT 配合 RLS Policy 生效。

GRANT ALL ON public.user_profiles TO service_role;
GRANT ALL ON public.daily_usage TO service_role;
GRANT SELECT ON public.user_profiles TO authenticated, anon;
GRANT SELECT ON public.daily_usage TO authenticated, anon;
