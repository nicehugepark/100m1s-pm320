"""상한가 종목 backfill — limit-up-trend.json 기반 (REQ-080 §1 union 정합).

배경:
  ka10017 응답에서 일부 우선주(006345 대원전선우 등)가 누락되어
  stock_status_badges + daily_picks 양쪽 모두에서 빠지는 결함 발생.
  backfill_limit_up.py는 daily_picks 기반이므로 daily_picks에도 missing이면 작동 X.

해결:
  homepage data/limit-up-trend.json (정확한 SoT, change_pct 기준 ±29.5% 자동 분류)을
  source로 stock_status_badges + daily_picks 양쪽 동시 backfill.

source 라벨:
  - stock_status_badges: source='backfill_limit_up_trend' (실시간 ka10017 / pipeline_chg와 분리)
  - daily_picks: source='kiwoom' (build_daily union 시점에 자연 흡수)

사용:
  python3 -m scripts.news_pipeline.backfill_limit_up_from_trend                  # 17일 전체
  python3 -m scripts.news_pipeline.backfill_limit_up_from_trend --date 2026-05-04
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.news_pipeline.config import HOMEPAGE  # noqa: E402
from scripts.news_pipeline.db import connect, init_schema  # noqa: E402

SOURCE_BADGE = "backfill_limit_up_trend"
SOURCE_PICKS = "kiwoom"
BADGE_TYPE_LIMIT_UP = "상한가"
TREND_PATH = HOMEPAGE / "data" / "limit-up-trend.json"


def backfill(target_date: str | None = None) -> dict:
    init_schema()
    if not TREND_PATH.exists():
        return {"error": f"TREND not found: {TREND_PATH}"}
    trend = json.loads(TREND_PATH.read_text())
    items = trend.get("items", [])
    summary: dict = {
        "by_date": {},
        "total_badges_inserted": 0,
        "total_picks_inserted": 0,
        "skipped_existing_badges": 0,
        "skipped_existing_picks": 0,
    }
    now = datetime.now().isoformat()

    with connect() as conn:
        for item in items:
            d = item.get("date")
            if not d:
                continue
            if target_date and d != target_date:
                continue
            stocks = item.get("stocks", [])
            for s in stocks:
                code = s.get("code")
                if not code or len(code) != 6 or not code.isdigit():
                    continue
                name = s.get("name", "")
                price = s.get("price")
                chg = s.get("change_pct")
                amt = s.get("trade_amount")
                payload = {
                    "stk_nm": name,
                    "cur_prc": price,
                    "flu_rt": chg,
                    "trde_prica_calc": amt,
                    "estimated": True,
                    "estimation_source": "limit-up-trend.json",
                }
                # 1) stock_status_badges UPSERT (active_until=NULL)
                # 동일 (date, code, badge_type) 의 active row가 *어떤 source라도* 존재하면 skip
                existing_active = conn.execute(
                    """SELECT id, source FROM stock_status_badges
                       WHERE date=? AND stock_code=? AND badge_type=?
                         AND active_until IS NULL""",
                    (d, code, BADGE_TYPE_LIMIT_UP),
                ).fetchone()
                if existing_active:
                    summary["skipped_existing_badges"] += 1
                else:
                    conn.execute(
                        """INSERT INTO stock_status_badges
                             (date, stock_code, badge_type, source, payload_json,
                              active_from, active_until, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
                        (
                            d,
                            code,
                            BADGE_TYPE_LIMIT_UP,
                            SOURCE_BADGE,
                            json.dumps(payload, ensure_ascii=False),
                            now,
                            now,
                        ),
                    )
                    summary["total_badges_inserted"] += 1
                # 2) daily_picks INSERT OR IGNORE (kiwoom source 보존)
                existing_pick = conn.execute(
                    """SELECT id FROM daily_picks
                       WHERE date=? AND stock_code=? AND source=?""",
                    (d, code, SOURCE_PICKS),
                ).fetchone()
                if existing_pick:
                    summary["skipped_existing_picks"] += 1
                else:
                    # rank: 거래대금 큰 순. trend 내 종목은 거래대금 다양하므로
                    # daily_picks 내부 rank는 trade_amount 역순 자연 정렬에 맡김 (기본 0).
                    conn.execute(
                        """INSERT INTO daily_picks
                             (date, stock_code, rank, trade_amount, change_pct,
                              price, open_price, high_price, low_price, source, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            d,
                            code,
                            None,
                            amt,
                            chg,
                            price,
                            s.get("open_price"),
                            s.get("high_price"),
                            s.get("low_price"),
                            SOURCE_PICKS,
                            now,
                        ),
                    )
                    summary["total_picks_inserted"] += 1
                summary["by_date"].setdefault(d, {"badges": 0, "picks": 0})
                if not existing_active:
                    summary["by_date"][d]["badges"] += 1
                if not existing_pick:
                    summary["by_date"][d]["picks"] += 1
        conn.commit()
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--date",
        type=str,
        default=None,
        help="특정 일자만 backfill (YYYY-MM-DD). 미지정 시 trend 전체.",
    )
    args = ap.parse_args()
    result = backfill(target_date=args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
