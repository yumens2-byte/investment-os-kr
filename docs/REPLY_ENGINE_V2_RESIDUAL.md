# 자동댓글 v2 잔여 검증

사용자의 잔여 진행 승인에 따른 운영 후속 검증. 2026-10-02 KST.

## 좋아요 조회 오류 원인·수정

실제 운영 DB 조회에서 코드가 요구하는 `public.kr_reply_likes`가 없음을 확인했다.
별도 `kr_reply_like_history` 테이블은 존재하지만 0행이었다. 기존 답글 이력은 123행이며 변경하지 않았다.
서비스 키가 사용하는 서버용 테이블을 아래 계약으로 추가했다.

```sql
CREATE TABLE public.kr_reply_likes (
  reply_tweet_id text PRIMARY KEY,
  author_id text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('live', 'shadow')),
  would_like boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  liked_at timestamptz
);
CREATE INDEX kr_reply_likes_created_at_idx ON public.kr_reply_likes (created_at);
ALTER TABLE public.kr_reply_likes ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.kr_reply_likes FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT ON TABLE public.kr_reply_likes TO service_role;
```

Supabase 관리 마이그레이션 `reply_engine_likes_contract`, `reply_engine_likes_least_privilege` 적용.
SQL 검증: RLS=true, anon SELECT=false, authenticated INSERT=false, service_role SELECT/INSERT=true, UPDATE/DELETE=false.
service_role에서 live/shadow 2행 INSERT·SELECT 및 PK 중복 거부 검사 통과 후 ROLLBACK. 검사 데이터는 남기지 않았다.
사전 DB 계약 검사에 live 기록용 `liked_at`을 추가하고 해당 칼럼 누락 회귀 테스트를 추가했다.
보안 advisor의 새 테이블 관련 결과는 의도적인 서버 전용 RLS 정책 없음 INFO다. 공개 접근 권한은 없다.

좋아요 활성화 설정은 변경하지 않았다. 기존 shadow/live 좋아요 공유 집계 정책은 활성 전환 전 별도 검토가 필요하다.

## 실제 댓글 품질 재생

최근 실제 답글 이력을 최대 10건 읽고 X의 원문·부모 컨텍스트를 조회해 분류·생성·gate를 재생한다.
과거 댓글을 X에 다시 발행하지 않으며 좋아요·답글 이력·커서를 수정하지 않는다. 실제 API 사용량은 일일 예산에 기록한다.
유료 Gemini 키 fallback은 사용하지 않는다. 결과는 `logs/reply_quality_replay.json`으로 보존한다.
원문이나 부모 문맥을 조회하지 못한 댓글은 별도로 표시해 검증 완료로 계산하지 않는다.

검증 실행은 기존 베타 workflow에서 명시적인 `reply-engine-v2-residual-check` 커밋 표식과 이 문서 변경이 함께 있을 때만 트리거한다.
전체 테스트와 품질 재생의 분류·gate 통과 후 기존 dry_run·최대 1회 발행 live 베타를 실행한다. 의미·자연스러움은 재생 성공과 구분해 원문·부모·생성문을 직접 검토한다. 모델 초안 통과 여부와 템플릿 fallback도 별도 기록한다. 신규 적격 댓글이 없으면 실제 발행 미검증으로 기록한다.

독립 AI 리뷰 3개에서 DB 계약·권한, 답글 문맥·생성, 실제 SDK 통합을 재검토했다. 재생의 통과 답글 0건일 때 성공 처리하던 문제를 수정하고 회귀 검증했다. 로컬 전체 655건 통과(13.93초), 실제 SDK 통합 7건 포함. Ruff·diff 검사 통과.

## 실행 결과

[후속 운영 실행 36929310707](https://github.com/yumens2-byte/investment-os-kr/actions/runs/36929310707)은 성공했다. 운영 커밋은 `1f67ec8bbeb8725c31b8b087c9de0c32ff8059cb`, artifact ID는 11195352679다.

| 항목 | 실제 결과 |
|---|---|
| 전체 GitHub 테스트 | 655 passed / 15.05초, Ruff 통과 |
| 최근 7일 실발행 표본 | 6건 |
| 실제 X 원문·부모·대화 루트 검증 | 6건 |
| 최종 분류·gate 통과 | 6건 |
| AI 초안이 최종 통과 | 4건 |
| 정형 fallback 최종 통과 | 2건 |
| 실제 Gemini API | 무료 키 2회, 총 토큰 3047 |
| 재생 X 읽기 | 3회 |
| dry_run / live | 모두 EXIT_NO_MENTIONS |
| 실제 발행·좋아요 | 각각 0건 |
| 이력·커서 변경 | 없음 |
| 사전·사후 이력 감사 | 123행, 발행 불일치·중복·미완결 0건 |

이 재생+베타의 설정 단가 기준 추정 사용 증분은 250원(X 읽기 5회)이다. 실제 청구 금액이 아니다. 종료 시 당일 누계 read=10, write=0, gemini=2, estimate=500원이었다.

실제 문맥 6건을 직접 비교하고 품질 리뷰 에이전트가 독립 재검토했다. AI 응답 4건에서 투자 행동 유도·역할 반전·지어낸 완료 선언은 없었다. `가즈아!!!!`를 그대로 반복한 AI 초안은 GATE_ECHO로 차단됐다. 외국어 정형 문구가 이전 답글과 겹친 후보도 GATE_SIMILARITY로 대체됐다. 최종 정형 응답 2건은 안전하지만 자연스러움 개선의 여지가 있다. `관문 얘기가 현실적이군요.`도 다소 딱딱하다. 자동 보고서의 quality_verified=false는 의미 검수가 자동 gate의 증명이 아니라는 구분이며 그대로 유지한다.

**좋아요 테이블 APIError는 운영 API 감사에서 여전히 남았다.** 직접 DB의 존재·권한 검증과 런타임 API 조회는 별도다. 테이블 추가만으로 오류 해결을 선언하지 않고 스키마 캐시 재로드와 오류 코드·운영 연결 역할 진단을 진행한다. 진단 커밋 표식 `reply-engine-v2-schema-probe`는 X/Gemini/live 실행 없이 DB 읽기 검사만 수행한다.

신규 적격 댓글이 없어서 실제 X 발행 ID·발행 후 responded 저장 검증은 미완료다. 실제 신규 대상이 들어오는 운영 실행에서 확인해야 한다.

## 권한 오류의 최종 진단·필요 설정

[읽기 전용 운영 진단 36930363218](https://github.com/yumens2-byte/investment-os-kr/actions/runs/36930363218)의 전체 테스트는 660건 통과(15.45초)했다. DB probe는 실패 상태를 정확하게 반환했다. artifact ID 11195433653.

- 실제 SQLSTATE: `42501`(권한 거부).
- 운영 연결의 프로젝트 ref 일치: true.
- 키 유형: JWT, 선언된 역할: anon. JWT 진단은 서명을 인증한 결과가 아니지만 실제 API 권한 거부와 함께 원인을 구분하는 근거다.
- 필수 기존 테이블·123행 정합성은 정상, likes만 권한 거부.
- 이 프로브는 DB 읽기만 수행했다. X/Gemini/답글·좋아요 발행/예산·커서 변경 없음.

reply 본 실행·사전 검사·베타에서 전용 GitHub Secret `SUPABASE_REPLY_SERVICE_ROLE_KEY`를 우선 사용하도록 수정했다. 미등록 시 기존 `SUPABASE_KEY`를 사용하여 현재 답글 기능을 유지한다. **키를 등록하기 전에는 좋아요 API 오류가 해결되지 않는다.** 다른 파이프라인의 키 설정은 변경하지 않았다.

운영자가 [저장소 Actions Secrets](https://github.com/yumens2-byte/investment-os-kr/settings/secrets/actions)에 `SUPABASE_REPLY_SERVICE_ROLE_KEY`를 등록해야 한다. 값은 현재 운영 Supabase 프로젝트의 서버용 service_role 키다. 현재 연결 도구는 GitHub Secrets 쓰기를 지원하지 않아 여기서 등록하지 못했다. 키 원문을 보고서·로그·소스·대화에 기록하지 않는다.

등록 후 기존 X Reply Engine을 dry_run으로 실행해 DB 사전 검사 artifact의 `optional_schema_errors={}`와 정상 정합성을 확인한다. 이후 신규 적격 댓글에서 실제 X 발행·response ID 저장을 확인한다. 좋아요 활성화는 이번 변경에 포함되지 않는다.

완료: 코드 수정, 3개 독립 AI 리뷰, 전체 660개 테스트, 실제 댓글 6건 생성·gate·직접 의미 검토, 운영 반영·재생·dry/live 베타, 오류 원인 확정. 미완료: 서버 키 직접 등록 및 그 키의 실제 API 조회 검증, 신규 적격 댓글 실발행 검증, 운영 표본 누적에 따른 스킵 감소율.


## 기존 키 권한 부여 후 재검증 — 2026-10-02 10:23 KST

사용자는 기존 SUPABASE_KEY 유지와 Supabase 권한 직접 부여를 선택했다. 직접 DB 검사에서 anon SELECT=true, INSERT=true가 확인됐다. RLS=true, 정책 0개다. 서버 키 등록은 기존 키 사용 선택에 따라 필수 후속 작업으로 두지 않는다. 앞선 서버 키 등록 안내는 이전 해결안의 기록이다.

허용 RLS 정책이 없는 상태이므로 GRANT만으로 좋아요 이력 등록이 완성되지는 않는다. 운영 API의 읽기 전용 probe를 다시 실행하여 테이블 권한 오류 해소와 실제 등록 가능 여부를 구분한다. 좋아요 활성화 설정은 변경하지 않는다.

anon 역할의 트랜잭션 내 INSERT 검사에서 SQLSTATE 42501 / new row violates row-level security policy로 거부됨을 확인했다. 검사 행은 저장되지 않았다.

[권한 부여 후 운영 읽기 검증 36950864167](https://github.com/yumens2-byte/investment-os-kr/actions/runs/36950864167): 성공. 전체 660건 테스트 통과(15.50초), 운영 anon 키 그대로 사용, 연결 프로젝트 일치, schema_errors/optional_schema_errors/error_codes 모두 빈 값, 123행 이력 정합성 정상. 좋아요 테이블 조회의 기존 권한 거부는 해소됐다. 이 검사는 SELECT 계약만 확인하므로 RLS INSERT 차단이 해결됐다는 의미는 아니다. 직접 등록 검사에서는 RLS 거부를 확인했고, 검사 행 0건·테이블 총 0행을 재확인했다.

현재 잔여: 기존 anon 키로 좋아요 이력을 등록하려면 접근 범위를 정한 RLS 정책 또는 별도 인증이 필요하다. 좋아요 활성화를 완료로 판단하지 않는다. 신규 댓글 실제 발행과 스킵 감소율 측정도 남아 있다.


## #166 수정 후 운영 베타 승인 — 2026-10-05 KST

사용자가 운영 반영 후 운영 베타 테스트 진행을 명시 승인했다. PR #23 병합 커밋 f874ef1907ab6456b49c4eff14dd896212831e53을 대상으로 기존 베타 절차를 실행한다. 전체 테스트 → DB 사전 감사 → dry_run → 사용량 기록 → live 최대 1건 → 사후 감사. 좋아요는 비활성화한다. 신규 적격 댓글이 없으면 실제 발행 미검증으로 구분한다. 실행 결과는 후속 기록한다.
