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

## 라이트 버전 고도화 설계 (미장 7 : 국장 3)

현재 구조(트윗/쓰레드 생성 파이프라인)를 유지하면서, 미니멀 운용을 전제로 아래 5인 상세설계 관점으로 보완했습니다.

### 1) 전략 설계 (PM/포트폴리오)
- 운용 기준을 `Mini Flow 7:3`으로 고정: **미장 70 / 국장 30**.
- 일일 콘텐츠에서 “관측(지표) → 판단(시그널) → 행동(비중 유지/미세조정)” 순서를 강제.
- 목표: 정보 과잉 없이 동일 포맷 반복으로 의사결정 피로 최소화.

### 2) 데이터 설계 (데이터 엔지니어)
- 기존 KR 지표 수집은 유지하고, 쓰레드 2번 트윗에 운용 프레임을 명시해 해석 기준 통일.
- 결측치 발생 시 기존 발행 게이트로 차단(안전 우선), 스냅샷 저장은 계속.
- 향후 확장: 미장 70 비중 판단 근거를 위해 S&P500/Nasdaq/VIX를 contract test와 함께 점진 추가.

### 3) 콘텐츠 설계 (콘텐츠 에디터)
- 쓰레드 2번 트윗에 운용 프레임 문구를 고정 삽입:
  - `운용 프레임: Mini Flow 7:3 (미장 70 : 국장 30)`
- 의미: 당일 시그널이 강해도 전략의 기준축(7:3)을 먼저 상기하도록 유도.
- 해시태그/시그널 구조는 유지해 기존 팔로워 학습비용 최소화.

### 4) 리스크/보안 설계 (보안 담당)
- 비밀키/토큰 비노출 정책 유지(환경변수 점검 시 값 출력 금지).
- 필수 데이터 누락 시 외부 발행 차단 정책 유지(오탐 발행 방지).
- 운영 보완 권장:
  - 키 로테이션 주기 정책화(월 1회)
  - 발행 계정 권한 최소화 및 채널별 토큰 분리
  - 실패 로그에 개인식별/민감정보 포함 금지 점검 자동화

### 5) 운영/검증 설계 (QA/SRE)
- formatter 테스트에 운용 프레임 문구 검증을 추가해 회귀 방지.
- 목표 SLO(권장):
  - 평일 장전 발행 성공률 99%+
  - 결측치 발행 0건
  - 쓰레드 포맷 일관성 100%
 
- # KR Market OS — 라이트 OS 고도화 v1.4.0

**작업일**: 2026-05-05  
**작업 범위**: investment-os-kr (라이트 OS) 3가지 기능 추가  
**테스트 결과**: 78/78 PASS  
**산출물 형식**: 통째로 교체 또는 신규 추가 가능한 파일 모음

---

## 🎯 변경 개요

| 트랙 | 기능 | 신규 파일 | 수정 파일 | 테스트 |
|---|---|---|---|---|
| **A** | 미장 영업일 체크 (Step 0) | `config/us_market_holidays.py` | `run_market.py` | 18 PASS |
| **B** | DRY_RUN 결과 리포트 | `core/dryrun_reporter.py` | `run_market.py` | 11 PASS |
| **C** | Gemini AI 톤 보정 (4키 chain) | `core/gemini_gateway.py`<br>`publishers/kr_ai_formatter.py` | `run_market.py` | 39 PASS |
| **D** | 워크플로우 + 통합 | — | `.github/workflows/kr_daily.yml`<br>`requirements.txt` | 10 PASS |

**핵심 결정**:
- `publishers/kr_formatter.py`는 **무수정** — AI 로직을 별도 모듈로 분리하여 fallback 자동 작동
- Gemini는 무료 키(Main → Sub → Sub2) 우선, 전부 실패 시 유료 키(Pay) fallback
- `cron`은 KST 평일 08:30 (UTC 23:30 1-5) — 미국 휴장 다음 날 자동 스킵

---

## 📦 파일 구조 (압축 파일 내부)

```
kr-os-upgrade/
├── README.md                              # 본 문서
├── requirements.txt                       # ← 교체 (google-genai 추가)
├── run_market.py                          # ← 교체 (v1.3.0 → v1.4.0)
│
├── config/
│   ├── __init__.py                        # 신규 (없으면 추가)
│   └── us_market_holidays.py              # 신규
│
├── core/
│   ├── __init__.py                        # 신규
│   ├── dryrun_reporter.py                 # 신규
│   └── gemini_gateway.py                  # 신규
│
├── publishers/
│   ├── __init__.py                        # 신규 (없으면 추가)
│   └── kr_ai_formatter.py                 # 신규
│
├── tests/
│   ├── __init__.py                        # 신규 (없으면 추가)
│   ├── test_us_market_holidays.py         # 신규
│   ├── test_dryrun_reporter.py            # 신규
│   ├── test_gemini_gateway.py             # 신규
│   ├── test_kr_ai_formatter.py            # 신규
│   └── test_integration_smoke.py          # 신규
│
└── .github/workflows/
    └── kr_daily.yml                       # ← 교체
```

**중요**: 라이트 OS 본체 파일(`collectors/`, `engines/`, `db/`, `publishers/kr_formatter.py`, `publishers/x_publisher.py`, `publishers/tg_publisher.py`)은 **수정 없음**. 압축 파일에 포함되어 있지 않음.

---

## 🚀 마이그레이션 가이드 (GitHub 웹 에디터로 직접 push)

### 1단계: 신규 파일 추가 (충돌 없음)

```
config/us_market_holidays.py
core/__init__.py
core/dryrun_reporter.py
core/gemini_gateway.py
publishers/kr_ai_formatter.py
tests/test_us_market_holidays.py
tests/test_dryrun_reporter.py
tests/test_gemini_gateway.py
tests/test_kr_ai_formatter.py
tests/test_integration_smoke.py
```

기존 `__init__.py`가 없으면 빈 파일로 추가 (`config/__init__.py`, `publishers/__init__.py`, `tests/__init__.py`).

### 2단계: 기존 파일 교체 (롤백 가능 단위)

| 파일 | 변경 핵심 |
|---|---|
| `requirements.txt` | `google-genai>=1.0.0` 추가 (기존 6개 그대로 유지) |
| `run_market.py` | v1.3.0 → v1.4.0 (Step 0 신규, Step 6/6.5 수정, `_build_snapshot` x_published 로직 보완) |
| `.github/workflows/kr_daily.yml` | cron 정정 + test job + Gemini 키 + dry_run input + artifact upload |

### 3단계: GitHub Secrets 등록 (필수)

`Settings → Secrets and variables → Actions`에서 다음 4개 추가/확인:

```
GEMINI_API_KEY              ← 메인 무료 키 (필수)
GEMINI_API_SUB_KEY          ← 서브 무료 키 (선택, 권장)
GEMINI_API_SUB_SUB_KEY      ← 서브2 무료 키 (선택, 권장)
GEMINI_API_SUB_PAY_KEY      ← 유료 fallback 키 (선택)
```

본 OS(`investment-os`)에 이미 등록된 키들을 그대로 라이트 OS 리포지터리에도 추가. 본 OS의 `gemini_gateway.py` v3.1.0이 이미 동일한 환경변수를 인식 중.

**키 미등록 시 동작**: USE_AI_TONE=true여도 Gemini 호출 실패 → fallback(Type A) 자동 작동. 안전.

### 4단계: 동작 확인 (실행 전)

GitHub Actions 탭에서 `KR Market Daily` 워크플로우 → `Run workflow`:

| 시나리오 | 입력 | 예상 결과 |
|---|---|---|
| **DRY_RUN 테스트** | `dry_run: true`, `force_run: true` | 트윗 생성 + 리포트만 (발행/Supabase 저장 없음). artifact `dryrun-report-*` 다운로드 가능 |
| **AI 톤 + DRY_RUN** | `dry_run: true`, `use_ai_tone: true` | Gemini 호출 → 3트윗 생성. 리포트 확인 |
| **휴장일 우회 테스트** | `force_run: true` (주말 실행) | 휴장 체크 우회, 정상 진행 |

### 5단계: 정식 가동

cron 스케줄: KST 평일 08:30 (UTC 23:30 월~금). 미장 휴장 다음 날에는 코드 내부에서 자동 스킵.

---

## 🔧 환경변수 정리

| 환경변수 | 디폴트 | 설명 |
|---|---|---|
| `DRY_RUN` | `false` | 발행 없이 트윗 생성 + JSON 리포트만 |
| `FORCE_RUN` | `false` | 휴장일/주말 강제 실행 |
| `USE_AI_TONE` | `true` | Gemini AI 톤 보정 (false 시 Type A fallback) |
| `GEMINI_API_KEY` | (없음) | 메인 무료 키 |
| `GEMINI_API_SUB_KEY` | (없음) | 서브 무료 키 |
| `GEMINI_API_SUB_SUB_KEY` | (없음) | 서브2 무료 키 |
| `GEMINI_API_SUB_PAY_KEY` | (없음) | 유료 fallback 키 |

기존 환경변수 (FRED, X, TG, Supabase 등)는 변경 없음.

---

## 🧪 테스트 결과 (78/78 PASS)

| 테스트 파일 | PASS |
|---|---|
| `test_us_market_holidays.py` | 18 |
| `test_dryrun_reporter.py` | 11 |
| `test_gemini_gateway.py` | 12 |
| `test_kr_ai_formatter.py` | 27 |
| `test_integration_smoke.py` | 10 |
| **합계** | **78** |

로컬 검증 명령:
```bash
pip install -r requirements.txt
pip install pytest
pytest -q tests/
```

---

## ⚠️ 주의사항 / 알려진 제한사항

### 1. 비용 통제
- 무료 키만 등록 시 **비용 0**. 유료 키 fallback이 작동할 때만 과금 (보통 무료 한도 충분)
- Gemini Flash-Lite 1회 호출 ≈ $0.0004 (한 달 22회 ≈ 1센트 미만)
- 유료 키 진입 시 `[GeminiGW] ⚠️ 무료 키 전부 실패 → 유료 키(pay) 사용` 경고 로그

### 2. AI 응답 품질 가드
- 280자 초과 → 자동 재시도 (1회)
- 비한국어 문자(힌디/아랍/태국/러시아/가나) 감지 → 자동 재시도
- 재시도 실패 → 자동 fallback (Type A)

### 3. DRY_RUN 게이트 우회 (Track B)
- 데이터 누락 있어도 DRY_RUN이면 트윗 생성 시뮬레이션 진행 (Type A 또는 AI)
- 실 발행은 `can_publish=True AND dry_run=False`일 때만
- DRY_RUN의 `tweet_ids`는 `["dry_run_id", ...]` 더미값. `_build_snapshot`이 이를 인식해 `x_published=False` 설정

### 4. 휴장일 정밀도 (Track A)
- 2026/2027년 NYSE 휴장일 하드코딩 (10건/연)
- 한국 아침 발행 = 전날 ET 마감 데이터 → 전날 ET 기준으로 휴장 체크
- DST(서머타임) 정밀도는 날짜 단위 체크에 영향 없음 (EST -5h 고정)
- 2028년 이후는 `config/us_market_holidays.py`에 추가 필요

### 5. cron 스케줄
- `30 23 * * 1-5` (UTC) = KST 평일 **08:30**
- 정각(`:00`) 회피 — GitHub Actions 혼잡 시간대 분산 (마스터 메모리 원칙 준수)
- 미국 휴장 다음 날: 코드 내부 Step 0에서 자동 스킵 (cron은 매일 트리거됨)

### 6. 본 OS와의 호환성
- 본 OS(`investment-os`) `gemini_gateway.py` v3.1.0은 이미 `GEMINI_API_SUB_PAY_KEY` 인식
- GitHub Secret 추가 시 양쪽 시스템 자동 적용
- 두 시스템 간 코드 분리 유지 (별도 repo, 별도 import)

### 7. 롤백 절차
문제 발생 시 순서:
1. **즉시 비활성화**: GitHub Secrets에서 `USE_AI_TONE` 영향 안 받음 → 워크플로우 input에서 `use_ai_tone: false` 또는 환경변수로 제어
2. **AI만 비활성화**: `USE_AI_TONE=false`로 디폴트 변경 (`.github/workflows/kr_daily.yml`의 `inputs.use_ai_tone || 'true'`를 `'false'`로)
3. **전체 롤백**: `run_market.py`를 v1.3.0으로 되돌리기 (이전 git 커밋 revert)

---

## 📋 작업 체크리스트 (마스터용)

```
[ ] 1단계: 신규 파일 10개 GitHub에 업로드
[ ] 2단계: 기존 3개 파일 교체 (requirements.txt, run_market.py, kr_daily.yml)
[ ] 3단계: GitHub Secrets에 GEMINI_API_KEY 계열 4개 등록
[ ] 4단계: workflow_dispatch로 dry_run=true + force_run=true 수동 실행
[ ] 5단계: artifact `dryrun-report-*` 다운로드 → JSON 내용 검토
[ ] 6단계: artifact `run-logs-*` 다운로드 → 에러 없는지 확인
[ ] 7단계: AI 톤 트윗 품질 검토 (3트윗, 한국어, 280자 이내)
[ ] 8단계: dry_run=false 1회 실행 → 실제 X/TG 발행 확인
[ ] 9단계: 정식 가동 (cron 스케줄 자동 트리거 대기)
```

---

## 🔍 트러블슈팅

### Q1. test job이 GitHub Actions에서 실패
- `requirements.txt`에 `google-genai>=1.0.0` 추가 확인
- `pip install pytest` 별도 실행 (test job 내부)

### Q2. Gemini 호출 실패 로그가 계속 나타남
- GitHub Secrets에 `GEMINI_API_KEY` 등록 확인
- 무료 키 quota 소진 가능성 → SUB_KEY 등록 권장
- 일시적 실패는 fallback으로 자동 복구되므로 안전

### Q3. 휴장일에도 발행됨
- `_check_us_market_session`은 **전날 ET 기준**으로 체크 (한국 아침 발행 시 전날 미장 데이터 사용)
- 2028년 이후 휴장일은 `config/us_market_holidays.py`에 추가 필요

### Q4. AI 트윗에 영어/숫자만 나옴
- 비한국어 가드는 힌디/아랍 등을 차단하지만, 영어 지배는 차단하지 않음
- 프롬프트의 `_SYSTEM_INSTRUCTION`에서 "한국어 + 영어" 허용 명시 → temperature 조정 또는 프롬프트 강화로 개선 가능

---

**문의/이슈**: 마스터에게 직접 보고

