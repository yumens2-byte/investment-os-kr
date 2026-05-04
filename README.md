# investment-os-kr

## CI / 배포 전 점검 (고도화 분석설계)

현재 기준(2026-05-04)으로 배포 안정성 관점에서 테스트 체계를 분석하고,
5명 관점 병렬 검토 브레인스토밍 결과를 아래와 같이 정리합니다.

### 1) 현재 상태 진단

- 장점
  - 도메인 로직 단위 테스트가 이미 존재(엔진/포매터/리트라이/클라이언트 유틸).
  - `tests/test_ci_smoke.py`로 엔트리포인트(`run_market.main`) DRY_RUN 스모크 검증이 가능.
- 공백
  - GitHub Actions 워크플로우에 테스트 실행 단계가 없음(배포 Job 전에 품질 게이트 부재).
  - 외부 연동 실패/타임아웃/빈 데이터에 대한 회귀 검증이 엔트리포인트 수준에서 부족.
  - 테스트 계층(단위/계약/통합/배포 스모크) 구분과 실행 정책이 문서화되지 않음.

### 2) 병렬 검토(5인) 브레인스토밍

#### Reviewer A — CI 파이프라인 엔지니어
- 제안: 워크플로우를 `test` → `deploy` 2단계로 분리하고, `deploy`는 `needs: test`로 보호.
- 포인트: 최소 게이트는 `pytest -q tests/test_ci_smoke.py`; 권장 게이트는 `pytest -q`.

#### Reviewer B — 백엔드/런타임 안정성
- 제안: `main()`에서 단계별 실패를 구조화된 상태값으로 기록(예: `step_status`).
- 포인트: 단일 예외 메시지보다 “어느 단계에서 실패했는지”를 기계적으로 추적 가능해야 함.

#### Reviewer C — 데이터 품질/정합성
- 제안: 수집 결과 스키마 검증 테스트 추가(필수 키, 타입, 허용 범위).
- 포인트: 발행 게이트를 “None 체크”에서 “품질 점수 기반 체크”로 확장.

#### Reviewer D — QA 자동화
- 제안: pytest marker 체계 도입 (`unit`, `smoke`, `integration`, `contract`).
- 포인트: PR에서는 `unit+smoke`, nightly에서는 `integration`까지 확장 실행.

#### Reviewer E — 운영/SRE
- 제안: 실패 시 알림 메시지 템플릿 표준화(실패 단계, 영향도, 복구 가이드).
- 포인트: 테스트 성공/실패를 운영 관측 지표와 연결(예: 실행시간, 실패율, flaky 추적).

### 3) 고도화 아이템 Top 5 (우선순위)

1. **워크플로우 테스트 게이트 강제**  
   - `kr_daily.yml`에 테스트 단계 추가, 테스트 실패 시 배포 차단.
2. **테스트 계층/마커 체계화**  
   - 실행 정책을 PR/스케줄 배포/야간 검증으로 분리.
3. **데이터 계약(Contract) 테스트 도입**  
   - 수집/분석/포맷 산출물의 필수 키/타입/범위 검증.
4. **실패 주입(Failure Injection) 시나리오 테스트**  
   - API timeout, 빈 응답, 부분 누락 데이터에 대한 회복 동작 검증.
5. **문서·운영 연계 강화**  
   - 장애 대응 Runbook 및 실패 알림 포맷을 README에 연결.

### 4) 실행 로드맵 (2주)

- Week 1
  - 워크플로우에 `pytest -q tests/test_ci_smoke.py` 필수화
  - marker 도입 + PR 기본 게이트 확정
- Week 2
  - 계약 테스트/실패 주입 테스트 추가
  - 운영 알림 템플릿/Runbook 문서화

### 5) 권장 실행 명령

- 전체: `pytest -q`
- 배포 스모크: `pytest -q tests/test_ci_smoke.py`
- (향후) 마커 기반: `pytest -q -m "unit or smoke"`

## CI / 배포 전 점검

GitHub Actions 배포 안정성을 위해 테스트 모듈을 운영합니다.

- 전체 테스트: `pytest -q`
- CI 스모크 테스트(배포 진입점): `pytest -q tests/test_ci_smoke.py`

`tests/test_ci_smoke.py`는 외부 API/X/TG/Supabase를 실제 호출하지 않고 `run_market.main()` 경로를 검증해,
배포 시점의 구조적 회귀(엔트리포인트 실패, 스냅샷 키 누락 등)를 조기 탐지합니다.
