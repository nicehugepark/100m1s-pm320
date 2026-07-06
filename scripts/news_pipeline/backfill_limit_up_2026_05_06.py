"""5/6 limit-up-trend.json 가격/등락률/거래대금 backfill — 단발 P0.

5/6 cron partial 실행으로 상한가 종목 list (15건) 만 적재되고 가격 fields 모두 null.
키움 ka10081 일봉차트 600행 응답에서 5/6 row 추출 + dailybars 보강 +
limit-up-trend.json 5/6 entry 보강.

실행:
  python3 -m scripts.news_pipeline.backfill_limit_up_2026_05_06
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# cycle25 env-unification (2026-05-28): 메인 .env 단일 source 일원화.
# 우선순위: shell-level export (kiwoom_cron.sh) > MAIN_ENV setdefault > POC_ENV fallback (deprecated).
# S5 자립화 (DOC-20260707-REQ-001): 메인 레포 절대경로 → env(M1S_COMPANY) 우선 + pm320 레포 로컬 fallback.
_M1S_COMPANY = Path(os.environ.get("M1S_COMPANY", str(Path(__file__).resolve().parents[2])))
MAIN_ENV = _M1S_COMPANY / ".env"
POC_ENV = _M1S_COMPANY / "scripts" / "news_pipeline" / ".env"
for env_path in (MAIN_ENV, POC_ENV):
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.news_pipeline.db import connect  # noqa: E402

KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL", "https://api.kiwoom.com")
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)

TARGET_DATE = "2026-05-06"
TARGET_DATE_RAW = "20260506"
# HOMEPAGE_DATA SoT — config.py 의 DATA_DIR (env M1S_HOMEPAGE override 가능).
# cron worktree 격리 (lead-meta §11.32) 정합 — 2026-05-28 cycle25 cron-isolation A1 환경변수 일괄.
from .config import DATA_DIR as HOMEPAGE_DATA

LIMIT_UP_JSON = HOMEPAGE_DATA / "limit-up-trend.json"


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
    token = r.json().get("token")
    if not token:
        raise RuntimeError("token 발급 실패")
    return token


def _pint(val) -> int:
    if val is None:
        return 0
    s = str(val).replace(",", "").replace("+", "").strip()
    if not s:
        return 0
    neg = s.startswith("-")
    s = s.lstrip("-").lstrip("0") or "0"
    try:
        return -int(s) if neg else int(s)
    except ValueError:
        return 0


def fetch_5_6_row(code: str, token: str) -> dict | None:
    """ka10081 호출 → 5/6 일봉 row 반환."""
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10081",
    }
    body = {
        "stk_cd": f"A{code}",
        "base_dt": "20260507",
        "upd_stkpc_tp": "1",
    }
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
            print(f"  {code} 429, backoff {back}s")
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(f"  {code} http {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        if data.get("return_code") == 5:
            back = 2 ** (attempt + 1)
            print(f"  {code} rc=5, backoff {back}s")
            time.sleep(back)
            continue
        if data.get("return_code") != 0:
            print(f"  {code} rc={data.get('return_code')}")
            return None
        rows = data.get("stk_dt_pole_chart_qry", [])
        for row in rows:
            if str(row.get("dt", "")).strip() == TARGET_DATE_RAW:
                return row
        print(f"  {code} 5/6 row 없음 (rows={len(rows)})")
        return None
    return None


def main() -> int:
    # 1) limit-up-trend.json 5/6 entry 로드
    data = json.loads(LIMIT_UP_JSON.read_text())
    target_entry = next((it for it in data["items"] if it["date"] == TARGET_DATE), None)
    if not target_entry:
        print("ERR 5/6 entry 없음")
        return 1
    codes = [(st["code"], st["name"]) for st in target_entry["stocks"]]
    print(f"target {len(codes)} stocks for {TARGET_DATE}")

    # 2) 키움 token
    token = _get_token()
    print("token ok")

    # 3) 종목별 fetch + 5/6 row 추출
    results: dict[str, dict] = {}
    for i, (code, name) in enumerate(codes):
        row = fetch_5_6_row(code, token)
        if not row:
            print(f"  [{i + 1}/{len(codes)}] {code} {name} FAIL")
            time.sleep(0.4)
            continue
        cur_prc = abs(_pint(row.get("cur_prc")))
        open_v = abs(_pint(row.get("open_pric")))
        high_v = abs(_pint(row.get("high_pric")))
        low_v = abs(_pint(row.get("low_pric")))
        pred_pre = _pint(row.get("pred_pre"))  # 부호 보존
        trde_prica_mn = abs(_pint(row.get("trde_prica")))  # 백만원 단위
        # change_pct = pred_pre / (cur_prc - pred_pre) × 100
        prev_close = cur_prc - pred_pre
        if prev_close <= 0:
            change_pct = None
        else:
            change_pct = round(pred_pre / prev_close * 100, 2)
        trade_amount = trde_prica_mn * 1_000_000  # 백만원 → 원
        results[code] = {
            "change_pct": change_pct,
            "trade_amount": trade_amount,
            "price": cur_prc,
            "open_price": float(open_v),
            "high_price": float(high_v),
            "low_price": float(low_v),
            "_for_db": (code, TARGET_DATE, open_v, high_v, low_v, cur_prc),
        }
        print(
            f"  [{i + 1}/{len(codes)}] {code} {name} OK "
            f"close={cur_prc} change={change_pct}% trade_amount={trade_amount:,}"
        )
        time.sleep(0.4)

    print(f"fetched {len(results)} / {len(codes)}")
    if not results:
        print("ERR 0 fetched")
        return 1

    # 4) dailybars 보강 (Invariant: high >= max(open, low, close))
    bars_rows = []
    for code, r in results.items():
        c, d, o, h, lo, cl = r["_for_db"]
        if h > 0:
            valid_others = [v for v in (o, cl) if v > 0]
            if lo > 0:
                valid_others.append(lo)
            if valid_others and h < max(valid_others):
                print(f"  WARN {code} INVARIANT high<max(o,c,lo) skip dailybars")
                continue
        bars_rows.append((c, d, o, h, lo, cl))
    if bars_rows:
        with connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO dailybars (code, date, open, high, low, close)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                bars_rows,
            )
            conn.commit()
        print(f"dailybars upsert {len(bars_rows)}")

    # 5) limit-up-trend.json 5/6 entry 패치
    for st in target_entry["stocks"]:
        code = st["code"]
        if code in results:
            r = results[code]
            st["change_pct"] = r["change_pct"]
            st["trade_amount"] = r["trade_amount"]
            st["price"] = r["price"]
            st["open_price"] = r["open_price"]
            st["high_price"] = r["high_price"]
            st["low_price"] = r["low_price"]

    data["generated_at"] = datetime.now().isoformat(timespec="microseconds")
    LIMIT_UP_JSON.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    print(f"wrote {LIMIT_UP_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
