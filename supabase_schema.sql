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
-- 当前免费/订阅两档限额（5000/80000 字符）由 backend.py 中
-- profile.get("daily_char_limit", 5000/80000) 硬编码兜底。
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

-- ─── 6. EPUB 任务表 (Track 3: 异步任务处理管道) ───────────────────────────────

CREATE TABLE IF NOT EXISTS public.epub_tasks (
  -- 1. 任务标识与归属
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,

  -- 2. 状态机与多维进度
  status                TEXT NOT NULL DEFAULT 'pending',
                        -- 枚举: 'pending', 'scanning', 'translating', 'generating', 'completed', 'failed'
  progress_percent      INTEGER NOT NULL DEFAULT 0,
  current_step_desc     TEXT DEFAULT '排队等待处理...',
  total_chapters        INTEGER DEFAULT 0,
  current_chapter       INTEGER DEFAULT 0,
  total_unique_words    INTEGER DEFAULT 0,
  translated_words      INTEGER DEFAULT 0,

  -- 3. 固化生成配置 (生成前选定)
  target_lang           TEXT NOT NULL DEFAULT 'zh-Hans',
  difficulty_level      INTEGER NOT NULL DEFAULT 900,
  original_filename     TEXT NOT NULL,

  -- 4. 存储路径 (Supabase Storage: 私有 Bucket)
  storage_input_path    TEXT NOT NULL,
  storage_output_path   TEXT,
  download_url          TEXT,

  -- 5. 容错与审计 (面向用户友好信息与面向内部排查堆栈分离)
  error_message         TEXT,
  error_detail          TEXT,
  failed_words_count    INTEGER DEFAULT 0,
  failed_words          JSONB DEFAULT '[]'::jsonb,

  -- 6. 时间戳与 Worker 审计
  worker_id             TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at            TIMESTAMPTZ,
  completed_at          TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_epub_tasks_user_id ON public.epub_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_epub_tasks_status_created ON public.epub_tasks(status, created_at);

ALTER TABLE public.epub_tasks ENABLE ROW LEVEL SECURITY;

CREATE POLICY "epub_tasks_select_own"
  ON public.epub_tasks
  FOR SELECT
  TO authenticated, anon
  USING (auth.uid() = user_id);

GRANT ALL ON public.epub_tasks TO service_role;
GRANT SELECT ON public.epub_tasks TO authenticated, anon;

-- ─── 7. 原子领取 EPUB 任务 RPC ────────────────────────────────────────────────
-- 用于 Worker 安全领取排队的任务，防止多个 Worker 重复领取。
-- 内部使用 FOR UPDATE SKIP LOCKED 实现。

CREATE OR REPLACE FUNCTION public.claim_epub_task(
  p_worker_id TEXT
) RETURNS SETOF public.epub_tasks AS $$
DECLARE
  v_task_id UUID;
BEGIN
  -- 1. 查找最老的一个待处理任务并锁定它
  SELECT id INTO v_task_id
  FROM public.epub_tasks
  WHERE status = 'pending'
  ORDER BY created_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT 1;

  -- 2. 如果找到了，更新状态并返回完整行
  IF v_task_id IS NOT NULL THEN
    RETURN QUERY
    UPDATE public.epub_tasks
    SET status = 'scanning',
        worker_id = p_worker_id,
        started_at = now(),
        updated_at = now(),
        current_step_desc = '开始处理任务...'
    WHERE id = v_task_id
    RETURNING *;
  END IF;
  
  RETURN;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ─── 8. Track 4: 支付订阅状态表与防乱序时间戳 ─────────────────────────────────

-- 1. 为 user_profiles 扩展订阅事件时序对比字段与外部关联 ID (初始为 NULL)
ALTER TABLE public.user_profiles 
  ADD COLUMN IF NOT EXISTS subscription_updated_at TIMESTAMPTZ DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS subscription_id         TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS customer_id             TEXT DEFAULT NULL;

-- 2. 创建 Webhook 处理流水审计表
CREATE TABLE IF NOT EXISTS public.webhook_logs (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id              TEXT UNIQUE,                -- LemonSqueezy 事件唯一标识或复合键 (用于幂等去重)
  event_name            TEXT NOT NULL,              -- 事件名称 (如 subscription_created)
  user_id               UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  event_updated_at      TIMESTAMPTZ NOT NULL,       -- LemonSqueezy payload 中的 data.attributes.updated_at
  status                TEXT NOT NULL DEFAULT 'processed', -- 'processed' | 'ignored_stale' | 'error'
  processed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_logs_user_id ON public.webhook_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_webhook_logs_event_id ON public.webhook_logs(event_id);

-- 3. 启用严格行级安全 (RLS)
ALTER TABLE public.webhook_logs ENABLE ROW LEVEL SECURITY;

-- 4. 权限隔离：不为 authenticated 或 anon 创建任何 Policy，仅授权 service_role 完全访问
REVOKE ALL ON public.webhook_logs FROM PUBLIC;
REVOKE ALL ON public.webhook_logs FROM anon, authenticated;
GRANT ALL ON public.webhook_logs TO service_role;

