"""신규 daily_picks 종목 자동 마스터 등록 + 240일 backfill 트리거 (P0 본질 fix 2026-05-11).

trigger 시점:
  pipeline.sh 내 collect_intraday / collect_dailybars 호출 **직전**.

동작:
  1) 오늘 daily_picks 중 stocks 마스터에 없는 코드 탐색
  2) seed_codes(...)로 KIND lookup + stocks UPSERT
  3) backfill_dailybars_bulk(--codes ..., history_days=240) 실행
  4) 결과 로그 (없으면 즉시 종료)

이 스크립트가 없으면:
  - collect_dailybars.py 의 UPDATE stocks SET ... WHERE code=? 가 0 rows 처리
    (silently skip) → price_high_240d/pct_from_high_240d 영구 NULL
  - frontend range_240d.high 가 "--" 로 렌더 (FLR-20260511 root cause B)

근거:
  사이클 5/11 P0 catch (4건). dev-hotfix-collect-5-11 가 439960 단발 수동 처리했으나
  hook 없으면 차회 신규 종목 진입 시 동일 결함 재발. 영구 hook 으로 차단.
"""

from __future__ import annotations

import argparse
import sys

from .config import pipeline_date
from .db import connect


def find_missing_master_codes(target_date: str) -> list[str]:
    """오늘 daily_picks 중 stocks 마스터에 없는 종목 코드 반환."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT dp.stock_code
                 FROM daily_picks dp
                 LEFT JOIN stocks s ON s.code = dp.stock_code
                WHERE dp.date = ?
                  AND s.code IS NULL
                  AND length(dp.stock_code) = 6
                  AND dp.stock_code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'
                ORDER BY dp.stock_code""",
            (target_date,),
        ).fetchall()
    return [r[0] for r in rows]


def run(target_date: str | None = None, history_days: int = 240) -> dict:
    today = target_date or pipeline_date()
    missing = find_missing_master_codes(today)
    if not missing:
        print(f"[seed_master_for_today] {today} 마스터 누락 종목 없음")
        return {"date": today, "missing": [], "seeded": 0, "backfilled": 0}

    print(f"[seed_master_for_today] {today} 마스터 누락 {len(missing)}종목: {missing}")

    # 1) seed_master --codes 로 stocks UPSERT
    from .seed_master import seed_codes

    seeded = seed_codes(missing)

    # 2) backfill_dailybars_bulk --codes 240d 백필
    from .backfill_dailybars_bulk import backfill

    result = backfill(
        markets=["KOSPI", "KOSDAQ"], history_days=history_days, codes_override=missing
    )
    backfilled = result.get("saved", 0)

    print(
        f"[seed_master_for_today] {today} done: missing={len(missing)} "
        f"seeded={seeded} backfilled={backfilled}"
    )
    return {
        "date": today,
        "missing": missing,
        "seeded": seeded,
        "backfilled": backfilled,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--history-days", type=int, default=240)
    args = ap.parse_args()
    result = run(args.date, args.history_days)
    sys.exit(0 if result is not None else 1)
