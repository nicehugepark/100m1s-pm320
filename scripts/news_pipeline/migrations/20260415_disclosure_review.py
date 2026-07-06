"""disclosure_review 테이블 신설 (REQ-20260415-REQ-008).

이시카와/토구사 2단 LLM 루프 판정 결과 누적.
few-shot 학습 재료 + 휴먼 피드백 소스.
"""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS disclosure_review (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              date TEXT NOT NULL,
              rcept_no TEXT NOT NULL,
              agent TEXT NOT NULL,             -- 'ishikawa' | 'togusa'
              raw_title TEXT NOT NULL,
              llm_response TEXT NOT NULL,      -- JSON string
              verdict TEXT NOT NULL,           -- 'good' | 'bad' | 'pending'
              evaluator TEXT,                  -- 'togusa' | 'human' | 'auto'
              evaluation_note TEXT,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_disc_review_date ON disclosure_review(date DESC);
            CREATE INDEX IF NOT EXISTS idx_disc_review_verdict ON disclosure_review(verdict);
            CREATE INDEX IF NOT EXISTS idx_disc_review_rcept ON disclosure_review(rcept_no);
            """
        )
        conn.commit()
        print("disclosure_review migration done")


if __name__ == "__main__":
    migrate()
