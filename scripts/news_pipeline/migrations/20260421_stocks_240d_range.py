"""stocks 테이블에 240영업일 min/max + 발생일 + 등락률 6컬럼 추가.

REQ-20260420-REQ-001 Phase 2.
가용 일수가 240 미만이면 가용 일수 기준 (필드명 그대로).
"""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stocks)").fetchall()}
        adds = [
            ("price_high_240d", "INTEGER"),
            ("price_high_240d_date", "TEXT"),
            ("price_low_240d", "INTEGER"),
            ("price_low_240d_date", "TEXT"),
            ("pct_from_high_240d", "REAL"),
            ("pct_from_low_240d", "REAL"),
        ]
        for name, typ in adds:
            if name not in cols:
                conn.execute(f"ALTER TABLE stocks ADD COLUMN {name} {typ}")
                print(f"added stocks.{name}")
        conn.commit()


if __name__ == "__main__":
    migrate()
