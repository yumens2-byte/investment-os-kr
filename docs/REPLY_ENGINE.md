# X Reply Engine 운영·설계 가이드 (v1.6.0)

마지막 검토: **2026-09-10**

> 목표는 탐지 회피가 아니라 **내 게시글에 직접 달린 정상 댓글에만, 낮은 빈도로,
> 관련성 있는 답글을 공식 X API로 발행**하는 것입니다. 지연이나 문구 다양화는
> 플랫폼 정책을 우회하는 수단이 아니며 차단 방지를 보장하지 않습니다. X의 최신
> [자동화 정책](https://help.x.com/en/rules-and-policies/x-automation)과
> [플랫폼 조작·스팸 정책](https://help.x.com/en/rules-and-policies/platform-manipulation)을
> 운영자가 배포 전과 정책 변경 시 직접 확인해야 합니다.

## 1. 워크플로우 분석

`reply_engine.yml`은 하루 4회(KST 10:17, 13:17, 18:17, 22:17) 실행됩니다.
각 실행은 전체 테스트를 먼저 통과해야 하며, 동일 워크플로우의 중복 실행은
`concurrency`로 직렬화됩니다. 이후 `run_reply.py`가 아래 순서로 동작합니다.

1. `REPLY_ENABLED`와 실행 모드 확인
2. 일일 API 비용/호출 예산 확인
3. OAuth user context로 멘션 수집 및 커서 전진
4. 직접 답글, 자기 댓글, 블랙리스트, 연령, 링크·스팸 여부 필터링
5. conversation root 작성자가 내 계정인지 별도 검증
6. 긍정/호응 댓글만 default-deny 방식으로 분류
7. 원글과 댓글을 함께 사용해 답글 생성
8. 길이, 투자 권유, 질문, 멘션·링크, 원문 반향, 최근 문구 유사도 게이트
9. 이력을 먼저 기록한 뒤 X 발행, 성공 직후 응답 ID와 예산 기록
10. JSON 검수 리포트와 로그를 14일간 artifact로 보관

## 2. 이번 검토에서 확인한 위험과 조치

| 우선순위 | 위험 | 조치/운영 원칙 |
|---|---|---|
| 높음 | 변수에 음수나 과도한 상한을 넣으면 보호 장치 의미가 약해짐 | 일/회/저자/대화/댓글 연령 상한을 코드에서 유효 범위로 강제 |
| 높음 | 일일 상한만으로 한 번의 실행에 답글이 몰릴 수 있음 | `REPLY_RUN_CAP` 기본 2건 추가; live 직전에도 재검증 |
| 높음 | 수동 실행에서 실수로 `live` 선택 | `confirm_live=true`가 없으면 수동 live 실행 실패 |
| 높음 | 타인 스레드에서 문맥·화자 역할이 뒤집힘 | 기본 비활성인 `REPLY_FOREIGN_THREAD_ENABLED=false` 유지 권장 |
| 중간 | 반복 답글이 스팸/저품질로 보임 | 최근 30건 유사도 게이트, 배치 내 중복 검사, 저자당 1건/일 유지 |
| 중간 | 액션 토큰의 불필요한 권한 | workflow 권한을 `contents: read`로 명시 |
| 중간 | 지터가 정책 준수 대신 '탐지 회피'로 오해될 수 있음 | 지터 목적을 동시 실행·순간 부하 분산으로 명시; 정책 우회에 사용 금지 |
| 잔여 | 최대 100건 수집이 포화되면 더 오래된 멘션이 누락될 수 있음 | `collection_saturated` 경고 감시; 반복 발생 시 페이지네이션을 별도 설계 |
| 잔여 | GitHub schedule은 정시 실행을 보장하지 않음 | 커서 정체 경고와 artifact를 함께 확인하고 필요 시 수동 dry-run 수행 |
| 잔여 | API 성공 후 DB 갱신 전 프로세스가 종료되는 작은 중복 창 | 발행 재시도 없음과 이력 선기록 유지; 향후 X 응답 조회 기반 reconciliation 검토 |

## 3. 자연스러운 품질을 위한 안전 원칙

- **선택적으로 답합니다.** 질문, 비판, 모호함, 광고에는 답하지 않는 것이 기본입니다.
- **원문을 되풀이하지 않습니다.** 댓글 상황어를 그대로 반사하거나 상대의 의도를
  추측하는 답글은 게이트에서 차단합니다.
- **사실·투자 판단을 생성하지 않습니다.** 감사와 가벼운 호응만 허용하며 매수·매도,
  전망, 수익 보장, 외부 링크, 대화 유도 질문은 금지합니다.
- **속도보다 관련성을 우선합니다.** 회당 2건, 저자당 하루 1건을 권장합니다.
- **임의 오탈자, 유니코드 변형, 프록시, 계정 순환, 헤더 위장 등 탐지 회피 기법은
  사용하지 않습니다.** 이런 기법은 자연스러움이 아니라 정책·신뢰 위험을 높입니다.
- AI fallback 문구 풀은 월 1회 사람이 검수하고, 실제 표본은 shadow 리포트의
  `review` 배열로 판단합니다.

## 4. 권장 GitHub Variables

| 변수 | 권장값 | 의미 |
|---|---:|---|
| `REPLY_ENABLED` | 초기 `false` | 전체 kill switch |
| `REPLY_MODE` | 초기 `dry_run` | `dry_run` → `shadow` → `live` 승격 |
| `REPLY_RUN_CAP` | `2` | 실행당 답글 수(코드 허용 1~10) |
| `REPLY_DAILY_CAP` | `8` 이하 | 하루 전체 답글 수(1~50) |
| `REPLY_AUTHOR_DAILY_CAP` | `1` | 같은 작성자에 대한 하루 답글 수(1~10) |
| `REPLY_CONV_DAILY_CAP` | `3` | 같은 대화의 하루 답글 수(1~20) |
| `REPLY_MAX_AGE_HOURS` | `24` | 오래된 댓글 폐기(1~168시간) |
| `REPLY_MENTIONS_MAX_RESULTS` | `100` | 1회 멘션 조회 크기(5~100) |
| `REPLY_LIKE_ENABLED` | `false` | 답글과 무관한 자동 좋아요는 기본 금지 |
| `REPLY_FOREIGN_THREAD_ENABLED` | `false` | 타인 원글 스레드 답글 금지 |
| `X_MY_USER_ID` | 내 숫자 ID | `get_me` 호출 절약; 계정 변경 시 반드시 갱신 |

API 단가 변수는 현재 계약/Developer Portal의 값을 운영자가 입력해야 합니다. 단가를
모르면 비워 두어 호출 수 기반 보수 상한을 사용합니다. 비밀키는 Variables가 아니라
반드시 Actions Secrets에 저장합니다.

## 5. 단계별 배포

1. `REPLY_ENABLED=false`, `REPLY_MODE=dry_run`으로 수동 실행하고 테스트와 artifact를 확인합니다.
2. `REPLY_ENABLED=true`, `REPLY_MODE=dry_run`으로 실제 멘션 수집 범위만 확인합니다.
3. 최소 3일간 `shadow`로 운영하며 `review`의 오분류, 역할 반전, 반복 문구를 전수 검수합니다.
4. `REPLY_RUN_CAP=1`, 좋아요/타인 스레드 비활성 상태로 첫 live 수동 실행을 합니다.
5. 정상 표본을 확인한 뒤에도 회당 2건, 저자당 1건/일을 기본으로 유지합니다.
6. 403/429, `SPEND_CAP`, 커서 정체, 포화가 보이면 즉시 `REPLY_ENABLED=false`로 전환합니다.

## 6. 운영 체크리스트와 롤백

매일 `published`, `skip_reasons`, `collection_saturated`, `cursor_stale_hours`,
`user_id_mismatch`, 예산 스냅샷을 확인합니다. 발행량의 갑작스러운 증가, 동일 작성자
반복, `PUBLISH_FAIL`, 403/429가 있으면 자동 재시도나 상한 상향을 하지 않습니다.

긴급 롤백은 Actions Variables에서 `REPLY_ENABLED=false`로 변경합니다. 이미 큐에 들어간
실행은 취소하고, 원인을 확인하기 전까지 `dry_run`으로 유지합니다. 키 노출이 의심되면
X와 Gemini 키를 폐기·재발급하고 GitHub Actions 로그도 점검합니다.

## 7. 로컬 검증

```bash
python -m compileall -q reply_engine run_reply.py
ruff check reply_engine run_reply.py tests/test_reply*.py
pytest -q tests/
```

YAML 변경 후에는 `actionlint .github/workflows/reply_engine.yml`도 권장합니다.
