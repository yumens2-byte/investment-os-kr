# X Reply Engine 고도화 1차 구현·검증 기록

- 기준일: 2026-10-24 KST
- 대상: `yumens2-byte/investment-os-kr`
- 기준 main: `f52d3eca3f5da6d9165b318952e72d9ce8bdf692`
- 작업 브랜치: `feat/reply-engine-evidence-quality` (로컬)
- 상태: 아래 범위 구현·로컬 회귀검증 완료. 원격 push, PR 생성, Actions 실행, 운영 반영, 실제 발행, 운영 DB 변경 없음.

## 구현 범위

| 구분 | 변경 | 동작·제약 |
|---|---|---|
| P0 발행 요청 추적 | 요청별 UUID, 계정, 대상 댓글, 정확한 문구 SHA-256, claim 시각 보관 | 기존 CAS claim의 메타데이터에 포함. claim 실패 시 기존처럼 전송하지 않음 |
| P0 요청·결과 이벤트 | 실제 요청 직전 시각, attempt_id, 해시와 게시 응답 ID 연결 | 요청 이벤트는 전송 직전, 확인 이벤트는 ID 수신 후 기록. 결과 불명은 기존 차단 유지 |
| P0 게시 증거 검토 | 오프라인 `reply_evidence_review` | 계정·직접 답글 대상·본문 해시·시각·고유 응답 ID 일치 시 수동 검토 대상으로 제시. DB 확정·재발행 기능 없음 |
| P0 지표 | 관측 고유 댓글 작성률, 범위별 지표, 요청 확인율 | 적격=정적 필터·범위·분류 통과. 상한·게이트 탈락은 적격 분모에 유지. 자기 글·범위 밖 제외 |
| P0 다중 실행 집계 | 오프라인 `reply_cohort_report` | 계정·플랫폼·모드 분리, 댓글 ID·attempt_id 중복 제거. artifact 중복 입력으로 지표가 증가하지 않음 |
| P1 품질 | 축하 의도·fallback, 역할 검증 | `100화 축하드립니다`에 `축하합니다`를 반환하는 주체 반전 차단. 축하 수신 감사 후보를 기존 전체 게이트로 재검사 |
| P1 생성 문맥 | 계정·댓글 작성자·최근 발행 문구 최대 10개 전달 | 최근 문구는 생성 시 참고하며 기존 발행 전 유사도 게이트도 유지 |
| P1 외부 스레드 | 발행 전 부모 작성자 검증 추가, 범위별 관측 분리 | 외부 스레드 활성화 설정·회당 상한은 변경하지 않음 |

기존 의도별 fallback, 작성자·대화·예산 제한, 결과 불명 자동 재시도 차단은 재사용한다. 운영용 YAML을 추가하지 않았다. 기존 CI 파일에는 새 오프라인 도구의 정적 검사만 추가했다.

## 대사 도구 계약

입력은 운영자가 확보한 history JSON 배열과 X API tweet export JSON 배열이다. 이 도구는 자격증명을 읽거나 외부 API를 호출하지 않는다. export의 진위·수집 계정은 운영자가 확인해야 한다.

```bash
python -m scripts.reply_evidence_review \
  --history /path/history.json \
  --evidence /path/x_tweets.json \
  --account-id ACCOUNT_ID
```

history에는 `reply_tweet_id`, `mode`, `responded`, `skip_reason`, `response_text`, `error_message`를 전달한다. 메타데이터에는 `reply_policy`, `attempt_id`, `account_user_id`, `target_tweet_id`, `response_text_sha256`, `requested_at` 또는 `claimed_at`이 필요하다.

X tweet 증거에는 `id`, `author_id`, `text`, 타임존이 있는 `created_at`, `referenced_tweets`가 필요하다. `replied_to` 대상이 원 댓글과 일치해야 하며 quote는 인정하지 않는다. 시각은 요청 시각(없으면 claim 시각) 이후 1시간 이내만 인정한다. 이 시간창은 보수적인 검토 조건이며 X의 전송 SLA를 의미하지 않는다.

일치하면 `PUBLISHED_EVIDENCE_REVIEW_REQUIRED`와 응답 ID를 반환한다. 매칭 없음·정보 부족·상충 증거는 `UNRESOLVED`이며 모든 결과의 `may_retry=false`다. 입력 파일 해시도 출력해 검토 자료를 식별한다. 기존 실패 4건은 실제 증거를 입력하지 않았으므로 이번 작업으로 해소됐다고 판정하지 않는다. 과거 해시·시각 없는 이력은 수동 대사 대상으로 유지한다.

## 작성률·실행 집계

```bash
python -m scripts.reply_cohort_report \
  --account-id ACCOUNT_ID --mode live \
  /path/run1/reply_events.jsonl /path/run2/reply_events.jsonl
```

- `unique_eligible`: 입력 관측에서 정적·범위·분류를 통과한 고유 댓글 수.
- `unique_confirmed_eligible`: 그중 게시 응답 ID 수신이 확인된 댓글 수. DB 저장 실패여도 실제 ID가 반환된 건은 게시 확인에 포함한다.
- `eligible_publish_rate`: 위 두 수치의 비율. 분모 0이면 `null`.
- `requests`, `confirmed_requests`: attempt_id 기준 전송 요청과 응답 ID 확인. 결과 불명은 성공으로 간주하지 않는다.
- `unconfirmed_requests`: 응답 ID 확인이 없는 요청. 거절·결과 불명·로그 중단이 포함되므로 실제 미게시 수가 아니다.
- `OWN_ROOT`, `FOREIGN_DIRECT`, `FOREIGN_OTHER`, `UNVERIFIED`: 검증 근거에 따른 범위 분리. 외부 스레드 비활성화로 제외된 댓글도 부모 정보가 있으면 범위를 식별할 수 있다.

`ELIGIBLE`, `PUBLISH_REQUEST`, 결과 이벤트를 처리 시점에 기록한다. 종료 시점에는 본문 없는 `DECISION` snapshot도 누적한다. 종료 리포트가 누락된 중단 실행도 처리 시점 이벤트가 남았다면 집계할 수 있다.

이 지표는 **입력한 보존 관측의 비율**이다. artifact 누락·보존기간 만료·기존 형식의 이벤트가 존재하므로 전체 운영 코호트 작성률이나 개선 실측치로 표시하지 않는다. 기존 funnel 필드는 호환을 위해 유지한다. 기존 가변 DB history의 응답률과 새 관측 지표를 혼용하지 않는다.

## 검증

- 환경: Python 3.12.14, 저장소 `requirements-reply.lock` 설치.
- 기존 전체 테스트: 868개 통과.
- 신규 28개 회귀 테스트 추가 후 전체: **896 passed**.
- 변경 Python 모듈 및 새 CLI·테스트 `ruff check`: 통과.
- `git diff --check`: 통과.
- subprocess를 통한 두 CLI 실제 실행: 통과.
- 외부 서비스는 mock 사용. 실제 X·Gemini 요청, 운영 DB 접근, GitHub Actions 실행은 하지 않음.
- CI 정의의 Python 3.11에서 원격 검증한 결과는 아직 없음.

신규 검증은 정확한 게시 증거·계정/대상/본문/시각 불일치·quote 제외·증거 없음·상충·레거시 이력·축하 주체 반전·실제 fallback 발행 경로·결과 불명 메타데이터·계정/모드/플랫폼 분리·중복 artifact·종료 리포트 없는 중단 실행·CLI 계약을 포함한다.

공통 policy/gate/telemetry는 Facebook에서도 사용하는 모듈이다. 기존 Facebook 테스트와 기본 생성 프롬프트 보존 테스트도 전체 테스트에 포함돼 통과했다. 의도별 축하 fallback 규칙 자체는 공통 모듈 변경이므로 Facebook에도 적용될 수 있다.

## 후속 검증·미구현 범위

1. 기존 결과 불명 4건의 실제 게시 증거 확보·수동 대사. 이 작업은 게시 여부를 추정하지 않는다.
2. 전체 코호트의 영속 이벤트 저장·보존기간 및 완전성 관리. 이번 버전은 기존 artifact journal을 활용하며 새 DB 테이블·마이그레이션은 구현하지 않았다.
3. 외부 스레드 별도 shadow 표본 평가·질문/링크/애매함 오탐 평가. 운영 응답 범위를 확대하지 않았다.
4. 동일 댓글 표본의 수동 품질 검수, 작성률·품질 개선 실측. 단위 테스트 통과는 운영 효과 실측을 대체하지 않는다.
5. Gemini 키·모델 404 조합의 실행 내 재호출 억제 및 예정 스케줄 대 실제 시작 시각 연결은 후속 P2 작업이다.

이번 산출물은 고도화 전체 종료가 아닌 P0 관측·증거 검토 및 P1 품질 방어의 1차 구현이다. 운영 반영 전에는 위 범위·공통 모듈 영향과 실제 증거를 함께 검토해야 한다.
