"""상한가 배지 14일 backfill (REQ-080 §5).

ka10017은 당일 시점 조회만 가능 → 과거 데이터 직접 조회 불가.
폴백: daily_picks.change_pct >= 29.0% 종목을 source='pipeline_chg' 배지로 적재.

⚠ pipeline_chg는 추정 SoT. 실시간 ka10017 적재 결과(source='kiwoom_ka10017')와
   다른 source 라벨로 분리 — 신뢰도 차별화.

사용:
  python3 -m scripts.news_pipeline.backfill_limit_up                    # 14일
  python3 -m scripts.news_pipeline.backfill_limit_up --days 30          # 30일
  python3 -m scripts.news_pipeline.backfill_limit_up --threshold 29.5   # 임계값
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.news_pipeline.db import connect, init_schema  # noqa: E402

SOURCE_PIPELINE = "pipeline_chg"
BADGE_TYPE_LIMIT_UP = "상한가"


def backfill(days: int = 14, threshold: float = 29.0) -> dict:
    init_schema()
    today = datetime.now().date()
    start = today - timedelta(days=days)
    summary: dict = {
        "from": start.strftime("%Y-%m-%d"),
        "to": today.strftime("%Y-%m-%d"),
        "threshold_pct": threshold,
        "by_date": {},
        "total_inserted": 0,
    }
    now = datetime.now().isoformat()
    with connect() as conn:
        rows = conn.execute(
            """SELECT dp.date, dp.stock_code, dp.change_pct, dp.price, dp.trade_amount,
                      s.name AS stk_nm
               FROM daily_picks dp
               LEFT JOIN stocks s ON s.code = dp.stock_code
               WHERE dp.date BETWEEN ? AND ?
                 AND dp.change_pct >= ?
               ORDER BY dp.date, dp.change_pct DESC""",
            (
                start.strftime("%Y-%m-%d"),
                today.strftime("%Y-%m-%d"),
                threshold,
            ),
        ).fetchall()
        for r in rows:
            payload = {
                "stk_nm": r["stk_nm"] or "",
                "cur_prc": r["price"],
                "flu_rt": r["change_pct"],
                "trde_prica_calc": r["trade_amount"],
                "estimated": True,
                "estimation_source": "daily_picks.change_pct",
            }
            # REQ-080 interval 모델 (2026-04-29):
            # pipeline_chg는 영구 활성 (active_until=NULL).
            # 멱등성: 같은 (date, code) 활성 행이 있으면 payload만 갱신.
            existing = conn.execute(
                """SELECT id FROM stock_status_badges
                    WHERE date=? AND stock_code=? AND badge_type=? AND source=?
                      AND active_until IS NULL""",
                (
                    r["date"],
                    r["stock_code"],
                    BADGE_TYPE_LIMIT_UP,
                    SOURCE_PIPELINE,
                ),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE stock_status_badges SET payload_json=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO stock_status_badges
                         (date, stock_code, badge_type, source, payload_json,
                          active_from, active_until, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (
                        r["date"],
                        r["stock_code"],
                        BADGE_TYPE_LIMIT_UP,
                        SOURCE_PIPELINE,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            d = r["date"]
            summary["by_date"][d] = summary["by_date"].get(d, 0) + 1
            summary["total_inserted"] += 1
        conn.commit()
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--threshold", type=float, default=29.0)
    args = ap.parse_args()
    result = backfill(days=args.days, threshold=args.threshold)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
