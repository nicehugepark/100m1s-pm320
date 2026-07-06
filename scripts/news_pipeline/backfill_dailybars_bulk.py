"""ka10081 전수 backfill — KOSPI+KOSDAQ 전 종목 dailybars 적재.

2026-05-07 dev-ka10081-bulk-v2 미션. 5/6 조건식(상한가 OR 500억+5%+15일고가) 정정 SQL 재실행 위해
dailybars 5/6 = 47건 → 전수 약 2660건 + volume/trade_amount 채움 필수.

기존 collect_dailybars.py는 daily_picks 종목만 처리하므로 별도 스크립트 분리.
- 대상: stocks WHERE market IN ('KOSPI','KOSDAQ') AND length(code)=6
- 기간: ka10081 1콜 = 600행 → 자동으로 16일 historical + 오늘 모두 받음
- ratelimit: 0.3s sleep (5req/s 안전 마진)
- UPSERT: dailybars (code, date, open, high, low, close, volume, trade_amount)
- trade_amount 단위 변환: ka10081 trde_prica (백만원) → DB 원 단위 (× 1_000_000)
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from pathlib import Path

import requests

# cycle25 env-unification (2026-05-28): 메인 .env 단일 source 일원화.
# 우선순위: shell-level export (kiwoom_cron.sh) > MAIN_ENV setdefault > POC_ENV fallback (deprecated).
MAIN_ENV = Path("/Users/seongjinpark/company/100m1s/.env")
POC_ENV = Path("/Users/seongjinpark/company/100m1s/projects/pm320/poc/.env")
for env_path in (MAIN_ENV, POC_ENV):
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from .db import connect
from .ka10081_helper import DailyBar, parse_int_field, parse_ka10081_row

KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL") or os.environ.get(
    "KIWOOM_BASE_URL", "https://api.kiwoom.com"
)
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)


def _get_token() -> str:
    if not KIWOOM_APPKEY or not KIWOOM_SECRETKEY:
        raise RuntimeError("KIWOOM_APPKEY/SECRETKEY 누락")
    r = requests.post(
        f"{KIWOOM_BASE}/oauth2/token",
        json={
            "grant_type": "client_credentials",
            "appkey": KIWOOM_APPKEY,
            "secretkey": KIWOOM_SECRETKEY,
        },
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"token http {r.status_code}: {r.text[:200]}")
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"token 발급 실패: {data}")
    return token


# _pint → ka10081_helper.parse_int_field 위임 (Q-20260519-CYCLE19-001 SSOT 통합).
# verbatim 복제 봉쇄. 하위 호환 alias.
_pint = parse_int_field


def fetch_daily_chart(code: str, token: str, base_dt: str) -> list[dict] | None:
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10081",
    }
    body = {"stk_cd": f"A{code}", "base_dt": base_dt, "upd_stkpc_tp": "1"}
    for attempt in range(4):
        try:
            r = requests.post(
                f"{KIWOOM_BASE}/api/dostk/chart",
                json=body,
                headers=headers,
                timeout=20,
            )
        except Exception as e:
            print(f"  {code} exception: {e}")
            return None
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"  {code} 429 backoff {back}s")
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(f"  {code} http {r.status_code}: {r.text[:100]}")
            return None
        try:
            data = r.json()
        except Exception:
            return None
        if data.get("return_code") == 5:
            back = 2 ** (attempt + 1)
            print(f"  {code} rc=5 backoff {back}s")
            time.sleep(back)
            continue
        if data.get("return_code") != 0:
            return None
        rows = data.get("stk_dt_pole_chart_qry") or []
        return rows if isinstance(rows, list) else None
    return None


def _normalize_row(row: dict) -> DailyBar | None:
    """ka10081 row → DailyBar (NamedTuple, tuple unpack 호환).

    Q-20260519-CYCLE19-001 (2026-05-19 후행 SSOT 통합):
      collect_dailybars._normalize_row와 verbatim 복제 봉쇄 위해 ka10081_helper.parse_ka10081_row
      단일 출처로 위임. NamedTuple 반환이지만 7-tuple unpack (d, o, h, lo, c2, vol, ta = n) 호환.
    """
    return parse_ka10081_row(row)


def backfill(
    markets: list[str],
    history_days: int = 20,
    sleep_sec: float = 0.3,
    codes_override: list[str] | None = None,
) -> dict:
    base_dt = datetime.now().strftime("%Y%m%d")

    if codes_override:
        codes = list(codes_override)
        print(f"[backfill_bulk] --codes override: {len(codes)}종목")
    else:
        with connect() as c:
            placeholders = ",".join("?" * len(markets))
            rows = c.execute(
                f"SELECT code FROM stocks WHERE market IN ({placeholders}) "
                f"AND length(code)=6 AND code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]' "
                f"ORDER BY code",
                markets,
            ).fetchall()
            codes = [r["code"] for r in rows]

    print(f"[backfill_bulk] 대상 {len(codes)}종목 ({','.join(markets)})")
    print(f"[backfill_bulk] base_dt={base_dt} sleep={sleep_sec}s")

    try:
        token = _get_token()
        print("[backfill_bulk] LIVE 토큰 발급 OK")
    except Exception as e:
        print(f"[backfill_bulk] token FAIL: {e}")
        return {"saved": 0, "failed": 0, "total_codes": len(codes)}

    saved = 0
    failed = 0
    total_bars = 0
    total_skipped_intraday = 0
    t_start = time.time()

    for i, code in enumerate(codes):
        rows_resp = fetch_daily_chart(code, token, base_dt)
        if not rows_resp:
            failed += 1
            if i % 50 == 0:
                print(f"  [{i + 1}/{len(codes)}] {code} FAIL")
            if i < len(codes) - 1:
                time.sleep(sleep_sec)
            continue

        # history_days 기간만 적재 (오늘 포함 직전 N일)
        bars_rows = []
        skipped_intraday = 0
        cnt = 0
        for r in rows_resp:
            n = _normalize_row(r)
            if not n:
                continue
            d, o, h, lo, c2, vol, ta = n
            # Fix (2026-05-20, FLR-20260406-TEC-001 SSOT 비대칭 동형 재발 critical recurring):
            # collect_dailybars.py L334-336 동형 skip 봉쇄. ka10081 응답에서 OHLC 4값이
            # 모두 동일 + vol=0 케이스 (거래정지 종목 stale padding 또는 봉 미확정 시점)
            # INSERT skip. 본 fix 부재 시 94종 × 22거래일 (4/15~5/18) flat o=h=l=c, vol=0
            # 적재 cascade catch (cycle20 dev-daily20-flat-audit 2026-05-20 15:21 KST).
            # 다음 cron에서 키움이 정상 OHLC를 반환하면 INSERT OR REPLACE로 갱신됨.
            if (
                o > 0
                and h > 0
                and lo > 0
                and c2 > 0
                and o == h == lo == c2
                and vol == 0
            ):
                skipped_intraday += 1
                cnt += 1
                if cnt >= history_days:
                    break
                continue
            bars_rows.append((code, d, o, h, lo, c2, vol, ta))
            cnt += 1
            if cnt >= history_days:
                break

        total_skipped_intraday += skipped_intraday

        if bars_rows:
            with connect() as c:
                c.executemany(
                    """INSERT INTO dailybars (code, date, open, high, low, close, volume, trade_amount)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(code, date) DO UPDATE SET
                         open=excluded.open, high=excluded.high, low=excluded.low,
                         close=excluded.close, volume=excluded.volume,
                         trade_amount=excluded.trade_amount""",
                    bars_rows,
                )
                c.commit()
            saved += 1
            total_bars += len(bars_rows)

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(codes) - i - 1) / rate if rate > 0 else 0
            print(
                f"  [{i + 1}/{len(codes)}] saved={saved} failed={failed} "
                f"bars={total_bars} elapsed={elapsed:.0f}s eta={eta:.0f}s"
            )

        if i < len(codes) - 1:
            time.sleep(sleep_sec)

    elapsed = time.time() - t_start
    print(
        f"[backfill_bulk] DONE saved={saved} failed={failed} "
        f"total_bars={total_bars} skipped_intraday={total_skipped_intraday} "
        f"elapsed={elapsed:.0f}s"
    )
    return {
        "saved": saved,
        "failed": failed,
        "total_bars": total_bars,
        "total_codes": len(codes),
        "elapsed_sec": elapsed,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="KOSPI,KOSDAQ")
    ap.add_argument("--history-days", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument(
        "--codes",
        default=None,
        help="comma-separated code override (e.g. 006345,001515) — markets 무시",
    )
    args = ap.parse_args()
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    codes_override = (
        [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None
    )
    backfill(markets, args.history_days, args.sleep, codes_override)
