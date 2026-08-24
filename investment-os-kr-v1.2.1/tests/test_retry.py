"""
utils/retry.py 단위 테스트
재시도 횟수, 간격, 최종 실패 시 None 반환, 성공 반환값 검증
"""

from __future__ import annotations

from unittest.mock import patch

from utils.retry import with_retry

# ---------------------------------------------------------------------------
# 성공 케이스
# ---------------------------------------------------------------------------

class TestRetrySuccess:
    def test_first_attempt_success(self):
        """1회 만에 성공하면 즉시 반환."""
        result = with_retry(lambda: 42, label="test")
        assert result == 42

    def test_returns_func_value(self):
        """함수 반환값을 그대로 전달."""
        result = with_retry(lambda: {"key": "value"}, label="test")
        assert result == {"key": "value"}

    def test_none_return_is_valid(self):
        """함수가 None을 반환해도 성공으로 처리."""
        # with_retry는 예외가 없으면 None도 성공으로 처리
        result = with_retry(lambda: None, label="test")
        assert result is None

    def test_args_forwarded(self):
        """위치 인자가 정상 전달됨."""
        result = with_retry(lambda x, y: x + y, 3, 4, label="test")
        assert result == 7

    def test_kwargs_forwarded(self):
        """키워드 인자가 정상 전달됨."""
        result = with_retry(lambda x=0, y=0: x * y, x=3, y=4, label="test")
        assert result == 12


# ---------------------------------------------------------------------------
# 재시도 케이스
# ---------------------------------------------------------------------------

class TestRetryOnFailure:
    def test_retries_on_exception(self):
        """실패 후 재시도하여 성공."""
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("일시 실패")
            return "성공"

        result = with_retry(flaky, delay_sec=0, label="test")
        assert result == "성공"
        assert len(calls) == 3

    def test_retry_count_exact(self):
        """정확히 max_attempts 횟수만큼 시도."""
        calls = []

        def always_fail():
            calls.append(1)
            raise ValueError("항상 실패")

        with_retry(always_fail, max_attempts=3, delay_sec=0, label="test")
        assert len(calls) == 3

    def test_all_fail_returns_none(self):
        """max_attempts 모두 실패 시 None 반환."""
        result = with_retry(
            lambda: (_ for _ in ()).throw(RuntimeError("실패")),
            max_attempts=3,
            delay_sec=0,
            label="test",
        )
        assert result is None

    def test_success_on_second_attempt(self):
        """2번째 시도에서 성공."""
        calls = []

        def second_try():
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("타임아웃")
            return "2차 성공"

        result = with_retry(second_try, delay_sec=0, label="test")
        assert result == "2차 성공"
        assert len(calls) == 2

    def test_custom_max_attempts(self):
        """max_attempts 파라미터 커스텀."""
        calls = []

        def always_fail():
            calls.append(1)
            raise Exception("실패")

        with_retry(always_fail, max_attempts=5, delay_sec=0, label="test")
        assert len(calls) == 5

    def test_delay_called_between_retries(self):
        """재시도 간 time.sleep 호출 확인."""
        calls = []

        def fail_twice():
            calls.append(1)
            if len(calls) < 3:
                raise Exception("실패")
            return "ok"

        with patch("utils.retry.time.sleep") as mock_sleep:
            with_retry(fail_twice, max_attempts=3, delay_sec=2.0, label="test")
            # 1회 실패 → sleep, 2회 실패 → sleep, 3회 성공 → sleep 없음
            assert mock_sleep.call_count == 2
            mock_sleep.assert_called_with(2.0)

    def test_no_delay_on_last_attempt(self):
        """마지막 시도(최종 실패) 후 sleep 없음."""
        with patch("utils.retry.time.sleep") as mock_sleep:
            with_retry(
                lambda: (_ for _ in ()).throw(Exception("실패")),
                max_attempts=3,
                delay_sec=2.0,
                label="test",
            )
            # max_attempts=3이면 sleep은 2번 (1→2 사이, 2→3 사이)
            assert mock_sleep.call_count == 2


# ---------------------------------------------------------------------------
# 예외 타입 무관 처리
# ---------------------------------------------------------------------------

class TestRetryExceptionTypes:
    def test_catches_connection_error(self):
        result = with_retry(
            lambda: (_ for _ in ()).throw(ConnectionError()),
            max_attempts=1, delay_sec=0, label="test",
        )
        assert result is None

    def test_catches_timeout_error(self):
        result = with_retry(
            lambda: (_ for _ in ()).throw(TimeoutError()),
            max_attempts=1, delay_sec=0, label="test",
        )
        assert result is None

    def test_catches_value_error(self):
        result = with_retry(
            lambda: (_ for _ in ()).throw(ValueError("잘못된 값")),
            max_attempts=1, delay_sec=0, label="test",
        )
        assert result is None

    def test_catches_runtime_error(self):
        result = with_retry(
            lambda: (_ for _ in ()).throw(RuntimeError()),
            max_attempts=1, delay_sec=0, label="test",
        )
        assert result is None
