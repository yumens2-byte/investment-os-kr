# 자동댓글 v2 운영 반영·베타 결과

검증일: 2026-10-02 KST.

## 결론

3개 독립 AI 코드리뷰에서 확인된 문제를 수정·재검증하고 PR #22를 main에 반영했다. 전체 640건과 실제 SDK 오프라인 통합 6건이 통과했다. 운영 베타의 서비스 연결·필수 DB 정합성·예산 저장은 정상이다.

**신규 멘션이 0건이어서 실제 답글 발행은 검증되지 않았다.** 운영 베타 실행 성공과 실제 발행 검증 완료를 구분한다. 실제 Gemini 생성·X 발행·발행 결과 DB 저장·자연스러움은 적격 신규 댓글이 들어오는 정상 운영 실행에서 확인해야 한다.

## 반영 근거

- [병합 PR #22](https://github.com/yumens2-byte/investment-os-kr/pull/22)
- 운영 커밋: 2348b619807f91a0608ddb2480f1a8ea631364dc.
- [최종 PR CI](https://github.com/yumens2-byte/investment-os-kr/actions/runs/36926447287): 성공, 640 passed / 14.80초, Ruff 통과.
- [운영 베타](https://github.com/yumens2-byte/investment-os-kr/actions/runs/36926602894): 성공, 운영 커밋으로 실행.
- beta artifact: reply-beta-36926602894, ID 11194192681.
- 사용자 승인에 따라 반영했다. 기존 운영 Variables, 정기 스케줄, DB 스키마는 변경하지 않았다.

## 실제 운영 결과

| 항목 | 결과 |
|---|---|
| 베타 사전 전체 테스트 | 640건 통과 |
| dry_run | EXIT_NO_MENTIONS, 수집 0건 |
| live | EXIT_NO_MENTIONS, 신규/복구 대상 0건 |
| X 답글 발행 | 0건 |
| Gemini 호출 | 0회 |
| 멘션 읽기 | dry_run 1회 + live 1회 |
| cursor 전진 | 없음 |
| 필수 DB 스키마 검사 | 사전·사후 정상 |
| DB 이력 검사 | 사전·사후 123건 |
| 발행 상태 불일치 | 0건 |
| 중복 response_tweet_id | 0건 |
| 미완결 live 상태 | 0건 |
| 결과 불명 발행 상태 | 0건 |
| publication_verified | false |

베타 live는 최대 1회 발행 시도로 제한했고 좋아요는 비활성화했다. dry_run 실제 API 읽기 사용량을 베타 실행기가 별도로 저장한 후 live를 실행했다. 설정 단가에 따른 이번 베타 추정 비용 증분은 100원(읽기 2회 × 50원)이며, 실제 청구 금액을 확인한 값은 아니다. 실행 종료 시 당일 누계는 read_calls=5, write_calls=0, gemini_calls=0, est_cost_krw=250이었다.

사전·사후 감사에서 선택적 kr_reply_likes 조회는 APIError였다. 필수 답글 테이블 검사는 정상이고 이번 베타는 좋아요 비활성이므로 답글 검증의 차단 요인은 아니었다. 이 결과만으로 테이블 부재나 권한 오류의 원인을 확정할 수 없다. 좋아요 활성화 전에는 해당 선택적 테이블 계약을 별도로 확인해야 한다.

## 잔여 검증

- 신규 적격 댓글의 실제 Gemini 답글과 gate 결과.
- 실제 X 발행 ID 및 responded=true/response_tweet_id 저장.
- 원 댓글·부모 문맥과 답글의 자연스러움 및 역할 일치.
- 운영 표본을 누적한 스킵 사유별 변화와 오분류율.

신규 멘션이 없는 상태에서 테스트용 공개 댓글을 임의로 만들거나 커서를 되돌려 과거 댓글을 강제로 재발행하지 않았다. 기존 스케줄에서 새 운영 버전이 적용되며, 신규 댓글이 생긴 실행의 artifact를 기준으로 잔여 검증한다.

상세 원인·수정·검증 계약은 [REPLY_ENGINE_V2_REVIEW.md](REPLY_ENGINE_V2_REVIEW.md)와 [REPLY_ENGINE_V2.md](REPLY_ENGINE_V2.md)에 기록했다.

## 잔여 진행 결과

후속 작업에서 실제 댓글·부모 문맥 6건을 검증하고 무료 Gemini 2회로 재생했다. 최종 6건 gate 통과(문맥 반영 AI 4건, 정형 fallback 2건) 및 독립 의미 검토를 수행했다. 실제 발행은 신규 적격 댓글 0건으로 미검증이다.

좋아요 오류는 코드가 요구하는 테이블 부재와 운영 anon 키의 권한 거부 `42501`을 순서대로 확인했다. 서버 전용 테이블·권한을 보완했고 reply 전용 서버 Secret 우선 연결을 준비했다. `SUPABASE_REPLY_SERVICE_ROLE_KEY` 등록 전에는 조회 오류가 남는다. 최종 전체 660건 테스트 통과와 상세 실제 결과·필요 설정은 [REPLY_ENGINE_V2_RESIDUAL.md](REPLY_ENGINE_V2_RESIDUAL.md)를 따른다. 위 초기 베타 시점의 DB 스키마 미변경 기록과 오류 원인 미확정 문구는 과거 실행의 기록이다.
