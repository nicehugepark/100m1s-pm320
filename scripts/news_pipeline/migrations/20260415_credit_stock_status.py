"""credit_stock_status 테이블 — kt20017 단건 신용융자 상태.

컬럼:
- credit_allowed (INTEGER): 1=신용융자 가능, 0=회사한도초과/불가
- grade (TEXT): 'A'~'E' 또는 null (불가능인 경우)
- limit_exceeded (INTEGER): credit_allowed의 역. 기존 호환용
- raw_status (TEXT): kt20017 crd_alow_yn 원문 (감사 로그)
"""

from __future__ import annotations

from ..db import connect


def up() -> None:
    with connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS credit_stock_status (
                date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                credit_allowed INTEGER,
                grade TEXT,
                limit_exceeded INTEGER NOT NULL,
                raw_status TEXT,
                PRIMARY KEY (date, stock_code)
            )"""
        )
        # 기존 테이블에 컬럼 증분 추가 (멱등)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(credit_stock_status)").fetchall()
        }
        if "credit_allowed" not in cols:
            conn.execute(
                "ALTER TABLE credit_stock_status ADD COLUMN credit_allowed INTEGER"
            )
        if "grade" not in cols:
            conn.execute("ALTER TABLE credit_stock_status ADD COLUMN grade TEXT")
        # 기존 행의 credit_allowed NULL → limit_exceeded의 역으로 역산
        conn.execute(
            """UPDATE credit_stock_status
               SET credit_allowed = CASE WHEN limit_exceeded=1 THEN 0 ELSE 1 END
               WHERE credit_allowed IS NULL"""
        )
        conn.commit()


if __name__ == "__main__":
    up()
    print("migration applied: credit_stock_status")
