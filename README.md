# KR Market OS — 라이트 OS 고도화 v1.4.0

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
