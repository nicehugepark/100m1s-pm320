"""
키움 ka10080 주식분봉차트 수집 (sparkline 렌더용).

모의투자 사용 (릴리즈 전 개발/테스트는 모의 — feedback_mock_trading_first.md).
키: KIWOOM_APPKEY / KIWOOM_SECRETKEY / KIWOOM_BASE_URL (pm320/poc/.env).

대상: daily_picks(오늘) 50종목.
TR: ka10080, tic_scope="5" (5분봉, 종목당 ~78포인트).
저장: intraday_snapshot(date, stock_code, step_min, open, prices_json, updated_at).
"""

from __future__ import annotations

import json
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
from .kiwoom_client import get_token  # noqa: E402,F401

KIWOOM_BASE = os.environ.get("KIWOOM_BASE_URL", "https://mockapi.kiwoom.com")
KIWOOM_APPKEY = os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_SECRETKEY")

TIC_SCOPE = "5"


def _get_token(session: requests.Session | None = None) -> str:
    # SSOT: kiwoom_client.get_token 위임 (Session keep-alive 보존, 본문 byte-identical).
    return get_token(KIWOOM_BASE, KIWOOM_APPKEY, KIWOOM_SECRETKEY, session=session)


def fetch_minute_chart(
    code: str,
    token: str,
    date_str: str,
    tic_scope: str = TIC_SCOPE,
    session: requests.Session | None = None,
) -> dict | None:
    """ka10080 분봉차트. 당일(date_str)만 오름차순 반환.

    응답은 최신→과거 900rows. cntr_tm(YYYYMMDDHHMMSS)으로 당일만 필터.
    429/rc=5(rate limit) 지수백오프 재시도.
    반환: {"open": int, "prices": [int,...]} or None.
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10080",
    }
    body = {"stk_cd": f"A{code}", "tic_scope": tic_scope, "upd_stkpc_tp": "1"}
    ymd = date_str.replace("-", "")
    poster = session.post if session is not None else requests.post
    for attempt in range(4):
        try:
            r = poster(
                f"{KIWOOM_BASE}/api/dostk/chart",
                json=body,
                headers=headers,
                timeout=20,
            )
        except Exception as e:
            print(f"[collect_intraday] {code} exception: {e}")
            return None
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"[collect_intraday] {code} 429, backoff {back}s")
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(f"[collect_intraday] {code} http {r.status_code}: {r.text[:200]}")
            return None
        try:
            data = r.json()
        except Exception:
            return None
        if data.get("return_code") == 5:
            back = 2 ** (attempt + 1)
            print(f"[collect_intraday] {code} rc=5, backoff {back}s")
            time.sleep(back)
            continue
        if data.get("return_code") != 0:
            return None
        rows = data.get("stk_min_pole_chart_qry") or []
        if not rows:
            return None
        today_rows = [r_ for r_ in rows if (r_.get("cntr_tm") or "")[:8] == ymd]
        if not today_rows:
            return None
        today_rows = list(reversed(today_rows))
        # 대표 지시: 첫 분봉은 시가(open_pric), 나머지는 종가(cur_prc)
        prices: list[int] = []
        day_open: int | None = None
        for idx, row in enumerate(today_rows):
            raw = row.get("open_pric") if idx == 0 else row.get("cur_prc")
            if raw is None:
                raw = row.get("cur_prc")
            try:
                val = abs(int(str(raw).strip()))
            except (ValueError, TypeError, AttributeError):
                continue
            prices.append(val)
            if idx == 0:
                try:
                    day_open = abs(int(str(row.get("open_pric") or raw).strip()))
                except Exception:
                    day_open = val
        if len(prices) < 2:
            return None
        return {"open": day_open or prices[0], "prices": prices}
    return None


def collect(date_str: str | None = None) -> int:
    """daily_picks 종목 분봉 수집 + UPSERT.

    Kiwoom 호출 최적화 (2026-05-26, FLR-20260526-JDG-001 실측 기반):
      ka10080 의 429 가 전체 키움 429 의 ~100% (실측 5/26 181/181). collect_intraday 는
      매 cycle 전 picks 종목의 분봉 full(~39포인트) 을 재fetch 했고 (가드 0건) — 장 마감 후
      ·주말·공휴일에도 동일 frozen 시계열을 반복 재fetch (실측 5/26 32 run × 43 = 2066 콜,
      이 중 ~절반이 마감 후 redundant). build_daily 는 intraday_snapshot 을 date 기준 DB read
      만 하므로 (build_daily.py:2858-2866) 마감 후 재fetch 는 출력에 영향 0.

    최적화:
      1. 마감 후 skip — 거래일 16:00(KRX 시간외 종가 종료, collect_dailybars 와 동일 임계)
         이후이고 해당 종목의 당일 snapshot 이 이미 존재하면 ka10080 호출 생략.
         (force/장중/snapshot 부재 종목은 정상 fetch — 당일 가시 데이터 보장.)
         M1S_FORCE_INTRADAY_RECOLLECT=1 시 skip 비활성화 (catch-up / 수동 정합용).
      2. requests.Session keep-alive — TCP+TLS 핸드셰이크 종목당 1회 → run당 1회로 절감.
    """
    from datetime import datetime as _dt

    from .config import is_market_holiday, pipeline_date

    today = date_str or pipeline_date()
    with connect() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS intraday_snapshot (
                date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                step_min INTEGER NOT NULL,
                open INTEGER NOT NULL,
                prices_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date, stock_code)
            )"""
        )
        rows = c.execute(
            "SELECT DISTINCT stock_code FROM daily_picks WHERE date=?", (today,)
        ).fetchall()
        codes = [r[0] for r in rows if r[0] and len(r[0]) == 6 and r[0].isdigit()]
        # 마감 후 skip 판정용 — 당일 snapshot 이 이미 적재된 종목 집합.
        snap_rows = c.execute(
            "SELECT DISTINCT stock_code FROM intraday_snapshot WHERE date=?", (today,)
        ).fetchall()
        already_snap = {r[0] for r in snap_rows}

    if not codes:
        print("[collect_intraday] 대상 종목 없음")
        return 0

    # 마감 후 skip 게이트 (당일 frozen 시계열 재fetch 회피).
    #   - force_recollect: 무조건 재fetch (catch-up / 수동 정합).
    #   - 거래일 + 16:00 이후 + snapshot 존재: skip (frozen, 출력 영향 0).
    #   - 장중 / 16:00 이전 / snapshot 부재 / 휴장일 picks 잔존: 정상 fetch.
    # 휴장일은 picks 자체가 비거나 전일 잔존 — snapshot 존재 시 동일 skip (frozen).
    force_recollect = os.environ.get("M1S_FORCE_INTRADAY_RECOLLECT", "").strip() == "1"
    now_dt = _dt.now()
    # KRX 시간외 종가 종료 = 16:00 (collect_dailybars market_close_dt 와 단일 임계).
    after_close = now_dt >= now_dt.replace(hour=16, minute=0, second=0, microsecond=0)
    is_holiday = is_market_holiday(today)
    close_skip_active = force_recollect is False and (after_close or is_holiday)

    try:
        session = requests.Session()
        token = _get_token(session)
    except Exception as e:
        print(f"[collect_intraday] token FAIL: {e}")
        return 0

    step_min = int(TIC_SCOPE)
    now_iso = datetime.now().isoformat(timespec="seconds")
    saved = 0
    total_pts = 0
    skipped_frozen = 0
    fetched = 0
    try:
        for i, code in enumerate(codes):
            # 마감 후 frozen skip — 당일 snapshot 존재 종목은 ka10080 호출 생략.
            if close_skip_active and code in already_snap:
                skipped_frozen += 1
                continue
            res = fetch_minute_chart(code, token, today, session=session)
            fetched += 1
            if res is None:
                print(f"  [{i + 1}/{len(codes)}] {code} SKIP")
            else:
                with connect() as c:
                    c.execute(
                        """INSERT INTO intraday_snapshot
                             (date, stock_code, step_min, open, prices_json, updated_at)
                           VALUES (?,?,?,?,?,?)
                           ON CONFLICT(date, stock_code) DO UPDATE SET
                             step_min=excluded.step_min,
                             open=excluded.open,
                             prices_json=excluded.prices_json,
                             updated_at=excluded.updated_at""",
                        (
                            today,
                            code,
                            step_min,
                            res["open"],
                            json.dumps(res["prices"]),
                            now_iso,
                        ),
                    )
                    c.commit()
                saved += 1
                total_pts += len(res["prices"])
                print(f"  [{i + 1}/{len(codes)}] {code} OK pts={len(res['prices'])}")
            # sleep 은 실제 fetch 직후에만 (skip 종목 사이 불필요 지연 제거).
            if i < len(codes) - 1 and res is not None:
                time.sleep(0.5)
    finally:
        session.close()

    avg_len = (total_pts / saved) if saved else 0
    skip_tag = f" / {skipped_frozen} frozen-skip" if skipped_frozen else ""
    print(
        f"[collect_intraday] {saved}/{len(codes)} saved for {today} "
        f"(avg_len={avg_len:.1f}, fetched={fetched}{skip_tag})"
    )
    return saved


if __name__ == "__main__":
    collect()
