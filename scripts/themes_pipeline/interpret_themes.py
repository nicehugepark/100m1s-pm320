"""이시카와 1차 — 테마 해석.

입력: 메인 stocks.db의 news 테이블 (read-only ATTACH).
출력: themes.db.themes_raw (UPSERT, dedupe by input_hash).

주의:
- 메인 DB는 read-only ATTACH (write 절대 금지)
- 모든 LLM 호출은 themes_pipeline.db.call_model_cached (themes.db.llm_cache,
  실 호출은 trackB news_pipeline.llm_client.call_model 위임)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from scripts.news_pipeline.config import DB_PATH as STOCKS_DB_PATH
from scripts.news_pipeline.llm_client import ISHIKAWA_MODEL, hash_input  # noqa: F401

from .config import (  # noqa: F401
    THEMES_DB_PATH,
    parse_llm_json_array,
    pipeline_date,
)
from .db import call_model_cached, init_schema

MAX_NEWS_FOR_LLM = 80

PROMPT = """한국 주식 테마 분석(이시카와). JSON 배열만.

## 뉴스 제목
{news_block}

## 원칙
- 인과 사슬 필수: "이슈→방향성→파생 테마→수혜 섹터" 4단
- 교차검증: 2+ 독립 매체=confirmed
- 가비지 필터: VI발동·신고가·[강세 토픽]·[특징주] 로봇기사 무시
- 테마=매수 이유. IR/홍보/공시 제외. "왜 돈이 모이나?"
- 테마명 정규화: 동일 테마=동일 표현 (예: "AI 인프라" 고정, "AI인프라"/"인공지능 인프라" 금지)

## 출력
[
  {{"theme_name": "정규화된 테마명", "parent_theme": "상위 테마|빈 문자열", "summary": "사건→영향→섹터 1문장", "source_count": 정수, "evidence": ["뉴스 제목1","뉴스 제목2"]}}
]"""


def _connect_themes():
    conn = sqlite3.connect(THEMES_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _attach_stocks_readonly(conn):
    """메인 stocks.db read-only ATTACH. write 발생 시 SQLite가 차단."""
    conn.execute(f"ATTACH DATABASE 'file:{STOCKS_DB_PATH}?mode=ro' AS stocks_db")


def _fetch_recent_news(conn, date: str, hours: int = 24):
    since = (datetime.fromisoformat(date) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        """SELECT id, title, source FROM stocks_db.news
           WHERE collected_at >= ? AND COALESCE(is_robot, 0) = 0
           ORDER BY published_at DESC
           LIMIT ?""",
        (since, MAX_NEWS_FOR_LLM),
    ).fetchall()
    return [dict(r) for r in rows]


def _persist_raw(
    conn, date: str, source_kind: str, ihash: str, themes: list, model_version: str
):
    now = datetime.now().isoformat()
    inserted = 0
    for t in themes:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO themes_raw
                   (date, theme_name, parent_theme, summary, source_count,
                    source_kind, input_hash, evidence_json, model_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date,
                    t.get("theme_name", "").strip(),
                    t.get("parent_theme", "").strip(),
                    t.get("summary", "").strip(),
                    int(t.get("source_count", 0) or 0),
                    source_kind,
                    ihash,
                    json.dumps(t.get("evidence", []), ensure_ascii=False),
                    model_version,
                    now,
                ),
            )
            inserted += conn.total_changes
        except sqlite3.Error as exc:
            print(f"[interpret_themes] persist FAIL: {exc}")
    conn.commit()
    return inserted


def interpret(date: str | None = None, ignore_cache: bool = False):
    init_schema()
    date = date or pipeline_date()

    with _connect_themes() as conn:
        _attach_stocks_readonly(conn)
        news_rows = _fetch_recent_news(conn, date)

        if not news_rows:
            print("[interpret_themes] no news to analyze")
            return []

        ids = sorted(r["id"] for r in news_rows)
        payload = {"news_ids": ids, "date": date, "kind": "rss"}
        ihash = hash_input(payload)

        lines = [f"- {r['title']} ({r['source']})" for r in news_rows]
        prompt = PROMPT.format(news_block="\n".join(lines))

        response = call_model_cached(
            prompt,
            ISHIKAWA_MODEL,
            domain="theme_extract",
            target_id=f"{date}:rss",
            input_hash=ihash,
            agent="themes:ishikawa",
            ignore_cache=ignore_cache,
        )

        if response is None:
            print("[interpret_themes] LLM FAIL")
            return []

        themes = parse_llm_json_array(response)
        if themes is None:
            print(
                f"[interpret_themes] JSON parse FAIL — first 200ch: {response[:200]!r}"
            )
            return []

        _persist_raw(conn, date, "rss", ihash, themes, ISHIKAWA_MODEL)
        print(f"[interpret_themes] {date} themes={len(themes)}")
        return themes


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date")
    p.add_argument("--ignore-cache", action="store_true")
    args = p.parse_args()
    interpret(args.date, args.ignore_cache)
