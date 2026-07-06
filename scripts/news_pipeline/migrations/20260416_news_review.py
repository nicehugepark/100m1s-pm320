"""news_review 테이블 신설 (DOC-20260416-REQ-001)."""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS news_review (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              date TEXT NOT NULL,
              stock_code TEXT NOT NULL,
              agent TEXT NOT NULL,
              news_titles TEXT NOT NULL,
              llm_response TEXT NOT NULL,
              verdict TEXT NOT NULL,
              evaluator TEXT,
              evaluation_note TEXT,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_news_review_date ON news_review(date DESC);
            CREATE INDEX IF NOT EXISTS idx_news_review_verdict ON news_review(verdict);
        """)
        conn.commit()
        print("news_review migration done")


if __name__ == "__main__":
    migrate()
