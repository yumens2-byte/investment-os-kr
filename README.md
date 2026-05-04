# investment-os-kr

## CI / 배포 전 점검

GitHub Actions 배포 안정성을 위해 테스트 모듈을 운영합니다.

- 전체 테스트: `pytest -q`
- CI 스모크 테스트(배포 진입점): `pytest -q tests/test_ci_smoke.py`

`tests/test_ci_smoke.py`는 외부 API/X/TG/Supabase를 실제 호출하지 않고 `run_market.main()` 경로를 검증해,
배포 시점의 구조적 회귀(엔트리포인트 실패, 스냅샷 키 누락 등)를 조기 탐지합니다.
