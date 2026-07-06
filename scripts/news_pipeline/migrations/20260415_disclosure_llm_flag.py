"""disclosures.llm_summarized 컬럼 방어 마이그레이션 (REQ-20260415-REQ-008).

기존 코드가 참조하나 schema.sql에 정의되지 않아 재처리 쿼리 실패.
이미 있으면 no-op.
"""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(disclosures)").fetchall()}
        if "llm_summarized" not in cols:
            conn.execute(
                "ALTER TABLE disclosures ADD COLUMN llm_summarized INTEGER DEFAULT 0"
            )
            conn.commit()
            print("added disclosures.llm_summarized")
        else:
            print("disclosures.llm_summarized already exists")


if __name__ == "__main__":
    migrate()
