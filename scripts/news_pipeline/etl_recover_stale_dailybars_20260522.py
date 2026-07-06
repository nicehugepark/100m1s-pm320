"""5/22 stale dailybars row 정정 ETL — 22종목 본문 본질.

DOC-20260522-FLR critical recurring 본문 본질:
  collect_dailybars 15:30 임계 너무 이름 + already_today skip 봉쇄 → 22종목 중 21종목
  dailybars 5/22 row가 장중/시간외 단일가 시작 전 incomplete OHLC + trde_prica 누적 본질 stale.

본 ETL 본문 본질:
  - 입력: /Users/seongjinpark/company/100m1s-homepage/data/kiwoom/latest.json (fetched_at 16:38:06)
  - 출력: dailybars (5/22 row) + per-stock dailybars JSON (data/dailybars/{code}.json)
  - 정합: latest.json의 ka10081 응답 본문 (open/high/low/close=price/volume/trade_amount) 그대로
  - 본 시점 본문 본질: 22종목 INSERT OR REPLACE (마키나락스 1종 본문 이미 정합 PASS이지만 idempotent).

본 ETL 1회성 사용 본문 (5/22 사고 한정). 후행 재발 봉쇄는 collect_dailybars Fix E-1-V3 + kiwoom_cron.sh
catch-up trigger 본문 본질 force 호출 본문 본질로 영구화.

사용:
  cd /Users/seongjinpark/company/100m1s
  /usr/bin/python3 -m scripts.news_pipeline.etl_recover_stale_dailybars_20260522
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

# HOMEPAGE SoT — config.py 의 HOMEPAGE (env M1S_HOMEPAGE override 가능).
# cron worktree 격리 (lead-meta §11.32) 정합 — 2026-05-28 cycle25 cron-isolation A1 환경변수 일괄.
from .config import HOMEPAGE

LATEST_JSON = HOMEPAGE / "data" / "kiwoom" / "latest.json"
DB_PATH = HOMEPAGE / "data" / "stocks.db"
DAILYBARS_DIR = HOMEPAGE / "data" / "dailybars"
TARGET_DATE = "2026-05-22"


def main() -> int:
    if not LATEST_JSON.exists():
        print(f"[ETL] FAIL: {LATEST_JSON} not found")
        return 1
    if not DB_PATH.exists():
        print(f"[ETL] FAIL: {DB_PATH} not found")
        return 1

    snap = json.loads(LATEST_JSON.read_text())
    fetched_at = snap.get("fetched_at", "")
    snap_date = snap.get("date", "")
    if snap_date != TARGET_DATE:
        print(f"[ETL] FAIL: snapshot date={snap_date} != target={TARGET_DATE}")
        return 1
    stocks = snap.get("stocks", [])
    if not stocks:
        print("[ETL] FAIL: no stocks in snapshot")
        return 1

    print(
        f"[ETL] snapshot date={snap_date} fetched_at={fetched_at} count={len(stocks)}"
    )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    updated_db = 0
    updated_json = 0
    skipped_match = 0
    for s in stocks:
        code = s["ticker"]
        open_p = int(s["open"])
        high_p = int(s["high"])
        low_p = int(s["low"])
        close_p = int(s["price"])  # ka10081 응답 본문: price=종가
        volume = int(s["volume"])
        trade_amount = int(s["trade_amount"])

        # DB 본 시점 row 확인 (idempotent + dry-run safe)
        cur.execute(
            "SELECT open, high, low, close, volume, trade_amount FROM dailybars "
            "WHERE code=? AND date=?",
            (code, TARGET_DATE),
        )
        existing = cur.fetchone()
        if existing:
            ex_ta = existing["trade_amount"]
            if abs(ex_ta - trade_amount) < 1_000_000:
                # 1백만원 이내 match → idempotent skip
                skipped_match += 1
                print(
                    f"  {code} {s['name']:12s} MATCH (db ta={ex_ta:,} ≈ snap ta={trade_amount:,})"
                )
                continue
            print(
                f"  {code} {s['name']:12s} UPDATE: "
                f"db(o={existing['open']},h={existing['high']},l={existing['low']},"
                f"c={existing['close']},v={existing['volume']:,},ta={ex_ta:,}) "
                f"→ snap(o={open_p},h={high_p},l={low_p},c={close_p},v={volume:,},ta={trade_amount:,})"
            )

        # INSERT OR REPLACE 본문 본질 (collect_dailybars 동형)
        cur.execute(
            "INSERT OR REPLACE INTO dailybars "
            "(code, date, open, high, low, close, volume, trade_amount) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, TARGET_DATE, open_p, high_p, low_p, close_p, volume, trade_amount),
        )
        # stocks.last_updated 본문 본질 동시 갱신 (collect_dailybars 동형)
        cur.execute(
            "UPDATE stocks SET last_updated=? WHERE code=?",
            (datetime.now().isoformat(timespec="seconds"), code),
        )
        updated_db += 1

        # per-stock dailybars JSON 본문 본질 갱신 (240영업일 본문, frontend SoT)
        json_path = DAILYBARS_DIR / f"{code}.json"
        if json_path.exists():
            db_json = json.loads(json_path.read_text())
            rows = db_json.get("rows", [])
            if rows and rows[-1].get("d") == TARGET_DATE:
                # 마지막 row 본질 update
                rows[-1] = {
                    "d": TARGET_DATE,
                    "o": open_p,
                    "h": high_p,
                    "l": low_p,
                    "c": close_p,
                    "v": volume,
                    "ta": trade_amount,
                }
            else:
                # row 부재 시 append + actual_days 보정
                rows.append(
                    {
                        "d": TARGET_DATE,
                        "o": open_p,
                        "h": high_p,
                        "l": low_p,
                        "c": close_p,
                        "v": volume,
                        "ta": trade_amount,
                    }
                )
                db_json["actual_days"] = db_json.get("actual_days", 240) + 1
            db_json["rows"] = rows
            db_json["generated_at_date"] = TARGET_DATE
            json_path.write_text(
                json.dumps(db_json, ensure_ascii=False, separators=(",", ":"))
            )
            updated_json += 1

    conn.commit()
    conn.close()
    print(
        f"[ETL] DONE: db_updated={updated_db} json_updated={updated_json} "
        f"skipped_match={skipped_match} total={len(stocks)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
