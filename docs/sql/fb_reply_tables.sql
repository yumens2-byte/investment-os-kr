-- ============================================================================
-- FB-1 (2026-10-07) Facebook Reply Engine 테이블 — 멱등 마이그레이션
-- ----------------------------------------------------------------------------
-- 컬럼 계약은 kr_reply_* 실측 스키마(2026-10-07 information_schema 조회)와 동일하다 (D8).
-- 공통 store(reply_engine/store.py)의 CAS/claim/복구 코드를 테이블명만 바꿔 재사용하기 위함.
-- 컬럼 의미 매핑: reply_tweet_id = FB comment id, response_tweet_id = 내 답글 comment id,
--                 conversation_id = FB post id, author_id = Page 범위 사용자 ID(PSID).
-- 보안: RLS 활성 + anon/authenticated 권한 회수 → service_role 키 전용
--       (마스터 기준: RLS 테이블은 service_role 필수. kr_reply_*와 달리 anon 노출 없음)
-- kr_reply_* 테이블은 이 스크립트에서 일절 변경하지 않는다.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.fb_reply_history (
  reply_tweet_id    text        NOT NULL,
  conversation_id   text,
  author_id         text        NOT NULL,
  author_username   text,
  comment_text      text,
  classification    varchar     NOT NULL,
  responded         boolean     NOT NULL DEFAULT false,
  skip_reason       varchar,
  response_text     text,
  response_tweet_id text,
  dry_run           boolean     NOT NULL,
  mode              varchar     NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  error_message     text,
  CONSTRAINT fb_reply_history_pkey PRIMARY KEY (reply_tweet_id)
);
CREATE INDEX IF NOT EXISTS idx_fb_reply_history_author_day
  ON public.fb_reply_history USING btree (author_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fb_reply_history_conv_day
  ON public.fb_reply_history USING btree (conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.fb_reply_cursor (
  account     text        NOT NULL,
  since_id    text        NOT NULL,   -- FB: 최신 댓글 created_time(ISO) 관측 워터마크
  my_user_id  text,                   -- FB: Page ID 캐시 (R-10 불일치 관측)
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fb_reply_cursor_pkey PRIMARY KEY (account)
);

CREATE TABLE IF NOT EXISTS public.fb_reply_budget (
  budget_date  date        NOT NULL,
  read_calls   integer     NOT NULL DEFAULT 0,
  write_calls  integer     NOT NULL DEFAULT 0,
  gemini_calls integer     NOT NULL DEFAULT 0,
  est_cost_krw numeric     NOT NULL DEFAULT 0,   -- Graph API 단가 없음 → 항상 0 (계약 호환)
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fb_reply_budget_pkey PRIMARY KEY (budget_date)
);

CREATE TABLE IF NOT EXISTS public.fb_reply_blacklist (
  author_id  text        NOT NULL,
  username   text,
  reason     text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fb_reply_blacklist_pkey PRIMARY KEY (author_id)
);

COMMENT ON TABLE  public.fb_reply_history IS 'FB-1 Facebook Page 댓글 자동답글 이력 (kr_reply_history 계약 미러)';
COMMENT ON COLUMN public.fb_reply_history.reply_tweet_id    IS 'Facebook comment id (공통 store 계약상 컬럼명 유지)';
COMMENT ON COLUMN public.fb_reply_history.response_tweet_id IS '내 Page가 단 답글 comment id';
COMMENT ON COLUMN public.fb_reply_history.conversation_id   IS 'Facebook post id (게시물 단위 일일 상한)';
COMMENT ON COLUMN public.fb_reply_history.author_id         IS 'Page 범위 사용자 ID (PSID). 실명은 저장하지 않음';

ALTER TABLE public.fb_reply_history   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fb_reply_cursor    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fb_reply_budget    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fb_reply_blacklist ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.fb_reply_history, public.fb_reply_cursor,
              public.fb_reply_budget, public.fb_reply_blacklist FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.fb_reply_history, public.fb_reply_cursor,
                                public.fb_reply_budget TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.fb_reply_blacklist TO service_role;

NOTIFY pgrst, 'reload schema';
