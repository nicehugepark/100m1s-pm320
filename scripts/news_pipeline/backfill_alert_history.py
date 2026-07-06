"""disclosures → stock_alert_history 백필 (임무 D).

투자주의/경고/위험/단기과열 공시를 스캔하여 이벤트 타입별 분류:
  - title에 '지정예고' 또는 '(예고)' → event_type='notice'
  - title에 '매매거래 정지 및 재개' → event_type='released' (해제)
  - 기타 '지정' → event_type='designated'

PRIMARY KEY (stock_code, date, stage, event_type) 충돌 시 INSERT OR IGNORE.

사용:
  python3 -m scripts.news_pipeline.backfill_alert_history          # 전체 백필
  python3 -m scripts.news_pipeline.backfill_alert_history --since 2026-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 이 스크립트를 단독 실행할 때도 패키지 import가 가능하도록 경로 보정
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.news_pipeline.db import connect, init_schema  # noqa: E402

TRACKED_CATS = ("투자주의", "투자경고", "투자위험", "단기과열")


def classify_event(title: str) -> str:
    """title → event_type.

    우선순위:
      1) '매매거래 정지 및 재개' → released
      2) '지정예고' 또는 '(예고)' → notice
      3) 기타 → designated
    """
    t = title or ""
    if "매매거래 정지 및 재개" in t:
        return "released"
    if "지정예고" in t or "(예고)" in t:
        return "notice"
    return "designated"


def backfill(since: str | None = None) -> dict:
    """disclosures 전수 스캔 → stock_alert_history 적재.

    Returns:
        {"scanned": int, "inserted": int, "skipped": int, "by_stage": {...}}
    """
    init_schema()  # 테이블·인덱스 보장
    with connect() as conn:
        # 스캔
        if since:
            rows = conn.execute(
                """SELECT id, stock_code, date, title, disclosure_cat,
                          period_start, period_end
                   FROM disclosures
                   WHERE disclosure_cat IN (?, ?, ?, ?) AND date >= ?
                   ORDER BY date""",
                (*TRACKED_CATS, since),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, stock_code, date, title, disclosure_cat,
                          period_start, period_end
                   FROM disclosures
                   WHERE disclosure_cat IN (?, ?, ?, ?)
                   ORDER BY date""",
                TRACKED_CATS,
            ).fetchall()

        inserted = 0
        skipped = 0
        by_stage: dict[str, int] = {}
        by_event: dict[str, int] = {}

        for r in rows:
            event_type = classify_event(r["title"])
            stage = r["disclosure_cat"]
            # INSERT OR IGNORE — PK 충돌 시 skip
            cur = conn.execute(
                """INSERT OR IGNORE INTO stock_alert_history
                   (stock_code, date, stage, event_type, source,
                    raw_disclosure_id, period_start, period_end)
                   VALUES (?, ?, ?, ?, 'disclosure', ?, ?, ?)""",
                (
                    r["stock_code"],
                    r["date"],
                    stage,
                    event_type,
                    r["id"],
                    r["period_start"],
                    r["period_end"],
                ),
            )
            if cur.rowcount:
                inserted += 1
                by_stage[stage] = by_stage.get(stage, 0) + 1
                by_event[event_type] = by_event.get(event_type, 0) + 1
            else:
                skipped += 1

        conn.commit()

        # 커버리지 요약
        date_range = conn.execute(
            "SELECT MIN(date) as min_d, MAX(date) as max_d, COUNT(*) as total "
            "FROM stock_alert_history"
        ).fetchone()

        return {
            "scanned": len(rows),
            "inserted": inserted,
            "skipped_duplicates": skipped,
            "by_stage": by_stage,
            "by_event": by_event,
            "coverage": {
                "min_date": date_range["min_d"],
                "max_date": date_range["max_d"],
                "total_records": date_range["total"],
            },
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD 이후만 스캔 (기본: 전체)")
    args = ap.parse_args()

    result = backfill(since=args.since)
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
