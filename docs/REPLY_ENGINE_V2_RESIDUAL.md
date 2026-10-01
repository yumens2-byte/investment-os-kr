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

실행 전. 실제 결과와 실행 링크는 완료 후 갱신한다.
