-- ============================================================================
-- DN2026_0099 (2026-11-06) X Reply Engine 랜덤 간격 실행 상태 테이블 — 멱등 마이그레이션
-- ----------------------------------------------------------------------------
-- 용도: reply_engine.yml 10분 틱 게이트(scripts/reply_schedule_gate.py)가 다음 실행 시각을 보관한다.
--   decide = SELECT 후 RUN_DUE일 때만 선점 INSERT(행 없음) 또는 조건부 UPDATE(next_run_at 일치 시),
--   dispatch = UPSERT, report = SELECT. SKIP 판정은 쓰지 않는다. DELETE/TRUNCATE는 사용하지 않는다.
-- account 키: run_reply.py `_ACCOUNT = "kr_main"`과 동일 규칙 (모드 접미사 없음).
-- 권한: 기존 kr_reply_* 와 동일 권한 패턴 (anon / authenticated / service_role 전 권한 GRANT).
--       RLS 비활성은 기존 kr_reply_* 의 Known 상태를 그대로 따른 것이며, 이 파일에서 개선하지 않는다.
-- kr_reply_* 기존 테이블은 이 스크립트에서 일절 변경하지 않는다.
-- 실행은 메인(자비스)이 회장 승인 후 수행한다 (이 파일 작성 시점 미실행).
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.kr_reply_schedule (
  account        text        NOT NULL,
  next_run_at    timestamptz NOT NULL,
  last_tick_at   timestamptz,
  last_gap_min   numeric,
  last_decision  text,
  updated_at     timestamptz DEFAULT now(),
  CONSTRAINT kr_reply_schedule_pkey PRIMARY KEY (account)
);

COMMENT ON TABLE  public.kr_reply_schedule IS 'DN2026_0099 X Reply Engine 40~80분 랜덤 간격 실행 상태 (계정당 1행)';
COMMENT ON COLUMN public.kr_reply_schedule.next_run_at   IS '이 시각 이후 첫 10분 틱에서 RUN_DUE';
COMMENT ON COLUMN public.kr_reply_schedule.last_tick_at  IS 'RUN_DUE로 선점한 틱 시각 (gate decide 시각, UTC)';
COMMENT ON COLUMN public.kr_reply_schedule.last_gap_min  IS '직전 RUN_DUE 틱 간 실제 간격(분)';
COMMENT ON COLUMN public.kr_reply_schedule.last_decision IS 'RUN_DUE (decide 선점 시 기록값)';

ALTER TABLE public.kr_reply_schedule DISABLE ROW LEVEL SECURITY;

GRANT ALL ON public.kr_reply_schedule TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';
