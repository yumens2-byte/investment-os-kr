# X Reply Engine 고도화 통합테스트 결과

- 일자: 2026-10-24 KST
- 구현 기준: 로컬 커밋 `175fb25`
- 저장소: `yumens2-byte/investment-os-kr`
- 결과: 신규 통합 시나리오 **7개 통과**, 전체 회귀 **903개 통과**.
- 환경: Python 3.12.14, 저장소 `requirements-reply.lock` 사용.

## 검증 방법

실제 `run_reply.main`, 분류·생성·게이트·store·telemetry·대사·집계 코드를 연결했다. 실제 Tweepy 및 PostgREST SDK를 사용하며 X HTTP 응답·DB HTTP 응답·모델 응답 경계는 로컬 상태 기반 fixture로 대체했다. 신규 통합테스트는 socket 연결을 실패시키므로 외부 네트워크 연결 시 테스트가 실패한다. 운영 계정의 자격증명 대신 fixture의 가짜 값을 사용했다.

| 시나리오 | 검증 결과 |
|---|---|
| 정상 발행 → claim → 요청/확인 이벤트 → JSON 리포트 → 다중 실행 집계 | attempt_id·문구 해시·응답 ID 연결 확인. 동일 journal 중복 입력에도 요청·성공 각 1건 유지 |
| 전송 타임아웃 → PUBLISH_UNKNOWN → 게시 증거 검토 → 다음 실행 | 계정·대상·해시·시각이 일치하면 검토 대상 반환. 대사 후에도 DB 불변. 두 번째 실행 포함 총 전송 1회로 재발행 차단 |
| shadow 모드 | 실제 전송 0회, 요청 증거 0건, live 집계와 분리 |
| dry_run 모드 | 실제 전송 0회, DB POST/PATCH/DELETE 0회, live 집계와 분리 |
| X 발행 성공 → DB 확인 저장 HTTP 503 | 실제 store 저장 재시도 경로 수행. PUBLISHED_DB_UNCONFIRMED 기록, 응답 ID 기준 전송 확인율 1.0, DB 상태는 PUBLISHING 유지 |
| 축하 댓글 → 역할이 반전된 모델 초안 → fallback → 발행 | GATE_ROLE_REVERSAL 차단 후 축하 수신 감사 풀로 교체, 1회 발행 |
| 최종 JSON 리포트가 작성되지 않는 경로 | 처리 시점 이벤트만으로 적격 1건·발행 확인 1건 집계 |

최종 리포트 누락 시나리오는 `_write_report`를 생략해 검증했으며 OS 강제 종료·디스크 유실·전원 장애를 재현한 테스트는 아니다. DB fixture는 PostgREST 요청과 주요 필터를 검증하지만 실제 PostgreSQL의 락·트랜잭션 격리를 대체하지 않는다.

## 실행 결과

```text
python -m pytest -q tests/test_reply_upgrade_integration.py
7 passed

python -m pytest -q tests/ --junitxml=reply-integration-results.xml
903 passed in 2.66s

ruff check tests/test_reply_upgrade_integration.py
All checks passed!
```

기존 896개와 신규 7개를 함께 실행했다. 기존 Facebook 공통 모듈 회귀 및 SDK 통합테스트도 전체 실행에 포함된다. 신규 테스트 파일은 기존 CI의 정적 검사 대상에도 추가했다. 애플리케이션 구현 코드의 추가 수정은 없었다.

증거: `tests/test_reply_upgrade_integration.py`, `evidence/tests/reply_upgrade_integration_20261024.xml` (전체 JUnit 결과에서 신규 7개 testcase를 추출한 파일).

## 판정 및 한계

로컬 오프라인 통합검증은 통과했다. GitHub push·원격 Actions 실행·운영 반영·실제 X 발행·운영 DB 변경은 하지 않았다. CI의 Python 3.11 환경 및 실제 X/Gemini/Supabase 서비스 연결 검증은 미수행이다.

기존 결과 불명 4건의 실제 게시 대사, 전체 코호트 영속 저장, 외부 스레드 shadow 평가와 실제 품질·작성률 개선 실측은 이번 통합테스트로 완료됐다고 판단하지 않는다.
