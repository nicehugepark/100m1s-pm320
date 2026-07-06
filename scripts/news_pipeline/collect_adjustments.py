"""권리락 (액면분할/무상증자/주식병합) metadata 적재 — Fix B Phase 2 옵션 C 본문.

DOC-20260514-REQ-002 Fix B + DOC-20260515-DSN-001 (정정 후 옵션 C 채택).
ka10068 옵션 B 환각 catch (cluster v7 결함 19) 후 옵션 C 채택 — 외부 API 의존 0건.

본 모듈 본질:
- (a) dailybars_adjustments table 신설 (운영 SoT `~/company/100m1s-homepage/data/stocks.db`)
- (b) manual seed (cycle3 audit catch 결함 종목 10건 4/9~4/24 권리락 evidence)
- (c) 후행 ETL 자동화 (KRX `kor_split_history` 옵션 A — 외부 source 검증 후 spawn)

schema:
  dailybars_adjustments (
    code TEXT NOT NULL,
    date TEXT NOT NULL,          -- 권리락 발생 거래일 (분할/증자 직후 첫 거래일)
    ratio FLOAT NOT NULL,        -- old_close / new_close (예: 1/2 분할 = 0.5)
    event_type TEXT NOT NULL,    -- 'split' | 'bonus' | 'rev_split' | 'rights'
    source TEXT NOT NULL,        -- 'manual' | 'krx' | 'kiwoom' (옵션 B 폐기)
    created_at TEXT NOT NULL,
    PRIMARY KEY (code, date)
  )

사용처 (Phase 3 후행):
  build_theme_stats.py L771 LAG(close) → close / adjustment_ratio cascade
  heroshik_strict 3 모듈 subquery prev_close 동형 수정
"""

from __future__ import annotations

from datetime import datetime

from .db import connect

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dailybars_adjustments (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  ratio FLOAT NOT NULL,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_dailybars_adjustments_code ON dailybars_adjustments(code);
"""

# cycle3 audit catch 결함 종목 10건 권리락 evidence (manual seed)
# 직접 SQL 검증 PASS (운영 SoT DB):
#   008290 4/17 close=836 → 4/20 close=418 (정확 1/2 분할, ratio=0.5)
#   001380 4/9 close=2200 → 4/10 close=1098 (≈1/2 분할, ratio≈0.499)
# 다른 8 종목은 후행 dev sub-agent 검증 후 추가 (Phase 2-2)
MANUAL_SEED: list[tuple[str, str, float, str, str]] = [
    # (code, date, ratio, event_type, source)
    ("008290", "2026-04-20", 0.5, "split", "manual"),
    ("001380", "2026-04-10", 0.499, "split", "manual"),
]


def ensure_schema(db_path: str | None = None) -> None:
    """dailybars_adjustments table + index 신설 (idempotent)."""
    conn = connect(db_path) if db_path else connect()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def seed_manual(db_path: str | None = None) -> int:
    """결함 종목 manual seed UPSERT. 반환: 적재 row 수."""
    conn = connect(db_path) if db_path else connect()
    try:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        count = 0
        for code, date, ratio, event_type, source in MANUAL_SEED:
            conn.execute(
                """INSERT OR REPLACE INTO dailybars_adjustments
                   (code, date, ratio, event_type, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (code, date, ratio, event_type, source, now),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def main() -> None:
    """본 모듈 단발 실행 — schema 신설 + manual seed."""
    ensure_schema()
    n = seed_manual()
    print(f"dailybars_adjustments: schema ensured + {n} manual seed rows applied")


if __name__ == "__main__":
    main()
