"""disclosures 테이블에 기간/조건 컬럼 추가 (REQ-20260415-REQ-002 B)."""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(disclosures)").fetchall()}
        adds = [
            ("period_start", "TEXT"),
            ("period_end", "TEXT"),
            ("condition_text", "TEXT"),
            ("regulation_period", "TEXT"),
        ]
        for name, typ in adds:
            if name not in cols:
                conn.execute(f"ALTER TABLE disclosures ADD COLUMN {name} {typ}")
                print(f"added disclosures.{name}")
        conn.commit()


if __name__ == "__main__":
    migrate()
