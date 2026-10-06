# Reply v2.1 통합 테스트 결과

검증일: 2026-10-07 KST. 대상: PR #24.

## 결과

- 실제 파이프라인 및 PostgREST/Tweepy SDK를 사용한 오프라인 통합 테스트: 16개 통과.
- 전체 회귀 테스트: 722개 통과 (기존 719개 + 신규 통합 시나리오 3개).
- 변경 Python 파일 정적 검사와 git diff --check 통과.
- 변경 전 PR 커밋 3ef398f의 GitHub CI #7 성공 확인.

## 추가 검증

| 시나리오 | 검증 결과 |
| --- | --- |
| 수집 → RECEIVED 저장 → 커서 전진 → worker 복구 → 발행 기록 | 실발행 경로 1회, 수집기의 모델 호출·발행 0회 |
| 발행 완료 댓글 재수집 및 worker 재실행 | 기존 발행 이력 보존, 추가 발행 0회 |
| inbox 저장 실패 후 재실행 | 실패 시 커서 유지, 재실행 후 정상 발행 1회 |
| 수집된 댓글의 유효시간 만료 | EXPIRED_DEFERRED 확정, 모델 호출·발행 0회 |

기존 테스트는 발행 claim 단일 획득, 타임아웃 결과 불명 상태의 재발행 방지,
RUN_CAP 다음 실행 복구, dry_run DB 변경·발행 금지, 분류 재시도 상한도 검증한다.

## 발견 및 수정

처리 대상이 없어 조기 종료하는 경로에서 actual_published와 simulated 필드가
누락됐다. 실행 요약 초기값에 두 필드를 0으로 추가해 정상 종료와 동일한 계약을
유지했다. 만료 댓글 통합 시나리오가 해당 경로를 검증한다.

테스트용 DB HTTP 대역은 SDK의 bulk upsert columns 매개변수 및
resolution=ignore-duplicates를 처리하도록 보완했다. 이는 테스트 인프라 수정이며
운영 저장 로직 변경은 아니다.

## 검증 범위

HTTP 전송 경계와 모델 응답은 테스트 대역이다. 실제 운영 DB 권한·제약조건,
X 인증·요금·전송, 실제 모델 응답 품질을 검증한 운영 베타 테스트가 아니다.
운영 DB 쓰기, 실제 댓글·좋아요 발행, 설정 변경, main 병합은 수행하지 않았다.

재현: `python -m pytest -q tests/test_reply_sdk_integration.py`,
`python -m pytest -q tests/`.
