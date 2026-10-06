# Facebook Reply Engine (FB-1) — 설계·운영 계약

기준: 2026-10-07, main `352db47` 위 브랜치 `feat/fb-reply-engine`.
목적: X Reply Engine의 판단 로직을 공통 모듈로 재사용해 Facebook Page(Tiger18272) 게시물 댓글에 자동 호응 답글을 단다.

## 1. 결정사항 (이해관계자 5인 논의, X 운영 사고 이력 근거)

| # | 결정 | 근거 |
|---|---|---|
| D1 | 1단계 수집 대상은 피드 게시물만. 릴스는 Preflight로 실측 후 별도 단계 | 릴스 엔드포인트 문서 충돌, 미확인 경로 live 금지 |
| D2 | 판단·저장·필터·예산·생성·감사·리포트 공통화(키워드 인자 + X 기본값), 오케스트레이션은 FB 러너 분리 | 오케스트레이션 공통화 시 기존 테스트 patch 26건 거짓 통과 위험 |
| D3 | 프롬프트는 페르소나 1줄만 분기, X 프롬프트 sha256 골든 고정 | X 품질 재생 결과 보존 |
| D4 | 테스트는 tests/에 공동 배치, X·FB 전수 동시 실행 | 마스터 지시 — 공통 모듈 회귀를 양쪽에서 검증 |
| D5 | FB 상한 일 5 / 회 1 / 작성자 1 / 게시물 2, TTL 24h | 테스트 모드 앱, 정책 차단 코드(368) 존재 |
| D6 | 읽기 전용 Preflight 필수 → dry_run → shadow → live(confirm_live) | 일반 사용자 from 반환 미확정, R-10 교훈 |
| D7 | FACE_PAGE_ID·FACE_PAGE_TOKEN Secrets, 유료 Gemini·공개채널·X 키 미주입 | 과금 차단·채널 격리 |
| D8 | fb_reply_* 별도 테이블, 컬럼은 X 계약 미러 | store CAS/claim 100% 재사용, X 상한·예산 오염 방지 |
| D9 | 워크플로·concurrency(fb-reply-engine)·예산 분리, cron KST 11:38/14:38/20:38/23:38 | X(:17)·collect(:47)와 비중첩 |
| D10 | 최상위 댓글만 기본, 내 Page 댓글의 대댓글은 opt-in(회당 1), 제3자 간 대화 제외 | P-1 주객전도 사고 |

## 2. 구조

```
run_fb_reply.py                 FB 오케스트레이션 (Step 0~8, run_reply.py와 같은 계약)
reply_engine/facebook/config.py FACE_* 설정, FB_TABLES
reply_engine/facebook/client.py Graph v25.0 수집·발행·오류분류·BUC
reply_engine/facebook/normalize.py  Graph 댓글 → 공통 item
── 공통 (X·FB) ──
reply_engine/store.py     ReplyTables / tables= / bind()
reply_engine/filter.py    check_tweet(max_age_hours=) / CapLimits / repo= / caps=
reply_engine/budget.py    BudgetGuard(costs=, limit_krw=, fallback=, cost_labels=)
reply_engine/generator.py generate_batch(platform=)
reply_engine/config.py    is_enabled(var=) / get_mode(var=)
reply_engine/db_audit.py  audit_reply_db(required_contracts=, optional_contracts=, history_table=)
reply_engine/telemetry.py write_run_report() / journal_path
classifier · gate · lang · policy — 무수정 재사용
```

X 호출부는 인자를 넘기지 않으므로 X 동작·요청 테이블·프롬프트는 공통화 이전과 동일하다.

## 3. Graph API 사용 범위 (공식 문서 확인분만)

| 용도 | 엔드포인트 |
|---|---|
| 게시물 | `GET /{page-id}/feed?fields=id,message,created_time,from,is_published` |
| 댓글 | `GET /{post-id}/comments?filter=stream&order=reverse_chronological&fields=id,message,from,created_time,parent{id,from,message},can_comment` |
| 답글 | `POST /{comment-id}/comments` (message) |
| 사용률 | `X-Business-Use-Case-Usage` 헤더 — FACE_REPLY_BUC_STOP_PCT 이상이면 회차 중단(수집 단계면 분류·발행까지 중단, 알림) |

필요 권한(Permissions Reference 기준): `pages_read_engagement`, `pages_read_user_content`, `pages_manage_engagement`.
가이드에 보이는 `pages_read_user_engagement`는 Permissions Reference에 없는 명칭이라 사용하지 않는다.

오류 분류:

| 코드 | 분류 | 처리 |
|---|---|---|
| 4·17·32·341·613·80001 | THROTTLE | PUBLISH_RETRYABLE(보류 복구), 회차 중단, 알림 |
| 102·190·10·200~299 | AUTH | PUBLISH_RETRYABLE(미발행 확정 → 토큰 교체 후 TTL 내 복구), 회차 중단, 알림 |
| 368 | POLICY_BLOCK | PUBLISH_REJECTED, 회차 중단, 알림 |
| 1·2·타임아웃·비JSON·id 없는 성공 | UNKNOWN | PUBLISH_UNKNOWN — 재발행 금지 |
| 그 외 코드(100·506 등) | REJECTED | PUBLISH_REJECTED |

## 4. 처리 흐름

수집(피드 → 게시물별 댓글, TTL 밖 도달 시 중단) → 정규화·사전 스킵 → 같은 모드 기처리 제외 + live 보류 복구 큐 → 필터(범위·SELF·블랙리스트·만료·스팸·CANNOT_REPLY) → 분류 → 생성(platform=facebook) → 게이트·안전 후보 → 상한 예약 → 이력 저장 → 원자적 claim → Graph 발행 → 결과 저장 → 커서(관측용) 확정.

Graph comments에는 since_id가 없다. 따라서 신규 판정은 커서가 아니라 이력 기준으로 한다. 같은 모드로 이미 기록된 댓글은 신규에서 제외하고, live 보류 건은 X와 같은 `get_retryable_history`로만 재처리한다. 커서 `since_id`에는 최신 댓글 시각을 저장하며, 정체 관측용으로만 쓴다.

예외로, READY 고아 행(이력 저장 후 claim 전에 실행이 중단된 live 행)은 신규로 재평가한다. X의 커서 미전진 재수집과 같은 효과이며, 상태는 insert_history CAS가 보호한다.

복구 건도 현재 설정과 현재 데이터로 범위를 다시 판정한다. 대댓글 opt-in이 해제됐거나 from이 누락됐으면 스킵한다.

사전 스킵 사유: `AUTHOR_UNVERIFIED`(from 누락), `TIME_UNVERIFIED`(시각 파싱 불가), `OUT_OF_SCOPE_THREAD`(대댓글 opt-in off), `CANNOT_REPLY`(can_comment=false).
작성자 팔로워 수는 Graph에서 받을 수 없어 X의 SPAM_ACCOUNT 휴리스틱은 적용되지 않는다(user=None 경로).
실명은 저장하지 않는다(author_username = ""). 식별은 Page 범위 ID로 한다.

## 5. 운영 절차

1. `docs/sql/fb_reply_tables.sql`을 Supabase에 적용한다(멱등, kr_reply_* 무변경).
2. Secrets/Variables를 등록한다(아래 표).
3. `FB Reply Engine` 워크플로를 dispatch한다. `task=db_check` → 계약 OK 확인.
4. `task=preflight` 판정을 확인한다.
   - `READY_FOR_DRY_RUN`이면 dry_run으로 진행한다.
   - `NEEDS_REVIEW`(댓글 조회 오류, from 누락, 시각 형식)이면 앱 모드와 권한을 검토한다.
   - `BLOCKED`(자격증명, Page 불일치, 권한)이면 진행하지 않는다.
   - live 전환 조건은 `live_ready=true`다. 일반 사용자 댓글의 from을 실제로 관측한 경우만 해당한다.
5. `FACE_REPLY_ENABLED=true`, `FACE_REPLY_MODE=dry_run`으로 정기 실행하며 리포트를 검수한다.
6. shadow로 전환해 최소 3일 전수 검수한다.
7. live로 수동 dispatch(`confirm_live`)하고 회당 1건으로 시작한다.

| 구분 | 이름 | 기본 |
|---|---|---|
| Secret | FACE_PAGE_ID, FACE_PAGE_TOKEN | 필수 |
| Secret (재사용) | SUPABASE_URL, SUPABASE_REPLY_SERVICE_ROLE_KEY, GEMINI_API_KEY / _SUB_KEY / _SUB_SUB_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID | — |
| Variable | FACE_REPLY_ENABLED / FACE_REPLY_MODE | false / dry_run |
| Variable | FACE_REPLY_DAILY_CAP / _RUN_CAP(1~5) / _AUTHOR_DAILY_CAP / _POST_DAILY_CAP | 5 / 1 / 1 / 2 |
| Variable | FACE_REPLY_MAX_AGE_HOURS / _RETRY_WINDOW_HOURS | 24 / 24 |
| Variable | FACE_REPLY_THREAD_ENABLED / _THREAD_RUN_CAP | false / 1 |
| Variable | FACE_REPLY_POST_LOOKBACK / _COMMENT_MAX_PAGES | 10 / 2 |
| Variable | FACE_REPLY_READ_CALLS_PER_DAY / _WRITE_CALLS_PER_DAY / _BUC_STOP_PCT | 120 / 10 / 80 |
| Variable | FACE_REPLY_RELEASE_SHA | 승인 SHA 고정 운영 시 |

Supabase 키 주의: FB 테이블은 RLS가 켜져 있고 anon 권한이 회수되어 있다. 따라서 FB 워크플로는 `SUPABASE_REPLY_SERVICE_ROLE_KEY`만 사용하며 anon fallback이 없다.

## 6. 미확정 (Preflight 실측 항목)

다음 항목은 아직 확정되지 않았다:

- 개발 모드 앱에서 일반 사용자 댓글의 from이 반환되는지
- 대댓글의 대댓글이 실제로 붙는 위치
- 릴스가 /feed에 포함되는지, 릴스 댓글 조회 가능 여부
- comments 엣지의 since 지원 여부(엔진은 사용하지 않음)
- created_time 형식

`+0000`과 ISO 확장 형식은 모두 지원한다. 파싱에 실패하면 TIME_UNVERIFIED로 스킵한다.

## 7. 테스트

- tests/test_fb_reply_common.py (20): X 프롬프트 골든 sha256, 테이블 라우팅, 상한·예산·스위치 독립, db_audit, 리포트 경로
- tests/test_fb_reply_client.py (44): 시각 파싱, 응답 범위, 오류코드 분류 18종, 토큰 마스킹(POST/GET·paging URL), 수집 페이지·TTL·예산·BUC·스로틀 정지
- tests/test_fb_reply_pipeline.py (35): 실제 PostgREST SDK + Gemini 게이트웨이 기반 live/dry_run/shadow, 멱등, 결과 불명 재발행 금지·예약 유지, RUN_CAP 보류→복구, R-2, 스로틀/368/토큰/BUC 정지, 복구 건 범위 재판정, READY 고아 복구(live 한정)·미재수집 고아 감사 검출, EXHAUSTED, DB 확정 실패, claim 실패, 발송 전 만료, 스레드 상한, 중복 수집, 범위 규칙, X↔FB 교차 실행 격리
- tests/test_fb_reply_preflight.py (10): 판정 규칙(BLOCKED/NEEDS_REVIEW/READY, live_ready), 쓰기 0건, 토큰·실명 미노출

## 8. 범위 밖 발견 (이번 변경에서 수정하지 않음)

- `reply_summary.yml`이 기다리는 이름(`reply-engine`)과 실제 워크플로 이름(`X Reply Engine`)이 다르다. workflow_run 트리거가 동작하는지 Actions 이력으로 확인이 필요하다.
- `kr_reply_history/cursor/budget/blacklist`는 RLS가 꺼져 있고 anon·authenticated에 전체 권한(DELETE·TRUNCATE 포함)이 있다(2026-10-07 운영 DB 조회). anon 키는 클라이언트에 노출될 수 있는 키이므로 별도 보안 점검을 권고한다.
