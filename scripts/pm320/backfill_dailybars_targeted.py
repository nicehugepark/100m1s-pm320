#!/usr/bin/env python3
"""PM320 한정 dailybars backfill — ka10081 직접 호출 + cron worktree DB UPSERT.

배경:
  - 2026-05-27~06-02 collect_dailybars cron pipeline 부분 실행 → cron worktree DB 결손
    (5/27: 1546건, 5/28: 77건, 5/29: 50건, 6/01: 27건, 6/02: 21건)
  - 5/21 running 잔존 7종 (440110 파두 + 425040 + 142280 + 477850 + 185490 + 142210 + 373110)
    의 D+3 만기 (5/27) dailybars 부재 → compute_result None → state running 잔존.
  - 대표 verbatim "파두는 즉시 실행" — 본 7종 한정 1회 backfill.

flow:
  1. CLI codes list parse (default: 5/21 running 7종)
  2. cron worktree DB target = ~/company/100m1s-homepage-cron/data/stocks.db
  3. 종목별 ka10081 1회 호출 → 600+행 응답 → dailybars UPSERT (INSERT OR REPLACE)
  4. progress 출력 + 적재 본문 verify

usage:
  python3 scripts/pm320/backfill_dailybars_targeted.py \
    [--codes 440110,425040,142280,477850,185490,142210,373110]

  생략 시 default = 5/21 running 7종.

doc_id: feat(pm320,P0,dailybars,targeted-backfill,FLR-20260527-TEC-001) — 파두 즉시 실행
generated: 2026-06-03 (대표 verbatim 21:11 KST trigger)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# 메인 worktree path import (collect_dailybars + ka10081_helper SSOT 위임)
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# .env load 의무 (KIWOOM_*KEY)
try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass  # python-dotenv 부재 시 env 직접 export 가정

# 본 본문 import 직전 .env load 의무 본 lead 사전 confirm PASS
from scripts.news_pipeline.collect_dailybars import (  # noqa: E402
    _get_token,
    _normalize_row,
    fetch_daily_chart,
)

# cron worktree DB target — 본 wrapper write SoT.
CRON_DB_PATH = Path.home() / "company" / "100m1s-homepage-cron" / "data" / "stocks.db"

# 5/21 running 잔존 7종 (history JSON grep PASS — 본 lead 사전 verify)
DEFAULT_CODES = [
    "440110",  # 파두 (대표 verbatim trigger)
    "425040",  # 티이엠씨
    "142280",  # 녹십자엠에스
    "477850",  # 마키나락스
    "185490",  # 아이진
    "142210",  # 유니트론텍
    "373110",  # 엑셀세라퓨틱스
]


def log(msg: str) -> None:
    print(f"[backfill_dailybars_targeted] {msg}", file=sys.stderr, flush=True)


def upsert_dailybars(db_path: Path, code: str, rows: list[dict]) -> int:
    """ka10081 응답 600+행 → dailybars UPSERT.

    collect_dailybars.collect() L596~L613 본문 verbatim pattern 정합:
      - OHLC 전 동일 + vol==0 = intraday-skip row 봉쇄 (Fix-5)
      - high < max(open,close,low) INVARIANT 위반 raise
      - INSERT OR REPLACE 정합 (UPSERT)
    """
    bars_rows: list[tuple] = []
    skipped_intraday = 0
    for r in rows:
        n = _normalize_row(r)
        if not n:
            continue
        d, o, h, lo, c2, vol, ta = n
        # Fix-5 (2026-05-22): OHLC 전 동일 + vol==0 = intraday skip
        if o > 0 and h > 0 and lo > 0 and c2 > 0 and o == h == lo == c2 and vol == 0:
            skipped_intraday += 1
            continue
        # Invariant: high >= max(open, close, low)
        if h > 0:
            valid_others = [v for v in (o, c2) if v > 0]
            if lo > 0:
                valid_others.append(lo)
            if valid_others and h < max(valid_others):
                raise RuntimeError(
                    f"INVARIANT_VIOLATION dailybars.high < max(o,c,lo): "
                    f"code={code} date={d} o={o} h={h} lo={lo} c={c2}"
                )
        bars_rows.append((code, d, o, h, lo, c2, vol, ta))

    if not bars_rows:
        return 0

    with sqlite3.connect(str(db_path), timeout=30.0) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO dailybars
               (code, date, open, high, low, close, volume, trade_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            bars_rows,
        )
        conn.commit()

    log(f"  {code}: UPSERT {len(bars_rows)} bars (intraday-skip {skipped_intraday})")
    return len(bars_rows)


def verify_target_dates(db_path: Path, code: str, dates: list[str]) -> dict:
    """본 작업 후 target dates 적재 확인 (5/27~5/29 본질)."""
    with sqlite3.connect(str(db_path), timeout=30.0) as conn:
        placeholders = ",".join("?" * len(dates))
        rows = conn.execute(
            f"SELECT date, close FROM dailybars "
            f"WHERE code=? AND date IN ({placeholders}) ORDER BY date",
            (code, *dates),
        ).fetchall()
    return dict(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codes",
        type=str,
        default=",".join(DEFAULT_CODES),
        help=f"Comma-separated code list. Default: {','.join(DEFAULT_CODES)}",
    )
    parser.add_argument(
        "--verify-dates",
        type=str,
        default="2026-05-27,2026-05-28,2026-05-29",
        help="Comma-separated dates for post-backfill verify",
    )
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    verify_dates = [d.strip() for d in args.verify_dates.split(",") if d.strip()]

    if not CRON_DB_PATH.exists():
        log(f"FATAL: cron DB not found: {CRON_DB_PATH}")
        return 2

    log(f"target DB: {CRON_DB_PATH}")
    log(f"codes: {codes}")
    log(f"verify dates: {verify_dates}")

    # kiwoom token
    try:
        token = _get_token()
    except Exception as e:
        log(f"FATAL: token FAIL: {e}")
        return 3

    log(f"kiwoom token acquired ({len(token)} chars)")

    total_saved = 0
    failed: list[str] = []
    verify_results: dict[str, dict] = {}

    for i, code in enumerate(codes):
        log(f"[{i + 1}/{len(codes)}] {code} fetching ka10081...")
        try:
            rows_resp = fetch_daily_chart(code, token)
        except Exception as e:
            log(f"  {code} fetch FAIL: {e}")
            failed.append(code)
            continue

        if not rows_resp:
            log(f"  {code} no data")
            failed.append(code)
            continue

        log(f"  {code} ka10081 response: {len(rows_resp)} rows")

        try:
            saved = upsert_dailybars(CRON_DB_PATH, code, rows_resp)
        except Exception as e:
            log(f"  {code} UPSERT FAIL: {e}")
            failed.append(code)
            continue

        total_saved += saved

        # verify target dates
        verify_results[code] = verify_target_dates(CRON_DB_PATH, code, verify_dates)

        # rate limit 보호
        if i < len(codes) - 1:
            time.sleep(0.3)

    # summary
    log("---SUMMARY---")
    log(f"total bars UPSERT: {total_saved}")
    log(f"failed codes: {failed}")
    log("verify (target dates):")
    for code, dates_map in verify_results.items():
        log(f"  {code}: {dates_map}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
