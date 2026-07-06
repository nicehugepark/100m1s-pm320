"""tachikoma_decisions 테이블 — 타치코마 오케스트레이션 판단 학습."""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tachikoma_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              date TEXT NOT NULL,
              task_description TEXT NOT NULL,
              grade TEXT NOT NULL,
              delegation TEXT NOT NULL,
              agent_spawn_mode TEXT,
              prompt_length INTEGER,
              result TEXT NOT NULL DEFAULT 'pending',
              agent_committed INTEGER DEFAULT 0,
              human_correction TEXT,
              lessons_learned TEXT,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_td_date ON tachikoma_decisions(date DESC);
            CREATE INDEX IF NOT EXISTS idx_td_result ON tachikoma_decisions(result);
        """)
        conn.commit()
        print("tachikoma_decisions migration done")


if __name__ == "__main__":
    migrate()
