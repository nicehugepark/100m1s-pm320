"""6/1~6/4 통합 모델 dailybars 백필 — carded-universe 한계 우회 (단발, 2026-06-05).

ROOT: 5/27 Fix-9 (FLR-20260527-TEC-001) 이후 cron 수집 = carded universe
(daily_picks ∪ intraday_snapshot ∪ 상한가) 한정. 통합 select 의 top-5 후보는
latest_stocks (조건검색식 통과 종목) 거래대금 desc 기준인데, 그 종목 다수가
carded universe 에 부재 → dailybars 0개 → 강세/거래대금음봉 선제거 무력화 +
양봉 판정 봉색결측 = 결측 무력화 PICK (거짓 충실성 FLR-AGT-002 동형).

대상 = 6/1·6/2·6/3·6/4 latest_stocks 합집합 18종목 (main+cron repo 동일).
ka10081 1콜 = 종목당 240+ 영업일 → 과거 10거래일 강세 윈도우 자동 충족.

적재: main + cron repo stocks.db 양쪽 (백테스트 재현 + 라이브 동일 소스).
정책: collect_dailybars.py:629-636 와 동형 8-column UPSERT. 추정/근사 0, raw 만.
키움: 모의 API (mockapi, 만료 2026-07-05). 토큰 사전 검증 PASS (IP/인증/만료).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv

# ── 경로 ──────────────────────────────────────────────────────────────
ROOT = "/Users/seongjinpark/company/100m1s"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
load_dotenv(os.path.join(ROOT, "projects/pm320/poc/.env"))

from news_pipeline.collect_dailybars import (  # noqa: E402
    KIWOOM_BASE,
    _get_token,
    fetch_daily_chart,
)
from news_pipeline.ka10081_helper import parse_ka10081_row  # noqa: E402

_ = (KIWOOM_BASE, _get_token)  # ruff F401 방어 (본문 사용)

REPOS = {
    "main": "/Users/seongjinpark/company/100m1s-homepage/data/stocks.db",
    "cron": "/Users/seongjinpark/company/100m1s-homepage-cron/data/stocks.db",
}
DATES = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]


def target_codes() -> list[str]:
    """6/1~6/4 latest_stocks 합집합 (양쪽 repo 동일하나 안전상 union)."""
    codes: set[str] = set()
    for repo_dir in ("100m1s-homepage", "100m1s-homepage-cron"):
        for d in DATES:
            f = f"/Users/seongjinpark/company/{repo_dir}/data/kiwoom/{d}.json"
            if not os.path.exists(f):
                continue
            with open(f, encoding="utf-8") as fh:
                ls = json.load(fh).get("latest_stocks") or []
            for x in ls:
                t = x.get("ticker") or x.get("code")
                if t and len(str(t)) == 6 and str(t).isdigit():
                    codes.add(str(t))
    return sorted(codes)


def main() -> int:
    # cron(collect_dailybars)과 동일 소스 = 실전 키 (KIWOOM_LIVE_*, api.kiwoom.com).
    # 라이브 정합 + 백테스트 재현 일관성 (모의 토큰으로 실전 endpoint 호출 시 no-data).
    token = _get_token()
    print(f"[backfill] 실전 토큰 OK. base={KIWOOM_BASE}")

    codes = target_codes()
    print(f"[backfill] 대상 {len(codes)}종목: {' '.join(codes)}")

    # 종목 → DailyBar rows (ka10081 1콜, 240+ 영업일). 추정 0, raw 만.
    fetched: dict[str, list[tuple]] = {}
    no_data: list[str] = []
    for i, code in enumerate(codes, 1):
        rows = fetch_daily_chart(code, token)
        if not rows:
            no_data.append(code)
            print(f"  [{i}/{len(codes)}] {code}: no data (skip, 결측은 결측)")
            continue
        bars = []
        for r in rows:
            bar = parse_ka10081_row(r)
            if bar is None:
                continue
            bars.append(
                (
                    code,
                    bar.date,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.trade_amount_won,
                )
            )
        fetched[code] = bars
        print(f"  [{i}/{len(codes)}] {code}: {len(bars)} bars")
        time.sleep(1.0)  # rate limit 방어 (모의 API 429 빈발, 보수적)

    if no_data:
        print(f"[backfill] no-data {len(no_data)}종목: {no_data}")

    # 양쪽 repo UPSERT (collect_dailybars.py:629-636 동형)
    for label, db_path in REPOS.items():
        conn = sqlite3.connect(db_path)
        try:
            total = 0
            for code, bars in fetched.items():
                if not bars:
                    continue
                conn.executemany(
                    """INSERT INTO dailybars
                         (code, date, open, high, low, close, volume, trade_amount)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(code, date) DO UPDATE SET
                         open=excluded.open, high=excluded.high, low=excluded.low,
                         close=excluded.close, volume=excluded.volume,
                         trade_amount=excluded.trade_amount""",
                    bars,
                )
                total += len(bars)
            conn.commit()
            print(f"[backfill] {label} UPSERT 완료: {total} bar-rows ({db_path})")
        finally:
            conn.close()

    # 검증: 6/1~6/4 각 날 대상 종목 dailybars 보유 확인
    for label, db_path in REPOS.items():
        conn = sqlite3.connect(db_path)
        try:
            print(f"[backfill] {label} 검증 (6/1~6/4 종목 수):")
            for d in DATES:
                n = conn.execute(
                    "SELECT COUNT(DISTINCT code) FROM dailybars WHERE date=?", (d,)
                ).fetchone()[0]
                print(f"    {d}: {n}")
        finally:
            conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
