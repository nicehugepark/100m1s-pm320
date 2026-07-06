"""daily_picks_old 삭제 — daily_picks에 중복."""

from __future__ import annotations

from ..db import connect


def migrate() -> None:
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS daily_picks_old")
        conn.commit()
        print("dropped daily_picks_old")


if __name__ == "__main__":
    migrate()
