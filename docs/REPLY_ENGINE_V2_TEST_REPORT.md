# 자동댓글 v2 개발 및 단위테스트 결과

기준 저장소: yumens2-byte/investment-os-kr.
기준 main 커밋: d7d91d395304349623446a5a1ca82894a1f9cf7f.
개발 브랜치: feat/reply-engine-context-deferred-20261002.
검증일: 2026-10-02 KST.

## 검증 결과

| 검증 | 결과 |
|---|---|
| 변경 전 전체 pytest | 509 passed, 13.97초 |
| 신규 v2 회귀 테스트 | 60 passed, 0.37초 |
| 변경 후 전체 pytest | 569 passed, 13.32초 |
| Ruff: run_reply.py, reply_engine, gateway, 신규 테스트 | 통과 |
| git diff --check | 통과 |

전체 569건은 신규 60건과 기존 509건으로 구성된다. 기존 테스트 일부는 변경된 계약에 맞게 수정했다. 모든 단계의 스킵 이력 기록, 독립 shadow 커서, 안전 후보를 통한 초안 복구, 결과 불명 재발행 금지, guarded INSERT/PATCH가 주요 계약 변경이다. 단순 버전 문자열 검증도 해당 모듈 버전에 맞춰 갱신했다.

## 신규 테스트 범위

- 단일 반응 및 홍보가 아닌 광고/홍보 언급 허용, 실제 홍보 차단 유지.
- 혼합 의도·수사적 칭찬을 문맥 분류로 이관, 비라틴 언어 라우팅.
- 이모지 상한·줄바꿈·투자 행동 유도·근거 없는 수정 완료 선언 차단.
- 질문·게이트 탈락·DB 저장 실패가 뒤의 안전 댓글 상한을 선점하지 않음.
- 회당 상한 보류를 다음 실행에서 복구, 원 댓글 TTL·블랙리스트·직접 답글 범위 재검사.
- timeout 결과 불명 이후 재발행 금지, DB 성공 기록 실패 이후 차단 상태 유지.
- 발행 claim 실패 시 API 호출 금지, 조건부 claim 한 번만 성공.
- 429 재시도 횟수·대기 시각·TTL·최대 시도 검사, 재수집 시 횟수 초기화 금지.
- shadow가 live 커서를 사용하거나 live/완료 이력을 덮어쓰지 않음.
- 분류 JSON 중복/누락/요청 밖 ID를 신뢰하지 않음, 분류 장애 보류.
- 작성자 역할을 포함한 FOREIGN_DIRECT 프롬프트, 부모 작성자 미확인 시 보류.
- 실제 API 호출 수·토큰 반환 정보 집계, 댓글 처리의 유료 키 fallback 차단.
- 타인 스레드/언어/의도별 안전 문구, 링크 검토 opt-in 시 룰 분류 우회 금지.
- 루트 100 ID 배치 및 읽기 예산 제한, 결과 불명 발행 시도도 회당 상한에 포함.
- 실제 PostgREST SDK를 HTTP MockTransport와 결합해 claim PATCH 조건 직렬화 검증.
- audit에 미해결 발행 건 표시.

## 실행 방법

```bash
python -m pytest -q tests/
ruff check run_reply.py reply_engine core/gemini_gateway.py tests/test_reply_v2.py
git diff --check
```

개발 브랜치 push와 PR에 단위테스트 전용 CI를 추가했다. 이 CI는 운영 secrets를 전달하지 않고 모든 테스트를 수행한다. 로컬 실행에서 확인한 주요 버전은 pytest 9.1.1, ruff 0.16.10, tweepy 4.17.0, supabase/postgrest 2.31.0이다.

## 운영 적용 범위와 남은 검증

main 병합·운영 변수 변경·DB 쓰기·실제 X 답글 발행은 수행하지 않았다. 기존 스키마 안에서 보류 큐를 구현하여 DB migration은 없다.

단위테스트는 mock과 인메모리 저장소 및 SDK 직렬화 검증이다. 운영 Supabase에 대한 통합시험이나 실제 Gemini/X 호출 결과, 댓글 자연스러움 점수·오분류율·스킵 감소율을 입증하는 시험은 아니다. 실제 원문을 사용한 dry_run/replay와 수동 품질 검수가 다음 검증이다.

타인 스레드 및 링크 검토는 기본 비활성이다. 질문 자동답변·정정·새 계정 휴리스틱 해제는 후속 응답 범위로 남긴다. 기존 PUBLISH_FAIL과 새 UNKNOWN/PUBLISHING은 실발행 여부를 확인한 뒤 운영자가 처리해야 한다.

구체적인 처리 계약과 운영 절차는 [REPLY_ENGINE_V2.md](REPLY_ENGINE_V2.md)를 따른다.

## 후속 3인 리뷰·통합 검증·운영 반영

후속 독립 리뷰에서 발견된 문제를 수정한 최종 전체 테스트는 640건 통과(로컬 13.64초, GitHub PR CI 14.80초)했다. 실제 SDK 오프라인 통합 6건이 포함된다. PR #22를 main에 반영하고 운영 베타를 수행했다.

운영 베타는 성공했으나 신규 멘션 0건으로 실제 발행 검증은 남아 있다. 이후 결과는 [REPLY_ENGINE_V2_BETA_RESULT.md](REPLY_ENGINE_V2_BETA_RESULT.md), 리뷰 원인과 수정은 [REPLY_ENGINE_V2_REVIEW.md](REPLY_ENGINE_V2_REVIEW.md)를 따른다. 위 문서의 초기 569건 결과와 미반영 문구는 최초 개발 검증 시점의 기록이다.

## 잔여 작업 재검증

실제 댓글 재생·DB 좋아요 계약 검사를 추가한 전체 테스트는 655건 통과했다(로컬 13.93초, GitHub 15.05초). 실제 SDK 통합 7건, 품질 재생 13건, `liked_at` 누락 계약 회귀 1건을 포함한다. Ruff·diff 검사와 독립 AI 리뷰 3개를 통과했다.

[운영 후속 실행](https://github.com/yumens2-byte/investment-os-kr/actions/runs/36929310707)의 테스트 게이트가 이를 재검증했다. 실제 원문 재생·운영 DB 보완 결과는 [REPLY_ENGINE_V2_RESIDUAL.md](REPLY_ENGINE_V2_RESIDUAL.md)에 기록한다.

후속 DB 오류 코드·키 비노출 진단까지 포함한 최종 전체 660건이 통과했다(로컬 13.69초, GitHub 15.45초). 읽기 전용 운영 probe는 테스트를 통과한 후 실제 권한 오류 `42501`을 검출하여 실패로 종료했다. 이 실패는 운영 테이블 조회의 미해결 권한을 나타낸다.
