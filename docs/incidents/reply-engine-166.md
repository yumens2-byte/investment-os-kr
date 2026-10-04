# X Reply Engine #166 — 분석·설계·검증

## 범위와 증거
- 대상: yumens2-byte/investment-os-kr, 기준 커밋 13a14e3b8702af959c139605ffed6d7c6ca99e64.
- 실패 실행: https://github.com/yumens2-byte/investment-os-kr/actions/runs/37210619967
- #166 Run Tests 실패, Reply Pipeline skipped. 원본 로그: 1 failed, 659 passed.
- 원본 설치 로그: supabase/postgrest 2.32.0. 테스트는 eq.False를 기대하지만 실제 요청은 eq.false.
- 동일 SHA에서 #160–162 성공, #163–166 실패가 조회됨. 이전 성공 실행의 의존성 버전은 대조하지 않았으므로 정확한 버전 변경 시점은 미확정.
- 로컬 수정 전 동일 테스트 실패 재현.

## 원인과 설계
운영 claim_publication은 .eq("responded", False)를 사용한다. SDK 2.32.0의 실제 불리언 직렬화와 테스트의 문자열 기대값이 달라 테스트 게이트가 실패했다. requirements.txt의 supabase>=2.0.0은 설치 시 SDK 버전 변경을 허용한다.

1. 기대값을 eq.false로 수정하되 PATCH 메서드, 모든 선점 필터 및 PUBLISHING 본문 검증은 유지한다.
2. 검증 대상 Supabase SDK를 2.32.0으로 고정한다. 해당 패키지가 PostgREST 및 Supabase 동반 SDK 버전을 고정한다. 전체 전이 의존성을 잠근 lockfile은 아니며 다른 라이브러리의 버전 변동 가능성은 남는다.
3. 실제 PostgREST SDK와 상태형 HTTP fixture로 비대상 행 5종의 선점 거부 및 동일 행 재선점 거부를 추가 검증한다.
4. 운영 발행 코드, DB 스키마, 권한, 시크릿, QC 및 알림 정책은 변경하지 않는다.

## 테스트와 한계
- Python 3.11.16: 전체 tests/ 666 passed in 17.39s. 기존 Reply CI 정적 검사 All checks passed. 의존성 호환성 검사 통과.
- 추가 6건: dry_run 행, 응답 완료, response_tweet_id 존재, PUBLISHING, PUBLISH_UNKNOWN의 변경 거부; 정상 선점 후 두 번째 선점 거부.
- 기존 실제 SDK 오프라인 통합 테스트: 정상 발행/저장/커서, 타임아웃 재발행 방지, 호출 간 한도, dry_run DB 무변경 및 X 무발행 등.
- HTTP 경계는 모의 서버로 대체한다. 실제 PostgreSQL 동시성, 운영 RLS, X 권한/요금 및 실제 발행 성공을 증명하지 않는다.
- 운영 live/beta 워크플로 재실행은 하지 않는다. PR CI는 운영 자격증명 없이 검증한다.

## 운영 반영 절차
PR CI 통과와 변경 파일 검토 후 사용자 승인으로 main에 병합한다. 이후 별도 승인된 운영 점검으로 실제 연동 상태를 확인한다. 본 작업에서는 병합 및 운영 발행을 하지 않는다. 되돌릴 경우 이 수정 커밋을 revert할 수 있지만 원래 테스트 실패가 재발할 수 있다.
