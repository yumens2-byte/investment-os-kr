"""
shock_news_engine/collector.py — RSS 수집·정규화·해시 (v1.0.0)

소스별 타임아웃 10초, 실패 소스는 건너뜀 (부분 실패 허용).
24시간 초과 기사 제외. URL 정규화(추적 파라미터 제거) 후 SHA-256 → article_hash (L1 멱등키).
"""

from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from shock_news_engine.config import (
    ARTICLE_MAX_AGE_HOURS,
    MAX_ARTICLES_PER_SOURCE,
    RSS_SOURCES,
    RSS_TIMEOUT_SEC,
)

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "igshid")


def normalize_url(url: str) -> str:
    """추적 파라미터·fragment 제거 (동일 기사 이형 URL 통일 — L1 정확도)."""
    try:
        parts = urlparse(url.strip())
        query = [
            (k, v) for k, v in parse_qsl(parts.query)
            if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
        ]
        return urlunparse(parts._replace(query=urlencode(query), fragment=""))
    except Exception:
        return url.strip()


def article_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def _parse_rss(xml_text: str, source: str) -> list[dict]:
    items: list[dict] = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        published = None
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                published = None
        items.append({"title": title, "url": link, "published": published, "source": source})
        if len(items) >= MAX_ARTICLES_PER_SOURCE:
            break
    return items


def fetch_articles(session: str) -> list[dict]:
    """
    세션(KR/US)의 RSS 소스 전체 수집 → 24h 필터 → URL 해시 부여.
    반환 항목: {title, url, published, source, article_hash}
    """
    cutoff = datetime.now(UTC) - timedelta(hours=ARTICLE_MAX_AGE_HOURS)
    collected: list[dict] = []
    for source in RSS_SOURCES.get(session, ()):
        try:
            resp = requests.get(
                source, timeout=RSS_TIMEOUT_SEC,
                headers={"User-Agent": "Mozilla/5.0 (rss-reader)"},
            )
            resp.raise_for_status()
            items = _parse_rss(resp.text, source)
        except Exception as exc:
            logger.warning(f"[SCollector] 소스 실패 (건너뜀): {source} | {exc}")
            continue

        for it in items:
            if it["published"] is not None and it["published"] < cutoff:
                continue   # 24h 초과
            it["article_hash"] = article_hash(it["url"])
            collected.append(it)

    # article_hash 기준 중복 제거 (소스 간 동일 기사)
    seen: set[str] = set()
    unique = []
    for it in collected:
        if it["article_hash"] in seen:
            continue
        seen.add(it["article_hash"])
        unique.append(it)

    logger.info(f"[SCollector] {session} 수집 {len(unique)}건 (중복 제거 후)")
    return unique
