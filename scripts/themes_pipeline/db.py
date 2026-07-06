"""themes.db 연결 헬퍼 (write 전용). 메인은 read-only ATTACH로만 접근.

LLM 캐시:
- themes.db.llm_cache 테이블은 trackB stocks.db.llm_cache와 컬럼·인덱스 동일.
- call_model_cached() wrapper는 trackB news_pipeline.llm_client.call_model_cached와
  동일 시그니처. 차이: 캐시 lookup/store가 themes.db로 향함 (DB 격리 유지).
- 실제 LLM 호출은 trackB llm_client.call_model 위임 — usage_llm/throttle/retry 인프라 공유.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from scripts.news_pipeline import llm_client

from .config import THEMES_DB_PATH

SCHEMA = Path(__file__).parent / "schema.sql"
logger = logging.getLogger(__name__)


def connect():
    """themes.db write 연결. 테마뉴스 cron 전용."""
    THEMES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(THEMES_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema():
    with connect() as conn:
        conn.executescript(SCHEMA.read_text())
        conn.commit()
    logger.info("themes.db schema initialized at %s", THEMES_DB_PATH)


# ── LLM 캐시 (themes.db.llm_cache) ──
# trackB llm_client._cache_lookup/_cache_store와 동일 동작, DB만 themes.db.

_CACHE_STATS = {"hit": 0, "miss": 0, "store_fail": 0}


def _cache_lookup(domain: str, target_id: str, input_hash: str, model_version: str):
    try:
        with connect() as conn:
            row = conn.execute(
                """SELECT id, response_json FROM llm_cache
                   WHERE domain=? AND target_id=? AND input_hash=? AND model_version=?""",
                (domain, target_id, input_hash, model_version),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE llm_cache SET hit_count = hit_count + 1 WHERE id=?",
                (row["id"],),
            )
            conn.commit()
            return row["response_json"]
    except sqlite3.Error as e:
        logger.debug("themes cache lookup FAIL: %s", e)
        return None


def _cache_store(
    domain: str,
    target_id: str,
    input_hash: str,
    model_version: str,
    response_json: str,
) -> bool:
    try:
        with connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO llm_cache
                   (domain, target_id, input_hash, model_version, response_json)
                   VALUES(?,?,?,?,?)""",
                (domain, target_id, input_hash, model_version, response_json),
            )
            conn.commit()
        return True
    except sqlite3.Error as e:
        logger.warning("themes cache store FAIL %s/%s: %s", domain, target_id, e)
        _CACHE_STATS["store_fail"] += 1
        return False


def call_model_cached(
    prompt: str,
    model: str,
    *,
    domain: str,
    target_id: str,
    input_hash: str,
    agent: str = "themes",
    timeout: int = 60,
    max_retries: int = 2,
    ignore_cache: bool = False,
) -> str | None:
    """themes.db 캐시 적용 LLM 호출. trackB call_model_cached와 동일 시그니처."""
    force_miss = ignore_cache or os.environ.get("IGNORE_CACHE", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if not force_miss:
        cached = _cache_lookup(domain, target_id, input_hash, model)
        if cached is not None:
            _CACHE_STATS["hit"] += 1
            logger.debug("themes cache HIT %s/%s/%s", domain, target_id, model)
            return cached
    _CACHE_STATS["miss"] += 1
    response = llm_client.call_model(
        prompt, model, agent=agent, timeout=timeout, max_retries=max_retries
    )
    if response is not None:
        _cache_store(domain, target_id, input_hash, model, response)
    return response


def cache_stats() -> dict:
    return dict(_CACHE_STATS)


if __name__ == "__main__":
    init_schema()
    print(f"themes.db schema initialized at {THEMES_DB_PATH}")
