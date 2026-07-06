"""테마맵 + 매크로 요약 빌드.

- theme_map: 검증된(pass/weak) themes_raw + verdict로 트리 JSON 구성
- macro_summary: 메인 stocks.db.macro_events에서 best-of 추출 → themes.db로 이전
  (메인은 read-only ATTACH로 macro_summary를 다시 참조)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from scripts.news_pipeline.config import DB_PATH as STOCKS_DB_PATH

from .config import THEMES_DB_PATH, pipeline_date
from .db import init_schema


def _connect_themes():
    conn = sqlite3.connect(THEMES_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _attach_stocks_readonly(conn):
    conn.execute(f"ATTACH DATABASE 'file:{STOCKS_DB_PATH}?mode=ro' AS stocks_db")


def _build_tree(conn, date: str) -> dict:
    """themes_raw + themes_verified 결합으로 트리 구성."""
    rows = conn.execute(
        """SELECT r.theme_name, r.parent_theme, r.summary, r.source_count,
                  COALESCE(v.verdict, 'unverified') as verdict
           FROM themes_raw r
           LEFT JOIN themes_verified v
             ON v.date = r.date AND v.theme_name = r.theme_name
           WHERE r.date = ? AND COALESCE(v.verdict, 'unverified') != 'reject'
           ORDER BY r.source_count DESC""",
        (date,),
    ).fetchall()

    tree: dict[str, dict] = {}
    for r in rows:
        parent = (r["parent_theme"] or "").strip() or "(독립)"
        node = tree.setdefault(parent, {"name": parent, "children": []})
        node["children"].append(
            {
                "name": r["theme_name"],
                "summary": r["summary"],
                "source_count": r["source_count"],
                "verdict": r["verdict"],
            }
        )
    return {"date": date, "roots": list(tree.values())}


def _refresh_macro_summary(conn, date: str):
    """stocks.db.macro_events에서 best-of 5건 → themes.db.macro_summary."""
    rows = conn.execute(
        """SELECT keyword, summary, source_count, COALESCE(verified, 0) AS verified
           FROM stocks_db.macro_events
           WHERE date=? AND summary IS NOT NULL
             AND (source IN ('interpret', 'llm') OR summary != keyword)
           ORDER BY
             CASE WHEN source='interpret' THEN 0
                  WHEN source='llm' THEN 1
                  WHEN source='fallback' THEN 3
                  ELSE 2 END,
             source_count DESC
           LIMIT 5""",
        (date,),
    ).fetchall()

    now = datetime.now().isoformat()
    conn.execute("DELETE FROM macro_summary WHERE date=?", (date,))
    for rank, r in enumerate(rows, start=1):
        conn.execute(
            """INSERT INTO macro_summary
               (date, rank, keyword, summary, source_count, verified, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                date,
                rank,
                r["keyword"],
                r["summary"],
                r["source_count"],
                r["verified"],
                now,
            ),
        )
    conn.commit()
    return len(rows)


def build(date: str | None = None):
    init_schema()
    date = date or pipeline_date()

    with _connect_themes() as conn:
        _attach_stocks_readonly(conn)

        tree = _build_tree(conn, date)
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO theme_map (date, tree_json, built_from, created_at)
               VALUES (?, ?, 'themes_verified', ?)
               ON CONFLICT(date) DO UPDATE SET
                 tree_json = excluded.tree_json,
                 built_from = excluded.built_from,
                 created_at = excluded.created_at""",
            (date, json.dumps(tree, ensure_ascii=False), now),
        )
        macro_n = _refresh_macro_summary(conn, date)
        conn.commit()

        roots_n = len(tree["roots"])
        print(f"[build_theme_map] {date} roots={roots_n} macros={macro_n}")
        return {"roots": roots_n, "macros": macro_n}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date")
    args = p.parse_args()
    build(args.date)
