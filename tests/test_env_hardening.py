"""H-1 회귀 (2026-08-20 Actions 실사고): 미등록 GitHub Variable → env 빈 문자열 → int('') 크래시.

수정: reply_engine.config.env_int — 빈 값/파싱 불가는 기본값 fallback (설정 오류 ≠ 크래시).
"""

from __future__ import annotations

import importlib

from reply_engine.config import env_int

_FOLLOWING_INT_ENVS = (
    "FOLLOWING_MIN_RELEVANCE", "FOLLOWING_MIN_CONTENT", "FOLLOWING_MIN_ENGAGEMENT",
    "FOLLOWING_MAX_ACTIONS_PER_RUN", "FOLLOWING_MAX_ACTIONS_PER_DAY",
    "FOLLOWING_AUTHOR_COOLDOWN_HOURS",
)


def test_env_int_parser():
    import os
    os.environ["_H1_TEST"] = ""
    assert env_int("_H1_TEST", 5) == 5          # 빈 문자열 → 기본값 (실사고 케이스)
    os.environ["_H1_TEST"] = "  "
    assert env_int("_H1_TEST", 5) == 5          # 공백만
    os.environ["_H1_TEST"] = "abc"
    assert env_int("_H1_TEST", 5) == 5          # 파싱 불가
    os.environ["_H1_TEST"] = "7"
    assert env_int("_H1_TEST", 5) == 7          # 정상값
    del os.environ["_H1_TEST"]
    assert env_int("_H1_TEST", 5) == 5          # 미설정


def test_following_config_import_survives_empty_envs(monkeypatch):
    """실사고 완전 재현: yml이 미등록 변수를 빈 문자열로 주입해도 import가 성공해야 한다."""
    import following_engine.config as fc

    for name in _FOLLOWING_INT_ENVS:
        monkeypatch.setenv(name, "")
    try:
        importlib.reload(fc)   # 수정 전에는 여기서 ValueError로 크래시
        assert fc.MAX_ACTIONS_PER_DAY == 5
        assert fc.MAX_ACTIONS_PER_RUN == 2
        assert fc.MIN_RELEVANCE_SCORE == 85
        assert fc.AUTHOR_COOLDOWN_HOURS == 24
    finally:
        for name in _FOLLOWING_INT_ENVS:
            monkeypatch.delenv(name, raising=False)
        importlib.reload(fc)   # 모듈 상태 원복


def test_reply_config_import_survives_empty_envs(monkeypatch):
    """동일 잠복 결함 — reply_engine 쪽도 빈 env에 면역이어야 한다."""
    import reply_engine.config as rc

    names = ("REPLY_DAILY_CAP", "REPLY_AUTHOR_DAILY_CAP", "REPLY_CONV_DAILY_CAP",
             "REPLY_MAX_AGE_HOURS", "REPLY_PUBLISH_DELAY_MAX_SEC")
    for name in names:
        monkeypatch.setenv(name, "")
    try:
        importlib.reload(rc)
        assert rc.REPLY_DAILY_CAP == 8
        assert rc.PUBLISH_START_DELAY_MAX_SEC == 600
    finally:
        for name in names:
            monkeypatch.delenv(name, raising=False)
        importlib.reload(rc)


def test_versions_bumped_h_series():
    import following_engine.config as fc
    import reply_engine.config as rc

    assert rc.VERSION == "1.2.0"
    assert fc.VERSION == "1.0.2"
