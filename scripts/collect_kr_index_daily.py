#!/usr/bin/env python3
"""KR 지수(코스피·코스닥) 일봉 이력 수집 → 메인 repo staging (Q-20260613-161 1/2단).

대표 2026-06-13 09:39 verbatim: "코스피, 코스닥 지수를 볼 때 다른 종목 카드 처럼
모바일 버전에서 스파크라인이나 일봉캔들이 안보이고, 레인지의 최고점 최저점 날짜도
알 수 없다. 61일 레인지는 너무 적고 최소 120, 혹은 240일 정도는 되어야 의미가 있을것 같애."

61일 출처 (실측 박제): index_dailybars 61행(2026-03-16~06-12,
  collect_kiwoom_indices.py 디폴트 --backfill 30 누적 한계 — API 한계 아님)
  → collect_kr_index_intraday.py db_context() days=61
  → kr_indices.json range_240d.days=61
  → index-card.js L292 "실가용 일수 정직 표기" → 화면 "61일 레인지".

데이터 소스 — ka20006 업종일봉조회요청 (§11.15 3종 cross-check 완료, 2026-06-13):
  (a) WebSearch 2회 corroborating: openapi.kiwoom.com apiguide jobTpCode=07 +
      kiwoom-rest-api / KiwoomRestApi.Net 래퍼 + REST API 목차 PDF 일치
  (b) 공식 문서: openapi.kiwoom.com (차트 카테고리 ka20006)
  (c) repo verbatim: scripts/news_pipeline/collect_kiwoom_indices.py docstring
      (2026-04부터 production 가동 — index_dailybars 61행 실증)
  라이브 probe 실측 (2026-06-13 09:50 KST, read-only):
    * POST /api/dostk/chart, api-id=ka20006, body {"inds_cd": "001"|"101", "base_dt": YYYYMMDD}
    * 응답 키 inds_dt_pole_qry — 1페이지 600행 (newest 20260612 → oldest 20231220)
      → 241+영업일 백필 = 단일 호출 충분. ka10032형 "당일만" 함정 부재 실증.
    * 연속조회: 응답 헤더 cont-yn=Y + next-key 가용 (600행 초과 필요 시)
    * 필드: dt/cur_prc/open_pric/high_pric/low_pric/trde_qty/trde_prica
    * 스케일 = 실지수 × 100 → ÷100 + sanity range (FLR-20260406-TEC-001 /1000 오인 재발 방지)
  외부 API 사전 검증: FLR-20260408-TEC-001 정합 (인증=.env LIVE 키, rate=실행당 2호출).

산출 (메인 repo staging — 6/11 데이터 레이어 분리 지시 + DOC-20260611-DSN-002 정합.
  homepage WT 직접 쓰기 금지, 검증 후 lead promote 결정):
  1) projects/pm320/data/staging/kr_index_dailybars.json — 수집 이력 영구 보존
     (date 키 upsert, append-only 성격 — 검증·백필용. API 윈도우 이탈분도 보존)
  2) projects/pm320/data/staging/kr_indices.json — 서빙 promote 후보.
     base(현행 서빙 kr_indices.json, read-only) 필드 verbatim 보존 + 지수별 신규/확장:
       daily_expanded: [{date,o,h,l,c}] asc, 최근 240봉
         — 소비자 계약 verbatim (index-card.js L197-205: daily_expanded 우선,
           mini 20봉은 tail 20 자동 derive. us-digest §2.7.2 daily_expanded 동형)
       range_240d: {high, high_date, high_pct, low, low_date, low_pct, current, days}
         — 기존 {high,low,days} 보존 + 종목카드 shape (build_daily._calc_range_240d_at
           L467-475) + current 필수 (index-card.js L64-66: 부재 시 레인지 바 미렌더)
       range_120d: 동형 (대표 "최소 120, 혹은 240" — 양쪽 제공, 선택은 frontend/2단)
     ⚠️ lead 브리프의 `daily_240` 네이밍 대신 `daily_expanded` 채택 — 실 소비자
       (index-card.js)가 읽는 키 이름. frontend 재사용 비용 최소화 본질 정합.

promote 경로 (lead 결정 대상, 본 스크립트 비수행):
  (i) DB 백필: 기존 collect_kiwoom_indices.py --backfill 0 (cron WT에서 1회 — 600행)
  (ii) 서빙 지속 반영: collect_kr_index_intraday.py 가 daily_expanded/range 확장 필드를
       emit 하도록 후속 수정 (공통 모듈화 검토 — FLR-20260406-TEC-001 recurring 동형:
       range 계산 로직 2곳 divergence 방지)
  cron 등록 보류 — 본 1/2단은 스크립트 + 수동 1회 실행 검증까지.

frontend(js/·html·css) 무접촉. git push/DB write/homepage write 없음.

사용:
  python3 scripts/collect_kr_index_daily.py            # 수집 + staging 2종 산출
  옵션: --days 240 (서빙 윈도우), --base PATH (서빙 base JSON 경로 override)

exit: 0=PASS, 1=수집/검증 실패
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# env 로드 — 메인 .env 단일 source (collect_kr_index_intraday.py 동형)
MAIN_ENV = Path("/Users/seongjinpark/company/100m1s/.env")
if MAIN_ENV.exists():
    for line in MAIN_ENV.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.news_pipeline.kiwoom_client import get_token  # noqa: E402

KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL") or os.environ.get(
    "KIWOOM_BASE_URL", "https://api.kiwoom.com"
)
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)

STAGING_DIR = REPO_ROOT / "projects" / "pm320" / "data" / "staging"
STORE_PATH = STAGING_DIR / "kr_index_dailybars.json"
OUT_PATH = STAGING_DIR / "kr_indices.json"
# 현행 서빙 파일 (read-only base — 기존 필드 verbatim 보존용)
DEFAULT_BASE = Path(
    "/Users/seongjinpark/company/100m1s-homepage-cron/pm320/data/kr_indices.json"
)

# (index_code, 표시명, inds_cd) — collect_kiwoom_indices.py:63 동일
INDEX_TARGETS = [("KOSPI", "코스피", "001"), ("KOSDAQ", "코스닥", "101")]

# 스케일·sanity — collect_kiwoom_indices.py / collect_kr_index_intraday.py 동일
SCALE_DIVISOR = 100.0
SANITY_RANGE = {"001": (1800.0, 20000.0), "101": (400.0, 5000.0)}

KST = ZoneInfo("Asia/Seoul")


def log(msg: str) -> None:
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"[{ts}] {msg}", flush=True)


def _parse_scaled(val) -> float | None:
    """키움 지수값 파싱 — 콤마/부호 제거 후 ÷100 (peers 동형)."""
    if val is None:
        return None
    s = str(val).replace(",", "").replace("+", "").replace("-", "").strip()
    if not s:
        return None
    try:
        return round(int(s) / SCALE_DIVISOR, 2)
    except ValueError:
        return None


def _parse_int(val) -> int | None:
    if val is None:
        return None
    s = str(val).replace(",", "").replace("+", "").lstrip("-").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def fetch_index_daily(token: str, inds_cd: str, base_dt: str) -> list[dict]:
    """ka20006 1페이지 (probe 실측 600행 ≈ 2.5년 — 240일 백필 단일 호출 충분).

    수집 실패 = RuntimeError (빠른 실패 — 빈 산출물로 위장 금지, FLR-AGT-002).
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka20006",
    }
    body = {"inds_cd": inds_cd, "base_dt": base_dt}
    last_err = "unknown"
    for attempt in range(4):
        try:
            r = requests.post(
                f"{KIWOOM_BASE}/api/dostk/chart", json=body, headers=headers, timeout=20
            )
        except Exception as e:  # noqa: BLE001 - 네트워크 일시 오류 재시도
            last_err = f"exception {e}"
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code == 429:
            last_err = "http 429"
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(
                f"ka20006 {inds_cd} http {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        if data.get("return_code") == 5:  # 일시 오류 — peers 동형 재시도
            last_err = "rc=5"
            time.sleep(2 ** (attempt + 1))
            continue
        if data.get("return_code") != 0:
            raise RuntimeError(
                f"ka20006 {inds_cd} rc={data.get('return_code')} "
                f"msg={data.get('return_msg')}"
            )
        rows = data.get("inds_dt_pole_qry") or []
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"ka20006 {inds_cd} 응답 0행 — 백필 불가")
        return rows
    raise RuntimeError(f"ka20006 {inds_cd} 재시도 소진: {last_err}")


def parse_rows(raw_rows: list[dict], inds_cd: str) -> dict[str, dict]:
    """응답 → {date_iso: {o,h,l,c,v}}. close sanity 위반 시 RuntimeError."""
    lo_ok, hi_ok = SANITY_RANGE[inds_cd]
    out: dict[str, dict] = {}
    for raw in raw_rows:
        dt = str(raw.get("dt") or "").strip()
        c = _parse_scaled(raw.get("cur_prc"))
        if len(dt) != 8 or c is None:
            continue
        if not (lo_ok <= c <= hi_ok):
            raise RuntimeError(
                f"ka20006 {inds_cd} 스케일 sanity 위반: close={c} not in [{lo_ok},{hi_ok}]"
            )
        o = _parse_scaled(raw.get("open_pric"))
        h = _parse_scaled(raw.get("high_pric"))
        low = _parse_scaled(raw.get("low_pric"))
        bar = {
            "o": o if o is not None else c,
            "h": h if h is not None else c,
            "l": low if low is not None else c,
            "c": c,
        }
        v = _parse_int(raw.get("trde_qty"))
        if v is not None:
            bar["v"] = v
        out[f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"] = bar
    if not out:
        raise RuntimeError(f"ka20006 {inds_cd} 파싱 결과 0행")
    return out


def load_store() -> dict:
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 최초 실행 → 신규 store
        return {"_meta": {}}


def upsert_store(
    store: dict, index_code: str, name: str, inds_cd: str, bars: dict[str, dict]
) -> tuple[int, int]:
    """date 키 upsert (멱등). 기존 date 보존 + 신규/갱신 merge. (added, total) 반환."""
    entry = store.setdefault(index_code, {"name": name, "inds_cd": inds_cd, "bars": {}})
    entry["name"], entry["inds_cd"] = name, inds_cd
    before = set(entry["bars"].keys())
    entry["bars"].update(bars)
    return len(set(bars.keys()) - before), len(entry["bars"])


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def calc_range(
    window: list[tuple[str, dict]], current: float, trade_date: str, index_code: str
) -> dict:
    """range 객체 — build_daily._calc_range_240d_at L396-475 동형 (지수 float 변형).

    newest-first 순회 + 엄격 비교 → 동률 시 최신 날짜 우선 (종목카드 동형).
    current(라이브 값) 도 후보 포함 — 적재 타이밍 의존성 제거 (FLR-20260425 동형).
    REQ-008 Invariant 동형: high >= current >= low 위반 시 RuntimeError (라이브 차단).
    """
    high_val, high_date = -1.0, ""
    low_val, low_date = float("inf"), ""
    for d, b in reversed(window):  # newest first
        h = b.get("h") or b.get("c") or 0
        lo = b.get("l") or b.get("c") or 0
        if h > 0 and h > high_val:
            high_val, high_date = h, d
        if lo > 0 and lo < low_val:
            low_val, low_date = lo, d
    if high_val <= 0 or low_val == float("inf"):
        raise RuntimeError(f"{index_code} range 계산 불가 (유효 high/low 0건)")
    if current > high_val:
        high_val, high_date = current, trade_date
    if current < low_val:
        low_val, low_date = current, trade_date
    if high_val + 0.01 < current:
        raise RuntimeError(
            f"INVARIANT_VIOLATION range.high < current: {index_code} "
            f"high={high_val} current={current}"
        )
    if low_val - 0.01 > current:
        raise RuntimeError(
            f"INVARIANT_VIOLATION range.low > current: {index_code} "
            f"low={low_val} current={current}"
        )
    return {
        "high": round(high_val, 2),
        "high_date": high_date,
        "high_pct": round((current - high_val) / high_val * 100, 2),
        "low": round(low_val, 2),
        "low_date": low_date,
        "low_pct": round((current - low_val) / low_val * 100, 2),
        "current": round(current, 2),
        "days": len(window),
    }


def build_payload(base: dict, store: dict, days: int) -> dict:
    """base(서빙 현행) 필드 verbatim 보존 + daily_expanded/range_240d/range_120d 확장."""
    payload: dict = {}
    for index_code, _name, _inds_cd in INDEX_TARGETS:
        if index_code not in base:
            raise RuntimeError(f"base 서빙 JSON에 {index_code} 부재 — 비파괴 병합 불가")
        entry = dict(base[index_code])  # 기존 9 필드 verbatim 보존
        bars_map = store.get(index_code, {}).get("bars") or {}
        if not bars_map:
            raise RuntimeError(f"{index_code} staging store 봉 0건")
        bars_asc = sorted(bars_map.items())  # [(date, {o,h,l,c,v})] asc
        window = bars_asc[-days:]
        current = entry.get("value")
        trade_date = entry.get("trade_date") or window[-1][0]
        if not isinstance(current, (int, float)):
            raise RuntimeError(f"{index_code} base value 비수치 — current 산출 불가")
        if window[-1][0] != trade_date:
            raise RuntimeError(
                f"{index_code} 최신 일봉({window[-1][0]}) != 서빙 trade_date({trade_date})"
                " — stale 수집 의심, promote 후보 생성 차단"
            )
        entry["daily_expanded"] = [
            {"date": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
            for d, b in window
        ]
        entry["range_240d"] = calc_range(window, float(current), trade_date, index_code)
        entry["range_120d"] = calc_range(
            window[-120:], float(current), trade_date, index_code
        )
        payload[index_code] = entry
    return payload


def verify_summary(payload: dict, store: dict, days: int) -> None:
    """검증 수치 출력 — 결측 평일(휴장) 수·범위 날짜·기간 정합."""
    for index_code, entry in payload.items():
        bars = entry["daily_expanded"]
        d0 = date.fromisoformat(bars[0]["date"])
        d1 = date.fromisoformat(bars[-1]["date"])
        have = {b["date"] for b in bars}
        weekdays = 0
        cur = d0
        while cur <= d1:
            if cur.weekday() < 5:
                weekdays += 1
            cur += timedelta(days=1)
        missing = weekdays - len(bars)  # KR 휴장일 (공휴일) 기대 — 음수면 모순
        r240, r120 = entry["range_240d"], entry["range_120d"]
        total = len(store.get(index_code, {}).get("bars") or {})
        log(
            f"VERIFY {index_code}: store누적={total}봉, 윈도우={len(bars)}봉 "
            f"({bars[0]['date']}~{bars[-1]['date']}), 평일 결측(휴장)={missing}일, "
            f"중복일 0건 확인={len(have) == len(bars)}"
        )
        log(
            f"VERIFY {index_code} range_240d: high={r240['high']} ({r240['high_date']}) "
            f"/ low={r240['low']} ({r240['low_date']}) / current={r240['current']} "
            f"/ days={r240['days']}"
        )
        log(
            f"VERIFY {index_code} range_120d: high={r120['high']} ({r120['high_date']}) "
            f"/ low={r120['low']} ({r120['low_date']}) / days={r120['days']}"
        )
        if missing < 0:
            raise RuntimeError(f"{index_code} 평일 결측 음수 — 중복/비영업일 혼입 의심")
        if len(bars) < min(days, 120):
            raise RuntimeError(f"{index_code} 윈도우 {len(bars)}봉 < 요구 충족 실패")


def main() -> int:
    ap = argparse.ArgumentParser(description="KR 지수 일봉 백필 → 메인 repo staging")
    ap.add_argument(
        "--days", type=int, default=240, help="서빙 윈도우 봉 수 (기본 240)"
    )
    ap.add_argument(
        "--base",
        type=Path,
        default=DEFAULT_BASE,
        help="현행 서빙 kr_indices.json 경로 (read-only base)",
    )
    args = ap.parse_args()

    log(f"BEGIN collect_kr_index_daily days={args.days}")
    if not args.base.exists():
        log(f"FAIL: base 서빙 JSON 부재: {args.base} — 데이터 부족, 경로 확인 필요")
        return 1
    try:
        base = json.loads(args.base.read_text(encoding="utf-8"))
        token = get_token(
            KIWOOM_BASE,
            KIWOOM_APPKEY,
            KIWOOM_SECRETKEY,
            key_label="KIWOOM_LIVE_APPKEY/SECRETKEY",
        )
        store = load_store()
        base_dt = datetime.now(KST).strftime("%Y%m%d")
        for index_code, name, inds_cd in INDEX_TARGETS:
            raw = fetch_index_daily(token, inds_cd, base_dt)
            bars = parse_rows(raw, inds_cd)
            added, total = upsert_store(store, index_code, name, inds_cd, bars)
            log(
                f"{index_code}: API {len(raw)}행 → 파싱 {len(bars)}봉 "
                f"(신규 {added}, store 누적 {total})"
            )
        store["_meta"] = {
            "source": "kiwoom ka20006 (업종일봉조회요청)",
            "scale": "raw/100",
            "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
        }
        payload = build_payload(base, store, args.days)
        verify_summary(payload, store, args.days)
        _write_atomic(STORE_PATH, store)
        _write_atomic(OUT_PATH, payload)
        log(f"WRITE: {STORE_PATH}")
        log(f"WRITE: {OUT_PATH} (promote 후보 — lead 결정 대기)")
    except Exception as exc:  # noqa: BLE001 - 단일 실패 지점 보고
        log(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
