"""키움 ka20006 업종일봉 수집 — KOSPI/KOSDAQ 종합지수 (임무 C).

REQ: 투자경고·투자위험 predicted 경로의 `index_multiple` 조건(지수 등락률 대비 배수)
계산을 위해 KOSPI/KOSDAQ 일별 지수 시계열을 `index_dailybars`에 적재.

TR ID: ka20006 (업종일봉조회요청)
inds_cd:
  - 001 = KOSPI 종합지수
  - 101 = KOSDAQ 종합지수
엔드포인트: POST /api/dostk/chart
응답 키: inds_dt_pole_qry
응답 필드: cur_prc, trde_qty, dt, open_pric, high_pric, low_pric, trde_prica

스케일 보정:
  실제 KOSPI 2026-04-22 종가 = 6,417.93 ↔ 키움 cur_prc = '641793' (정수).
  즉 응답값은 **실제 지수 × 100** 스케일 — 저장 시 /100 (REAL).

사용:
  python3 -m scripts.news_pipeline.collect_kiwoom_indices          # KOSPI+KOSDAQ 수집
  python3 -m scripts.news_pipeline.collect_kiwoom_indices --backfill 30   # 30일 백필
"""

from __future__ import annotations

import argparse
import os
import sys
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

# 패키지 import 경로 보정 (단독 실행 시)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.news_pipeline.db import connect, init_schema  # noqa: E402
from scripts.news_pipeline.kiwoom_client import get_token  # noqa: E402,F401

KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL") or os.environ.get(
    "KIWOOM_BASE_URL", "https://api.kiwoom.com"
)
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)

# 수집 대상 (index_code, 지수명, inds_cd)
INDEX_TARGETS = [
    ("KOSPI", "KOSPI 종합지수", "001"),
    ("KOSDAQ", "KOSDAQ 종합지수", "101"),
]

# 키움 응답값은 실지수 × 100 (실측 검증: KOSPI 641793 ↔ 실제 6417.93)
# 교차검증 (2026-04-24 Google Finance + Wikipedia):
#   KOSPI 2026-04-22 종가 = 6,417.93  (키움 raw 641793 / 100)
#   KOSPI 2026-04-23 종가 = 6,475.81  (사상최고)
#   KOSPI 2026-04-24 현재 = 6,461.44 @ 10:07 KST
# → /100 스케일 확정. FLR-20260406-TEC-001(/1000 오인) 재발 방지.
SCALE_DIVISOR = 100.0

# 스케일 sanity check 범위 (2026-04 현재). 벗어나면 raise.
# 미래 지수 폭등/폭락에 대비한 느슨한 범위 (0.3× ~ 3×).
SANITY_RANGE = {
    "KOSPI": (1800.0, 20000.0),
    "KOSDAQ": (400.0, 5000.0),
}


def _get_token() -> str:
    # SSOT: kiwoom_client.get_token 위임 (본문 byte-identical, 기존 RuntimeError 문구 보존).
    return get_token(KIWOOM_BASE, KIWOOM_APPKEY, KIWOOM_SECRETKEY)


def _parse_int(val) -> int | None:
    """키움 응답 숫자 파싱 (부호·콤마·패딩 제거)."""
    if val is None:
        return None
    s = str(val).replace(",", "").replace("+", "").strip()
    if not s:
        return None
    neg = s.startswith("-")
    s = s.lstrip("-").lstrip("0") or "0"
    try:
        n = int(s)
        return -n if neg else n
    except ValueError:
        return None


def fetch_index_daily(token: str, inds_cd: str, base_dt: str) -> list[dict]:
    """ka20006 업종일봉 호출. cont-yn 연속조회 1페이지(~600행)면 30일 백필 충분."""
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka20006",
    }
    body = {"inds_cd": inds_cd, "base_dt": base_dt}
    for attempt in range(4):
        try:
            r = requests.post(
                f"{KIWOOM_BASE}/api/dostk/chart",
                json=body,
                headers=headers,
                timeout=20,
            )
        except Exception as e:
            print(f"[indices {inds_cd}] exception: {e}")
            return []
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"[indices {inds_cd}] 429, backoff {back}s")
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(f"[indices {inds_cd}] http {r.status_code}: {r.text[:200]}")
            return []
        try:
            data = r.json()
        except Exception:
            return []
        if data.get("return_code") == 5:
            back = 2 ** (attempt + 1)
            print(f"[indices {inds_cd}] rc=5, backoff {back}s")
            time.sleep(back)
            continue
        if data.get("return_code") != 0:
            print(
                f"[indices {inds_cd}] rc={data.get('return_code')} msg={data.get('return_msg')}"
            )
            return []
        rows = data.get("inds_dt_pole_qry") or []
        return rows if isinstance(rows, list) else []
    return []


def parse_row(raw: dict) -> dict | None:
    """응답 1행 → index_dailybars 1행 dict. 스케일 보정 포함."""
    dt = str(raw.get("dt") or "").strip()
    if not dt or len(dt) != 8:
        return None
    date_str = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"
    close_raw = _parse_int(raw.get("cur_prc"))
    open_raw = _parse_int(raw.get("open_pric"))
    high_raw = _parse_int(raw.get("high_pric"))
    low_raw = _parse_int(raw.get("low_pric"))
    volume = _parse_int(raw.get("trde_qty"))
    if close_raw is None:
        return None
    return {
        "date": date_str,
        "open": (open_raw / SCALE_DIVISOR) if open_raw is not None else None,
        "high": (high_raw / SCALE_DIVISOR) if high_raw is not None else None,
        "low": (low_raw / SCALE_DIVISOR) if low_raw is not None else None,
        "close": close_raw / SCALE_DIVISOR,
        "volume": volume,
    }


def upsert_rows(conn, index_code: str, rows: list[dict]) -> int:
    """index_dailybars UPSERT. PK (index_code, date)."""
    inserted = 0
    for row in rows:
        conn.execute(
            """INSERT INTO index_dailybars(index_code, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(index_code, date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume""",
            (
                index_code,
                row["date"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def collect(limit_days: int | None = None) -> dict:
    """KOSPI + KOSDAQ 수집 → index_dailybars 적재.

    Args:
        limit_days: 최근 N일만 적재 (None=전체 600행).
    """
    init_schema()  # index_dailybars 테이블 보장
    token = _get_token()
    base_dt = datetime.now().strftime("%Y%m%d")

    summary: dict = {"by_index": {}}
    with connect() as conn:
        for index_code, name, inds_cd in INDEX_TARGETS:
            raw_rows = fetch_index_daily(token, inds_cd, base_dt)
            parsed = [r for r in (parse_row(x) for x in raw_rows) if r]
            # 최근 N일로 자르기
            if limit_days and len(parsed) > limit_days:
                parsed = parsed[:limit_days]

            # Sanity check: 최신 1건 종가가 예상 범위 밖이면 스케일 오류 의심.
            # FLR-20260406-TEC-001 재발 방지 — /100 vs /1000 혼동 즉시 탐지.
            latest = parsed[0] if parsed else None
            if latest and latest.get("close") is not None:
                lo, hi = SANITY_RANGE.get(index_code, (0.0, float("inf")))
                if not (lo <= latest["close"] <= hi):
                    raise RuntimeError(
                        f"[indices {index_code}] 스케일 이상: "
                        f"close={latest['close']} (예상 {lo}~{hi}). "
                        f"SCALE_DIVISOR={SCALE_DIVISOR} 재검증 필요."
                    )

            inserted = upsert_rows(conn, index_code, parsed)
            summary["by_index"][index_code] = {
                "name": name,
                "inds_cd": inds_cd,
                "raw_count": len(raw_rows),
                "parsed_count": len(parsed),
                "inserted": inserted,
                "latest": latest,
            }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--backfill",
        type=int,
        default=30,
        help="최근 N일 백필 (기본 30일, 0=전체 ~600행)",
    )
    args = ap.parse_args()
    limit = args.backfill if args.backfill > 0 else None
    result = collect(limit_days=limit)
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
