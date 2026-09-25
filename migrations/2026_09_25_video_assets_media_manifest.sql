-- 2026-09-25 Weekly Digest v2 (B안 4초×4샷)
-- 목적: v2 미디어 manifest(키프레임/샷 경로, Motion QA 점수, 비용 내역, 재생성 횟수) 저장.
--       기존 cut1~3_video_uri 는 3컷까지만 담을 수 있어 4샷 구조를 표현하지 못한다.
-- 멱등: ADD COLUMN IF NOT EXISTS — 재실행 안전. 기존 행/코드 영향 없음(NULL 허용).
-- 코드 호환: weekly_media._update_row 는 컬럼 미적용 환경에서 manifest 만 제외하고 재시도한다.

ALTER TABLE icg.video_assets
    ADD COLUMN IF NOT EXISTS media_manifest_json JSONB;

COMMENT ON COLUMN icg.video_assets.media_manifest_json IS
    'Weekly Digest v2 media manifest: keyframes, shots, motion QA, cost breakdown, regenerations';

NOTIFY pgrst, 'reload schema';
