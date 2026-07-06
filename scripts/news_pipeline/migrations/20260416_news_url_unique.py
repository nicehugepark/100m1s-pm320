"""news 테이블에 (stock_code, url) UNIQUE 제약 추가."""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        # 기존 중복 제거 (최신 1건만 유지)
        conn.execute("""
            DELETE FROM news WHERE rowid NOT IN (
                SELECT MAX(rowid) FROM news GROUP BY stock_code, url
            )
        """)
        deleted = conn.execute("SELECT changes()").fetchone()[0]
        # UNIQUE 인덱스 추가
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_stock_url "
            "ON news(stock_code, url)"
        )
        conn.commit()
        print(f"news dedup: {deleted} rows deleted, UNIQUE index created")


if __name__ == "__main__":
    migrate()
