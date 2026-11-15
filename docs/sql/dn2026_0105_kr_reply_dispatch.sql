-- ═════════════════════════════════════════════════════════════════════
-- DN2026_0105 X Reply Engine DB 외부 트리거 (Supabase pg_cron → GitHub workflow_dispatch)
-- 대상: Supabase ccomoimhhttaklfadaos (pg_cron 1.6.4 · pg_net 0.20.0 · supabase_vault 0.3.1)
-- 선례: private.ia_dispatch_alert / private.ia_dispatch_reconcile (investment-alert, 14일 100/100 ok)
--
-- 왜: GitHub schedule 이벤트가 하루 144틱 중 약 4~5회만 발화 → late_min 중앙값 261분 (11-13~15 실측).
--     pg_cron 10분 job은 3일 432/432 성공.
-- 무엇: 10분마다 kr_reply_schedule.next_run_at 도래를 DB에서 판정하고, 도래했을 때만
--       reply_engine.yml을 workflow_dispatch(inputs.source=scheduler)로 호출한다.
--       실행 판정·선점(CAS)은 GitHub gate job이 그대로 한다 (이 함수는 '틱 공급원'일 뿐).
-- 모드: private.kr_reply_dispatch_config.mode = off | dry_run | live  (기본 dry_run = 호출 없이 기록만)
-- 시크릿(Vault):
--   github_reply_dispatch_pat  — fine-grained PAT, 레포 investment-os-kr 한정, Actions: Read and write
--   kr_tg_bot_token / kr_tg_alert_chat_id (없으면 ia_tg_bot_token / ia_tg_internal_chat_id 사용)
-- 멱등: 여러 번 실행해도 같은 결과 (IF NOT EXISTS / CREATE OR REPLACE / cron.schedule 이름 upsert)
-- 롤백: 파일 끝 [ROLLBACK] 블록
-- ═════════════════════════════════════════════════════════════════════

create schema if not exists private;

create table if not exists private.kr_reply_dispatch_config (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);
insert into private.kr_reply_dispatch_config (key, value)
values ('mode', 'dry_run')
on conflict (key) do nothing;

create table if not exists private.kr_reply_dispatch_log (
    id               bigserial primary key,
    requested_at     timestamptz not null default now(),
    mode             text not null,
    -- would_dispatch | requested | ok | failed | lost | error | no_secret
    status           text not null,
    next_run_at      timestamptz,
    late_min         numeric,
    request_id       bigint,
    http_status      integer,
    response_excerpt text,
    error            text,
    reconciled_at    timestamptz,
    notified_at      timestamptz
);
create index if not exists kr_reply_dispatch_log_requested_at_idx
    on private.kr_reply_dispatch_log (requested_at desc);
create index if not exists kr_reply_dispatch_log_open_idx
    on private.kr_reply_dispatch_log (status) where reconciled_at is null;

revoke all on private.kr_reply_dispatch_config from public, anon, authenticated;
revoke all on private.kr_reply_dispatch_log from public, anon, authenticated;


-- ── 틱: 도래 판정 → (모드별) GitHub 호출 ──────────────────────────────
-- 반환값: off | skipped_night | skipped_not_due | skipped_recent | would_dispatch
--         | requested | no_secret | error
create or replace function private.kr_reply_dispatch(p_now timestamptz default now())
returns text
language plpgsql
security definer
set search_path to ''
as $function$
declare
    v_mode text;
    v_next timestamptz;
    v_pat  text;
    v_req  bigint;
    v_late numeric;
begin
    select c.value into v_mode from private.kr_reply_dispatch_config c where c.key = 'mode';
    v_mode := coalesce(lower(trim(v_mode)), 'dry_run');
    if v_mode not in ('off', 'dry_run', 'live') then
        v_mode := 'dry_run';                                  -- 오입력은 호출 없는 쪽으로
    end if;
    if v_mode = 'off' then
        return 'off';
    end if;

    -- KST 03:00~03:59 실행 없음 (reply_engine/schedule.py NIGHT_HOUR_KST와 동일)
    if extract(hour from (p_now at time zone 'Asia/Seoul')) = 3 then
        return 'skipped_night';
    end if;

    select s.next_run_at into v_next
      from public.kr_reply_schedule s
     where s.account = 'kr_main';
    if v_next is not null and v_next > p_now then
        return 'skipped_not_due';
    end if;

    -- 직전 호출 후 15분 이내면 생략 (큐 대기·실행 중인 run이 next_run_at을 갱신할 시간)
    if exists (
        select 1 from private.kr_reply_dispatch_log l
         where l.status in ('would_dispatch', 'requested', 'ok')
           and l.requested_at > p_now - interval '15 minutes'
    ) then
        return 'skipped_recent';
    end if;

    v_late := case when v_next is null then null
                   else round(extract(epoch from (p_now - v_next)) / 60.0, 1) end;

    if v_mode = 'dry_run' then
        insert into private.kr_reply_dispatch_log (requested_at, mode, status, next_run_at, late_min)
        values (p_now, v_mode, 'would_dispatch', v_next, v_late);
        return 'would_dispatch';
    end if;

    select ds.decrypted_secret into v_pat
      from vault.decrypted_secrets ds
     where ds.name = 'github_reply_dispatch_pat'
     limit 1;
    if v_pat is null or length(trim(v_pat)) = 0 then
        -- 시크릿 부재 경보는 1시간에 1건만 남긴다 (10분 틱마다 쌓이지 않게)
        if not exists (
            select 1 from private.kr_reply_dispatch_log l
             where l.status = 'no_secret' and l.requested_at > p_now - interval '1 hour'
        ) then
            insert into private.kr_reply_dispatch_log (requested_at, mode, status, next_run_at, error)
            values (p_now, v_mode, 'no_secret', v_next, 'vault secret github_reply_dispatch_pat 없음');
        end if;
        return 'no_secret';
    end if;

    v_req := net.http_post(
        url := 'https://api.github.com/repos/yumens2-byte/investment-os-kr/actions/workflows/reply_engine.yml/dispatches',
        body := jsonb_build_object(
            'ref', 'main',
            'inputs', jsonb_build_object('source', 'scheduler')),
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' || trim(v_pat),
            'Accept', 'application/vnd.github+json',
            'X-GitHub-Api-Version', '2022-11-28',
            'User-Agent', 'investment-os-kr-reply-scheduler',
            'Content-Type', 'application/json'),
        timeout_milliseconds := 10000);

    insert into private.kr_reply_dispatch_log
        (requested_at, mode, status, next_run_at, late_min, request_id)
    values (p_now, v_mode, 'requested', v_next, v_late, v_req);
    return 'requested';
exception when others then
    insert into private.kr_reply_dispatch_log (requested_at, mode, status, error)
    values (p_now, coalesce(v_mode, 'unknown'), 'error', left(sqlerrm, 300));
    return 'error';
end;
$function$;


-- ── 정산: 응답 반영 → 실패 묶음 경보 ────────────────────────────────
create or replace function private.kr_reply_dispatch_reconcile()
returns integer
language plpgsql
security definer
set search_path to ''
as $function$
declare
    v_bot  text;
    v_chat text;
    v_fail integer;
    v_msg  text;
begin
    -- (a) 응답 정산: 2xx = ok, 그 외/timeout/오류 = failed
    update private.kr_reply_dispatch_log l
       set http_status      = r.status_code,
           response_excerpt = left(coalesce(r.content, r.error_msg, ''), 300),
           status           = case
                                when r.status_code between 200 and 299
                                 and not coalesce(r.timed_out, false) then 'ok'
                                else 'failed'
                              end,
           error            = case when coalesce(r.timed_out, false) then 'timed_out'
                                   else left(r.error_msg, 300) end,
           reconciled_at    = now()
      from net._http_response r
     where r.id = l.request_id
       and l.reconciled_at is null
       and l.status = 'requested';

    -- (b) pg_net 응답 TTL(6시간) 경과 미정산 = lost
    update private.kr_reply_dispatch_log
       set status = 'lost', reconciled_at = now()
     where status = 'requested'
       and reconciled_at is null
       and requested_at < now() - interval '6 hours';

    -- (c) 미통지 실패 묶음 경보
    select count(*) into v_fail
      from private.kr_reply_dispatch_log
     where status in ('failed', 'lost', 'error', 'no_secret')
       and notified_at is null;
    if v_fail = 0 then
        return 0;
    end if;

    select ds.decrypted_secret into v_bot  from vault.decrypted_secrets ds
     where ds.name = 'kr_tg_bot_token' limit 1;
    select ds.decrypted_secret into v_chat from vault.decrypted_secrets ds
     where ds.name = 'kr_tg_alert_chat_id' limit 1;
    if v_bot is null or v_chat is null then
        select ds.decrypted_secret into v_bot  from vault.decrypted_secrets ds
         where ds.name = 'ia_tg_bot_token' limit 1;
        select ds.decrypted_secret into v_chat from vault.decrypted_secrets ds
         where ds.name = 'ia_tg_internal_chat_id' limit 1;
    end if;
    if v_bot is null or v_chat is null then
        return v_fail;                          -- 경보 수단 없음: 로그에만 남김
    end if;

    select '[kr-reply-dispatch] 실패 ' || count(*) || '건: '
           || string_agg(status || coalesce('(' || http_status || ')', '')
                         || ' ' || to_char(requested_at at time zone 'UTC', 'MM-DD HH24:MI') || 'Z',
                         ', ' order by requested_at)
      into v_msg
      from private.kr_reply_dispatch_log
     where status in ('failed', 'lost', 'error', 'no_secret')
       and notified_at is null;

    perform net.http_post(
        url := 'https://api.telegram.org/bot' || trim(v_bot) || '/sendMessage',
        body := jsonb_build_object('chat_id', trim(v_chat), 'text', left(v_msg, 3500)),
        timeout_milliseconds := 10000);

    update private.kr_reply_dispatch_log
       set notified_at = now()
     where status in ('failed', 'lost', 'error', 'no_secret')
       and notified_at is null;
    return v_fail;
end;
$function$;

revoke all on function private.kr_reply_dispatch(timestamptz) from public, anon, authenticated;
revoke all on function private.kr_reply_dispatch_reconcile() from public, anon, authenticated;


-- ── 스케줄 (이름 upsert) ─────────────────────────────────────────
-- :04/:14/…/:54 틱, 정산은 :07/:17/…/:57 (ia_alert_dispatch_reconcile */10 과 분 단위로 비켜 둠)
select cron.schedule('kr_reply_dispatch', '4-59/10 * * * *',
                     $$select private.kr_reply_dispatch()$$);
select cron.schedule('kr_reply_dispatch_reconcile', '7-59/10 * * * *',
                     $$select private.kr_reply_dispatch_reconcile()$$);

notify pgrst, 'reload schema';


-- ── 단계 전환 (운영자 수동) ──────────────────────────────────────
-- P1 기록만:   update private.kr_reply_dispatch_config set value='dry_run', updated_at=now() where key='mode';
-- P2 실호출:   update private.kr_reply_dispatch_config set value='live',    updated_at=now() where key='mode';
-- 즉시 중지:   update private.kr_reply_dispatch_config set value='off',     updated_at=now() where key='mode';
-- 점검:
--   select status, count(*), round(avg(late_min),1) avg_late, max(requested_at)
--     from private.kr_reply_dispatch_log where requested_at > now() - interval '1 day' group by 1;

-- [ROLLBACK]
-- select cron.unschedule('kr_reply_dispatch');
-- select cron.unschedule('kr_reply_dispatch_reconcile');
-- drop function if exists private.kr_reply_dispatch(timestamptz);
-- drop function if exists private.kr_reply_dispatch_reconcile();
-- drop table if exists private.kr_reply_dispatch_log;
-- drop table if exists private.kr_reply_dispatch_config;
