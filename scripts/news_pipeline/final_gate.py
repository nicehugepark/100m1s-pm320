"""휴지(Hugepark) -- 최종 GO/NOGO 게이트. LLM 호출 없음.

토구사 검증 결과를 기반으로 종목별 최종 등급 부여:
  - 적극: strong_bull + 테마 대장
  - 관찰: weak_bull / neutral
  - 주의: weak_bear
  - nogo: strong_bear

NOGO 비율이 50% 초과 시 owner_alerts에 시장 경고 삽입.
"""

from datetime import datetime

from .config import pipeline_date
from .db import connect


def gate_check(target_date=None):
    """일일 최종 게이트 판정."""
    if not target_date:
        target_date = pipeline_date()

    with connect() as conn:
        # 테이블 생성 (안전)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hugepark_gate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL, stock_code TEXT NOT NULL,
                grade TEXT NOT NULL, reason TEXT,
                source TEXT DEFAULT 'hugepark', created_at TEXT NOT NULL,
                UNIQUE(date, stock_code)
            );
            CREATE TABLE IF NOT EXISTS owner_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL, level TEXT NOT NULL,
                message TEXT NOT NULL, acknowledged INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )

        verdicts = conn.execute(
            "SELECT * FROM togusa_verdicts WHERE date=?", (target_date,)
        ).fetchall()

        if not verdicts:
            print(f"[hugepark] {target_date} 토구사 검증 결과 없음 — 스킵")
            return

        go_count = 0
        nogo_count = 0
        now = datetime.now().isoformat()

        for v in verdicts:
            verdict = v["verdict"]
            code = v["stock_code"]

            if verdict == "strong_bear":
                grade = "nogo"
                reason = "토구사 strong_bear 판정"
                nogo_count += 1
            elif verdict == "weak_bear":
                grade = "주의"
                reason = "토구사 weak_bear — 관찰 필요"
            elif verdict == "neutral":
                grade = "관찰"
                reason = "경고 다수"
            elif verdict == "strong_bull" and v["is_theme_leader"]:
                grade = "적극"
                reason = "테마 대장 + 강한 호재"
                go_count += 1
            else:
                grade = "관찰"
                reason = f"verdict={verdict}, leader={v['is_theme_leader']}"
                go_count += 1

            conn.execute(
                """INSERT OR REPLACE INTO hugepark_gate
                   (date, stock_code, grade, reason, source, created_at)
                   VALUES (?, ?, ?, ?, 'hugepark', ?)""",
                (target_date, code, grade, reason, now),
            )

        conn.commit()

        print(f"[hugepark] {target_date} GO={go_count} NOGO={nogo_count}")

        # 알림 조건: NOGO 비율 50% 초과
        if nogo_count > len(verdicts) * 0.5:
            conn.execute(
                "INSERT INTO owner_alerts (date, level, message, created_at) VALUES (?, 'warn', ?, ?)",
                (
                    target_date,
                    f"NOGO 비율 {nogo_count}/{len(verdicts)} — 시장 주의",
                    now,
                ),
            )
            conn.commit()


if __name__ == "__main__":
    import sys

    td = sys.argv[1] if len(sys.argv) > 1 else None
    gate_check(td)
