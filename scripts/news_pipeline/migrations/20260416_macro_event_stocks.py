"""macro_event_stocks 조인 테이블 신설 + stock_codes_json 데이터 이관."""

from __future__ import annotations

import json

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS macro_event_stocks (
              macro_event_id INTEGER NOT NULL,
              stock_code TEXT NOT NULL,
              PRIMARY KEY (macro_event_id, stock_code),
              FOREIGN KEY (macro_event_id) REFERENCES macro_events(id)
            );
            CREATE INDEX IF NOT EXISTS idx_mes_stock
              ON macro_event_stocks(stock_code);
        """)
        # 기존 JSON 데이터 이관
        rows = conn.execute(
            "SELECT id, stock_codes_json FROM macro_events "
            "WHERE stock_codes_json IS NOT NULL"
        ).fetchall()
        migrated = 0
        for r in rows:
            codes = json.loads(r["stock_codes_json"] or "[]")
            for code in codes:
                if isinstance(code, str) and code.strip():
                    conn.execute(
                        "INSERT OR IGNORE INTO macro_event_stocks"
                        "(macro_event_id, stock_code) VALUES(?,?)",
                        (r["id"], code.strip()),
                    )
                    migrated += 1
        conn.commit()
        print(f"macro_event_stocks migration done ({migrated} rows)")


if __name__ == "__main__":
    migrate()
