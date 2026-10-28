# 대댓글 자연스러운 호응 고도화 — 상세설계 (2026-10-28)

기준 소스: main `f4f1331`. 근거 데이터: 운영 DB `kr_reply_history`(SELECT만), 실행 로그 run 40279047027 / 40460452506.

## 0. 현황 근거 요약

| 항목 | 사실 |
|---|---|
| live 발행 | 74건 (08-26 ~ 10-26) |
| PUBLISH_UNKNOWN | 6건, 10-10 이후 5건. 최근 2회 실행 연속 |
| 실패 패턴 | 첫 발행 딜레이(623s/673s) 종료 후 약 16분 정지 → `RemoteDisconnected` (두 로그 동일) |
| tweepy 4.17 | `Client.session = requests.Session()` 1개를 재사용, timeout 미지정 |
| Gemini | main 키 `gemini-2.5-flash-lite` 호출이 매번 404 → sub 키 성공 |
| 고정 문구 정확 일치 | 08/26~09/14 3% · 09/15~10/04 54% · 10/05~ 20% |
| 평균 길이 | 댓글 33→29→41자, 답글 18→15→12자 |
| 문구 경로 | 모델이 프롬프트 예시 문장을 그대로 출력 → 유사도 게이트 탈락 → 의도별 3~6개 안전 풀 → 상투 문구 |
| 분류 | CLASS_QUESTION 5건 전부 AI 분류기 판정 (룰 판정 0). 선택 표명·추측형 혼잣말이 QUESTION으로 분류됨 |
| 반응 증거 | "선택 감사합니다 😊" 답글에 상대가 "ㅎㅎㅎㅎ 멀요"로 반문 |
| 측정 공백 | 생성 경로(AI/고정 문구)·게이트 탈락 사유가 DB에 저장되지 않음 |

## 1. P0-1 X 발행 연결 안정화

원인 가설: 대기(최대 10분+) 동안 유휴였던 keep-alive 연결을 재사용 → 무응답 소켓에서 TCP 재전송 한도(리눅스 기본 약 15.5분)까지 정지. 수정 후 결과로 검증한다.

설계 (`reply_engine/x_client.py`):
- `TimeoutSession(requests.Session)`: `request()`에 timeout 미지정 시 `(connect 10s, read 60s)` 기본 적용.
- `get_x_client()`는 생성 직후 `client.session`을 `TimeoutSession`으로 교체.
- `reset_connection(client)`: 기존 세션 close 후 새 `TimeoutSession` 할당. `session` 속성이 없는 대역 객체는 무시.
- `run_reply.py`: 매 발행 직전(첫 딜레이·발행 간 지터 이후) `reset_connection(client)` 호출.
- 결과 불명 분류 정책(재시도 금지)은 변경하지 않는다. read timeout 도 기존처럼 PUBLISH_UNKNOWN.

## 2. P0-2 Gemini 404 키 반복 호출 제거

설계 (`core/gemini_gateway.py`):
- 프로세스 수명 집합 `_UNAVAILABLE_MODELS: set[(key_label, model_name)]`.
- 404 응답 시 등록 + 경고 1회("키 설정 확인 필요"). 이후 같은 (키, 모델)은 호출 생략.
- 근본 해결(main 키 프로젝트의 모델 권한)은 운영자 조치 대상이며 코드로 해결하지 않는다.

## 3. P1-1 생성: 예시 복사 차단 + 후보 3개

설계 (`reply_engine/generator.py`):
- 프롬프트의 구체 예시 문장(복사되어 유사도 탈락을 유발)을 방향 설명으로 교체.
- 응답 스키마 `{"id", "replies": [후보1, 후보2, 후보3]}`. 후보들은 첫 단어·구조가 서로 달라야 한다. 구 스키마 `reply`도 수용.
- `BatchResult.candidates[id]`에 정규화된 후보 목록 저장. `replies[id]`는 첫 후보(하위호환).
- `max_tokens = min(4096, 512 + 200 × 건수)`.

설계 (`reply_engine/gate.py`):
- `choose_reply(drafts, fallbacks, recent, comment_text, context)` → `(text|None, source, first_reason)`.
  순서: AI 후보 → 의도별 안전 풀. source는 `AI` / `AI_ALT`(2·3번째 후보) / `TEMPLATE_FALLBACK`.
- X(`run_reply.py`)·Facebook(`run_fb_reply.py`) 양쪽이 같은 함수를 사용한다.

## 4. P1-2 의도 확장 + 톤 게이트

`policy.intent_for(text, label, parent_text="")` 판정 순서:
MARKET_HYPE → REACTION → LAUGH → CONGRATULATIONS → **WORRY** → **GREETING** → **CHOICE** → THANKS → ACK(독자 공유) → PRAISE → **OPINION** → ACK

| 신규 의도 | 판정 | 안전 풀 방향 | 게이트 |
|---|---|---|---|
| WORRY | 무섭/공포/불안/걱정/어렵/힘들/쫄/버텨/비상/ㅠ·ㅜ | 공감, 전망·조언 없음 | 웃음·들뜬 표현(ㅎㅎ, ㅋㅋ, 반가, 기대, 화이팅, 😄🚀🔥 등) → `GATE_TONE` |
| GREETING | 좋은/행복한/편안한 + 하루/밤/아침/주말/저녁/휴일, "~보내세요/되세요" 안부 | 인사에 맞인사(미러링 회피) | 기존 ECHO 게이트 |
| CHOICE | 단독 A/B 표기 + (원글에 `A)`·`B)` 또는 선택 어휘) | 선택을 받았다는 중립 반응 | 지지 표현(좋은 선택, 현명, 정답, 저도 A/B, 동의) → `GATE_INTENT` |
| OPINION | 본문 40자 이상 의견, 다른 의도 미해당 | 정성 들인 의견을 잘 읽었다는 반응 | 기존 게이트 |

- 기존 의도의 판정 결과는 신규 의도가 끼어드는 경우 외에는 바뀌지 않는다(회귀 테스트로 확인).
- 감사가 포함된 인사("감사합니다 휴일 즐겁게 보내세요")는 GREETING으로 판정한다.

## 5. P1-3 길이 2단계

- `config.REPLY_MAX_LENGTH_LONG = 60`, `reply_max_length(comment)` = 본문 40자 이상이면 60, 아니면 40.
- 프롬프트 항목에 `max_len` 포함. 긴 의견에는 댓글 속 구체 소재 하나를 자기 말로 받아 호응(전망·동의·조언 금지).
- 게이트 GATE_LENGTH는 댓글 기준 상한을 사용한다(댓글 미지정 시 40, 기존 동작).

## 6. P1-4 측정 기반

- `encode_metadata`에 `response_source`, `draft_gate_reason`, `intent` 저장 (tweet의 `_response_source` 등 내부 키에서 복사).
- 실행 리포트 summary에 `source_counts` 추가.
- 운영 집계 SQL: `error_message::jsonb->>'response_source'` 기준 기간별 비율.

## 7. P2 정책

- ① 질문 댓글: **무응답 정책 유지**(마스터 별도 결정 전 보수적 기본값). 대신 오분류를 줄인다.
  - 분류 프롬프트: QUESTION은 운영자의 답변을 실제로 요구하는 경우만. 선택형 원글에 대한 선택 표명, "~듯/~지" 추측형 혼잣말, 감탄형은 QUESTION 아님.
  - 룰: 선택형 원글(`A)`·`B)`)에 물음표·의문 어미 없는 A/B 표명 → SUPPORTIVE_NEUTRAL.
- ② 실행 빈도(cron 4회 vs 실제 약 2회): 원인 미확정. 코드 변경 없음, 별도 추적 과제로 유지.

## 8. 하위호환·영향 범위

- X 생성·분류 프롬프트 골든 sha256(`tests/test_fb_reply_common.py`)은 의도된 변경으로 갱신한다. FB는 페르소나 한 줄만 다르다는 불변식은 유지.
- Facebook 파이프라인은 generator/gate/policy를 공유하므로 동일 개선이 적용된다.
- DB 스키마 변경 없음. 운영 변수·모드·cron 변경 없음.

## 9. 검증 계획

1. 단위·회귀: 신규 의도 분류, 신규 풀 전 문구 게이트 통과, 톤/선택 게이트 차단, 길이 2단계, 후보 선택, 구 스키마 수용, 세션 재생성·timeout, 404 캐시, 메타데이터 저장.
2. 실데이터 재현: 운영 이력의 문제 사례(본 문서 0절)를 대체 경로에 통과시켜 의도에 맞는 문구가 선택되는지 확인.
3. 한계: 오프라인 환경에 Gemini 키가 없어 AI 출력 품질은 테스트로 검증할 수 없다. shadow 실행 검수 → 마스터 승인 후 live.

## 10. 구현 결과 (2026-10-28)

| 설계 | 구현 위치 | 비고 |
|---|---|---|
| P0-1 | `x_client.TimeoutSession`, `reset_connection`, `run_reply.py` 발행 직전 호출 | Facebook은 요청별 timeout·비연결재사용이라 대상 아님 |
| P0-2 | `core/gemini_gateway._UNAVAILABLE_MODELS` | 테스트 격리용 `tests/conftest.py` 추가 |
| P1-1 | `generator` 프롬프트·`_parse_drafts`·`BatchResult.candidates`, `gate.choose_reply` | X·FB·`scripts/reply_quality_replay.py` 동일 경로 |
| P1-2 | `policy.intent_for` WORRY/GREETING/CHOICE/OPINION, `SAFE_POOLS`, `GATE_TONE`, CHOICE 지지 차단, 축하 무시 차단 | WORRY 정밀도: 감동의 ㅠ·부정문·지수 이름 제외 |
| P1-3 | `config.REPLY_MAX_LENGTH_LONG`, `policy.reply_max_length` | |
| P1-4 | 메타데이터 `response_source`·`draft_gate_reason`·`intent`, 리포트 `source_counts` | `reply_policy` 2.2.0 |
| P2-① | `classifier._is_vote_answer`, 분류 프롬프트 QUESTION 범위 | 질문 무응답 정책 유지 |

버전: run_reply 2.3.0, generator 2.3.0, gate 2.2.0, classifier 2.1.0, x_client 2.1.0, config 2.2.0, policy 2.2.0.
X 생성·분류 프롬프트 골든 sha256 갱신(의도된 변경).

### 검증
- 전체 테스트 949 passed (Python 3.11, CI와 동일 lock). 신규 46건.
- CI 정의 ruff 대상 전부 통과.
- 실데이터 재현(한국어 발행 72 + 결과 불명 6 = 78건. 외국어 정형 문구 경로 2건 제외. 기존 답글에 신규 게이트 적용):
  7건이 차단되어 의도별 문구로 대체된다. 나머지 71건은 차단되지 않는다(기존 정상 답글 보존).

| 일자 | 댓글 | 기존 답글 | 차단 사유 | 대체 |
|---|---|---|---|---|
| 09-20 | 고민될 때가 제일 어렵지 | 그쵸 ㅎㅎ 댓글 반갑습니다 | GATE_TONE | 흔들리는 장이라 신경 쓰이실 만해요 |
| 09-30 | 가즈아!!!! | 오늘도 힘차게 달려보시죠 ㅎㅎ 🔥 | GATE_MARKET_HYPE | 관심 가져주셔서 감사해요 |
| 10-05 | 돈복사 준비 | 돈복사 준비, 기대되네요 😊 | GATE_MARKET_HYPE | 반응 남겨주셔서 고맙습니다 |
| 10-06 | 👍👍👍 | 좋은 의견 감사합니다 😊 | GATE_INTENT | ㅎㅎ 반갑습니다 |
| 10-10 | 이런 지표 정리 덕분에 … | 지표 정리 감사합니다 😊 | GATE_ROLE_REVERSAL | 좋게 봐주셔서 감사해요 😊 |
| 10-12 | 에피소드 100화 축하드립니다 … | ㅎㅎ 반가워요 | GATE_INTENT | 축하해 주셔서 감사합니다 😊 |
| 10-13 | … 공포 지림요 ㅠ | ㅎㅎ 반가워요 | GATE_TONE | 불안한 마음 충분히 이해돼요 |

(09-30·10-05·10-06·10-10은 기존 게이트로도 차단되는 유형이며, 해당 게이트 도입 이전 발행분이다.)

### 미검증·잔여
- AI 후보 품질(후보 3개, 긴 의견 호응)은 오프라인에서 검증 불가. shadow 실행 검수 필요.
- P0-1 원인 가설은 다음 live 실행들의 PUBLISH_UNKNOWN 발생 여부로 확인한다.
- Gemini main 키 404의 근본 원인(키 프로젝트 권한)은 운영자 확인 대상.
- 실행 빈도(cron 4회 vs 실제 약 2회) 원인 미확정.

### 운영 측정 SQL
```sql
select error_message::jsonb->>'response_source' src, count(*)
from kr_reply_history
where mode = 'live' and responded and created_at >= '2026-10-28'
group by 1 order by 2 desc;
```
