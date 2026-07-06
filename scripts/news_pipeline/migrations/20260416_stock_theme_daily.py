"""stock_theme_daily 테이블 신설 — 종목-날짜-테마 일별 스냅샷."""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stock_theme_daily (
              date TEXT NOT NULL,
              stock_code TEXT NOT NULL,
              theme_id INTEGER NOT NULL,
              source TEXT DEFAULT 'ishikawa',
              created_at TEXT NOT NULL,
              PRIMARY KEY (date, stock_code, theme_id)
            );
            CREATE INDEX IF NOT EXISTS idx_std_date ON stock_theme_daily(date DESC);
            CREATE INDEX IF NOT EXISTS idx_std_stock ON stock_theme_daily(stock_code);
            CREATE INDEX IF NOT EXISTS idx_std_theme ON stock_theme_daily(theme_id);
        """)
        conn.commit()
        print("stock_theme_daily migration done")


if __name__ == "__main__":
    migrate()
