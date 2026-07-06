"""stocks.themes_json 컬럼 삭제 — stock_themes로 통일."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        # themes_json 데이터를 stock_themes에 백필 (아직 없는 것만)
        rows = conn.execute(
            "SELECT code, themes_json FROM stocks "
            "WHERE themes_json IS NOT NULL AND themes_json != '[]'"
        ).fetchall()
        backfilled = 0
        for r in rows:
            themes = json.loads(r["themes_json"] or "[]")
            for t in themes:
                theme_row = conn.execute(
                    "SELECT id FROM themes WHERE name=?", (t,)
                ).fetchone()
                if theme_row:
                    conn.execute(
                        "INSERT OR IGNORE INTO stock_themes"
                        "(stock_code, theme_id, date_added, source) "
                        "VALUES(?,?,?,?)",
                        (
                            r["code"],
                            theme_row["id"],
                            datetime.now().strftime("%Y-%m-%d"),
                            "migration",
                        ),
                    )
                    backfilled += 1

        # SQLite 3.35+ 에서만 DROP COLUMN 지원
        ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
        if ver >= (3, 35, 0):
            conn.execute("ALTER TABLE stocks DROP COLUMN themes_json")
            print(f"dropped stocks.themes_json (backfilled {backfilled} links)")
        else:
            print(
                f"SQLite {sqlite3.sqlite_version} — DROP COLUMN 미지원, "
                f"themes_json 유지 (데이터는 stock_themes에 백필 완료: {backfilled}건)"
            )
        conn.commit()


if __name__ == "__main__":
    migrate()
