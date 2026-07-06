#!/usr/bin/env python3
"""KR 지수(코스피·코스닥) 인트라데이 수집 → kr_indices.json (조니 확정 spec ①).

목적: PM320 화면 KR 지수 스파크용 단일 JSON 산출 + homepage serving repo push.
  launchd com.100m1s.kr-index-intraday — 평일 09:05~15:35 10분 간격 + 15:40 final.

🔴 NXT 장시간(08:00~20:00) 체제에서도 본 수집기는 09:05~15:35 현행 유지 (의도적):
  ka20005 업종지수는 KRX 정규장(09:00~15:30)만 산출 — 2026-06-12 19:05 KST probe 실측
  (KOSPI·KOSDAQ 양쪽 당일 40봉, 첫 봉 0900 / 마지막 봉 1530, 15:30 이후·09:00 이전 0건).
  프리(08:00~08:50)·애프터(15:40~20:00) 확장 = 빈 수집 공회전 (FLR-AGT-002 정직성 위반).

데이터 소스 — ka20005 업종 10분봉 (spec = scripts/market_anomaly_sensor.py docstring
read-only 실측 probe 검증분 verbatim, 2026-06-12 16:07 KST):
  * POST /api/dostk/chart, api-id=ka20005, body {"inds_cd": "001"|"101", "tic_scope": "10"}
  * 응답 키 inds_min_pole_qry, 1페이지 900행(≈22거래일, 연속조회 불요)
  * 필드: cur_prc/open_pric/high_pric/low_pric, cntr_tm(14자리, 내림차순)
  * cntr_tm = 봉 시작 라벨 (0900..1520 + 1530 종가 단일 프린트 = 40봉/일)
  * 스케일 = 실지수 × 100 → ÷100 + sanity range (FLR-20260406-TEC-001 /1000 오인 재발 방지)
  외부 API 사전 검증: FLR-20260408-TEC-001 — 위 probe가 인증·endpoint·스키마 실측 완료.
  rate: 1 fire 당 2호출 (KOSPI+KOSDAQ) — 부하 무시 수준.

240d 레인지·전일종가 — 기존 index_dailybars 재사용 (read-only):
  DB = {M1S_HOMEPAGE}/data/stocks.db, 테이블 index_dailybars (PK index_code,date —
  scripts/news_pipeline/db.py:230 _ensure_index_dailybars, collect_kiwoom_indices 적재).
  현재 적재분 61거래일(2026-03-16~) — range_240d 는 가용분으로 계산, "days" 필드로
  실사용 일수 정직 표기 (FLR-AGT-002 거짓 충실성 차단). 일봉 누적은 기존 cron 이 자연 확장.

산출: {M1S_HOMEPAGE}/pm320/data/kr_indices.json — 단일 파일 원자 덮어쓰기 (tmp→rename).
  스키마: {"KOSPI": {...}, "KOSDAQ": {...}} — 지수별
  {name, value, prev_close, change_pct, candles_10m[{t,o,h,l,c}], range_240d{high,low,days},
   trade_date, asof(KST), session("intraday"|"closed")}.

git 안전 (push_pick_preview.sync_push 동형 — FLR-20260519-TEC-001 / lead-meta §11.27):
  - pre-staged change 존재 시 add/commit SKIP (타 actor staged 보호)
  - add 는 본 스크립트 산출 1파일 화이트리스트만 (무차별 glob 금지)
  - pull --rebase --autostash → push HEAD:main (plain, force 금지), race 재시도 1회
  - 휴장/주말/내용 무변경 → write·push skip (asof 만의 공회전 commit 방지)

env:
  M1S_HOMEPAGE   필수 (config.py 동형 — fallback 폐기, DB/산출 경로 divergence 봉쇄)
  KIWOOM_LIVE_APPKEY / KIWOOM_LIVE_SECRETKEY (.env — market_anomaly_sensor 동형 로드)

사용:
  M1S_HOMEPAGE=~/company/100m1s-homepage-cron python3 scripts/collect_kr_index_intraday.py
  옵션: --final (15:40 final fire 로그 라벨 — 동작 동일, session 판정은 데이터 기반)
        --no-sync (git add/commit/push skip, dry-run)

exit: 0=PASS/no-op, 1=수집·write 실패, 2=git 실패, 3=push 실패
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# env 로드 — 메인 .env 단일 source (market_anomaly_sensor.py 동형, shell export 우선)
MAIN_ENV = Path("/Users/seongjinpark/company/100m1s/.env")
if MAIN_ENV.exists():
    for line in MAIN_ENV.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# 패키지 import 경로 보정 (단독 실행 시) — kiwoom_client 는 requests 만 의존 (부작용 0)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.news_pipeline.kiwoom_client import get_token  # noqa: E402

KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL") or os.environ.get(
    "KIWOOM_BASE_URL", "https://api.kiwoom.com"
)
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)

# M1S_HOMEPAGE 필수 — config.py 동형 (audit a106c8feefbc705f4, 옛 경로 write 봉쇄)
_HOMEPAGE_ENV = os.environ.get("M1S_HOMEPAGE")
if not _HOMEPAGE_ENV:
    raise RuntimeError(
        "M1S_HOMEPAGE 환경변수 필수 — cron: ~/company/100m1s-homepage-cron, "
        "ad-hoc: 대상 worktree 명시 (config.py 동형 fallback 폐기)"
    )
HOMEPAGE_DIR = Path(_HOMEPAGE_ENV)
STOCKS_DB = HOMEPAGE_DIR / "data" / "stocks.db"
OUT_PATH = HOMEPAGE_DIR / "pm320" / "data" / "kr_indices.json"

# (index_code, 표시명, inds_cd) — collect_kiwoom_indices.py:63 + ka20005 spec 동일
INDEX_TARGETS = [("KOSPI", "코스피", "001"), ("KOSDAQ", "코스닥", "101")]

# 스케일·sanity — market_anomaly_sensor.py 실측 검증값 동일 (FLR-20260406-TEC-001)
SCALE_DIVISOR = 100.0
SANITY_RANGE = {"001": (1800.0, 20000.0), "101": (400.0, 5000.0)}

RANGE_DAYS = 240  # range_240d LIMIT — 적재 부족 시 가용분 (days 필드로 표기)
CLOSE_LABEL = "1530"  # 종가 단일 프린트 봉 라벨 (실측 40봉/일)

KST = ZoneInfo("Asia/Seoul")


def log(msg: str) -> None:
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"[{ts}] {msg}", flush=True)


def _parse_scaled(val) -> float | None:
    """키움 가격 파싱 — 부호/콤마 제거 후 /100 (market_anomaly_sensor 동형)."""
    if val is None:
        return None
    s = str(val).replace(",", "").replace("+", "").replace("-", "").strip()
    if not s:
        return None
    try:
        return int(s) / SCALE_DIVISOR
    except ValueError:
        return None


def fetch_index_minutes(token: str, inds_cd: str) -> list[dict]:
    """ka20005 업종 10분봉 1페이지 — [{t14, o, h, l, c}] 오름차순.

    sanity 위반 시 RuntimeError (스케일 오인 데이터 serving 차단).
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka20005",
    }
    body = {"inds_cd": inds_cd, "tic_scope": "10"}
    last_err = "unknown"
    for attempt in range(3):
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
                f"ka20005 {inds_cd} http {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        if data.get("return_code") != 0:
            raise RuntimeError(f"ka20005 {inds_cd} rc={data.get('return_code')}")
        rows = data.get("inds_min_pole_qry") or []
        out: list[dict] = []
        lo, hi = SANITY_RANGE[inds_cd]
        for row in rows:
            dt14 = str(row.get("cntr_tm", "")).strip()
            close = _parse_scaled(row.get("cur_prc"))
            if len(dt14) != 14 or close is None:
                continue
            if not (lo <= close <= hi):
                raise RuntimeError(
                    f"ka20005 {inds_cd} scale sanity 위반: {close} not in [{lo},{hi}]"
                )
            o = _parse_scaled(row.get("open_pric"))
            h = _parse_scaled(row.get("high_pric"))
            low = _parse_scaled(row.get("low_pric"))
            out.append(
                {
                    "t14": dt14,
                    "o": o if o is not None else close,
                    "h": h if h is not None else close,
                    "l": low if low is not None else close,
                    "c": close,
                }
            )
        out.sort(key=lambda x: x["t14"])
        return out
    raise RuntimeError(f"ka20005 {inds_cd} 재시도 소진: {last_err}")


def db_context(index_code: str, trade_date_iso: str) -> tuple[float | None, dict]:
    """index_dailybars (read-only) — (prev_close, range_240d{high,low,days}).

    prev_close = trade_date 직전 거래일 close. range = 직근 RANGE_DAYS 행
    (high/low NULL 행은 close 로 보정 — 스키마상 close 만 NOT NULL).
    """
    uri = f"file:{STOCKS_DB}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        row = conn.execute(
            "SELECT close FROM index_dailybars"
            " WHERE index_code=? AND date<? ORDER BY date DESC LIMIT 1",
            (index_code, trade_date_iso),
        ).fetchone()
        prev_close = float(row[0]) if row and row[0] is not None else None
        rng = conn.execute(
            "SELECT MAX(COALESCE(high, close)), MIN(COALESCE(low, close)), COUNT(*)"
            " FROM (SELECT high, low, close FROM index_dailybars"
            "       WHERE index_code=? ORDER BY date DESC LIMIT ?)",
            (index_code, RANGE_DAYS),
        ).fetchone()
    high = round(float(rng[0]), 2) if rng and rng[0] is not None else None
    low = round(float(rng[1]), 2) if rng and rng[1] is not None else None
    days = int(rng[2]) if rng else 0
    return prev_close, {"high": high, "low": low, "days": days}


def db_close_on(index_code: str, date_iso: str) -> float | None:
    """index_dailybars 특정일 close (교차검증용, read-only)."""
    uri = f"file:{STOCKS_DB}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        row = conn.execute(
            "SELECT close FROM index_dailybars WHERE index_code=? AND date=?",
            (index_code, date_iso),
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def enrich_with_daily(entry: dict, index_code: str) -> None:
    """daily_expanded/range_240d/range_120d 확장 — 일봉 store(240봉)에서 산출 (in-place).

    Q-20260613-161 2/2단. 화면 '61일 레인지'(index_dailybars 적재 부족) → 240/120봉
    레인지·미니캔들·고저날짜 노출. range 계산은 collect_kr_index_daily.calc_range 단일
    source 재사용 (db_context 의 DB range 와 산식 divergence 방지 — DSN-pipeline §2.8 (ii)
    + FLR-20260406-TEC-001 recurring 동형). 라이브 value(entry['value']) = current 후보로
    포함 → 적재 타이밍 의존성 제거.

    graceful (FLR-AGT-002 거짓 충실성 차단): store 부재/봉 부족/계산 불가 시 기존
    db_context range_240d 보존 + daily_expanded 미부착 (mock·폴백 0). lazy import 로
    모듈 로드 부작용·미사용 import 0.
    """
    try:
        from scripts.collect_kr_index_daily import calc_range, load_store
    except Exception as exc:  # noqa: BLE001 - import 실패 시 확장 생략(기본 동작 보존)
        log(f"ENRICH skip {index_code}: import 실패 {type(exc).__name__}: {exc}")
        return
    current = entry.get("value")
    trade_date = entry.get("trade_date")
    if not isinstance(current, (int, float)) or not trade_date:
        return
    try:
        store = load_store()
        bars_map = store.get(index_code, {}).get("bars") or {}
        if len(bars_map) < 2:  # 최소 2봉 (range·캔들 의미)
            log(
                f"ENRICH skip {index_code}: store 봉 {len(bars_map)}건 (<2) — DB range 유지"
            )
            return
        bars_asc = sorted(bars_map.items())  # [(date, {o,h,l,c,v})] asc
        window240 = bars_asc[-RANGE_DAYS:]
        window120 = bars_asc[-120:]
        # range — current 포함 expand 는 calc_range 내부 처리(신고/신저 자동). store newest
        #   < 라이브 trade_date(새 거래일 첫 수집) 여도 current 가 갱신하므로 정합 (raise 없음).
        entry["range_240d"] = calc_range(
            window240, float(current), trade_date, index_code
        )
        entry["range_120d"] = calc_range(
            window120, float(current), trade_date, index_code
        )
        entry["daily_expanded"] = [
            {"date": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
            for d, b in window240
        ]
    except Exception as exc:  # noqa: BLE001 - 계산 실패 시 기존 range 유지(거짓 데이터 0)
        log(
            f"ENRICH fallback {index_code}: {type(exc).__name__}: {exc} — DB range 유지"
        )


def build_index_entry(name: str, bars: list[dict], index_code: str) -> dict:
    """단일 지수 payload. bars = ka20005 전체 (22거래일) — 최종 거래일분만 사용."""
    if not bars:
        raise RuntimeError(f"{index_code} ka20005 봉 0건 — payload 생성 불가")
    trade_date8 = bars[-1]["t14"][:8]
    trade_date_iso = f"{trade_date8[:4]}-{trade_date8[4:6]}-{trade_date8[6:8]}"
    day_bars = [b for b in bars if b["t14"][:8] == trade_date8]
    candles = [
        {
            "t": b["t14"][8:12],
            "o": round(b["o"], 2),
            "h": round(b["h"], 2),
            "l": round(b["l"], 2),
            "c": round(b["c"], 2),
        }
        for b in day_bars
    ]
    value = candles[-1]["c"]
    today_iso = datetime.now(KST).strftime("%Y-%m-%d")
    if trade_date_iso < today_iso:
        session = "closed"  # 휴장/개장 전 — 직전 거래일 데이터 유지
    else:
        session = (
            "closed" if any(c["t"] == CLOSE_LABEL for c in candles) else "intraday"
        )
    prev_close, range_240d = db_context(index_code, trade_date_iso)
    change_pct = round((value / prev_close - 1.0) * 100.0, 2) if prev_close else None
    entry = {
        "name": name,
        "value": value,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "candles_10m": candles,
        "range_240d": range_240d,
        "trade_date": trade_date_iso,
        "asof": datetime.now(KST).isoformat(timespec="seconds"),
        "session": session,
    }
    # Q-20260613-161 2/2단 — 일봉 store(240봉)에서 range_240d/range_120d/daily_expanded 확장
    #   (in-place 덮어쓰기). 실패 시 위 db_context range_240d 보존 (graceful, FLR-AGT-002).
    enrich_with_daily(entry, index_code)
    return entry


def payload_unchanged(new: dict) -> bool:
    """기존 파일 대비 asof 외 동일 여부 — 휴장 공회전 commit 방지."""
    try:
        old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 최초 실행/손상 → 변경으로 간주
        return False

    def strip_asof(d: dict) -> dict:
        # 지수 entry(dict)만 비교 대상 + 각 entry 의 asof 제외. top-level "as_of"
        #   (문자열, 본 함수가 추가한 파일 단위 집계 시각)는 isinstance dict 필터로
        #   자동 제외 → 휴장 공회전 시 as_of 만 갱신돼도 unchanged 판정 유지.
        return {
            code: {k: v for k, v in entry.items() if k != "asof"}
            for code, entry in d.items()
            if isinstance(entry, dict)
        }

    return strip_asof(old) == strip_asof(new)


def write_atomic(payload: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    tmp.replace(OUT_PATH)


def _git(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    # 고정 인자 리스트만 전달 (untrusted input 0) + PATH 는 launchd plist 가 통제
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=HOMEPAGE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def sync_push(date_iso: str) -> int:
    """산출 1파일 화이트리스트 add → commit → rebase+push (push_pick_preview 동형)."""
    rel = str(OUT_PATH.relative_to(HOMEPAGE_DIR))
    if not (HOMEPAGE_DIR / ".git").exists():
        log(f"SYNC FAIL: not a git repo: {HOMEPAGE_DIR}")
        return 2

    # 타 actor staged 보호 (lead-meta §11.27) — pre-staged 존재 시 본 fire SKIP
    pre = _git(["diff", "--cached", "--name-only"], timeout=30)
    if pre.returncode != 0:
        log(f"SYNC FAIL (git diff): rc={pre.returncode}")
        return 2
    if pre.stdout.strip():
        log(
            f"SYNC SKIP: pre-existing staged changes "
            f"({len(pre.stdout.splitlines())} files) — 다음 fire 재시도"
        )
        return 0

    status = _git(["status", "--porcelain", "--", rel], timeout=30)
    if status.returncode != 0:
        log(f"SYNC FAIL (git status): rc={status.returncode}")
        return 2
    if not status.stdout.strip():
        log("SYNC SKIP: no change (산출 동일)")
        return 0

    if _git(["add", rel], timeout=30).returncode != 0:
        log("SYNC FAIL (git add)")
        return 2
    msg = f"data(kr-indices,{date_iso}): KR 지수 인트라데이 (collect_kr_index_intraday.py)"
    if _git(["commit", "-m", msg], timeout=60).returncode != 0:
        log("SYNC FAIL (git commit)")
        return 2
    log(f"SYNC: commit done ({rel})")

    # rebase + plain push, race 시 재시도 1회 (force 금지)
    # fetch + rebase 2단계 분리: cron WT upstream(cron-isolation)과 origin/main 모호성 제거
    # (pull --rebase origin main 는 upstream 추적 브랜치가 별도 설정된 경우
    #  "Cannot rebase onto multiple branches" fatal 발생 — 2026-06-29 근본 수정)
    for attempt in (1, 2):
        fetch = _git(["fetch", "origin", "main"], timeout=30)
        if fetch.returncode != 0:
            log(f"SYNC FAIL (fetch, attempt {attempt}): {fetch.stderr.strip()[:200]}")
            return 3
        pull = _git(["rebase", "--autostash", "origin/main"])
        if pull.returncode != 0:
            log(f"SYNC FAIL (rebase, attempt {attempt}): {pull.stderr.strip()[:200]}")
            return 3
        push = _git(["push", "origin", "HEAD:main"])
        if push.returncode == 0:
            log(
                f"SYNC: push done → origin main (kr_indices {date_iso}, attempt {attempt})"
            )
            return 0
        log(
            f"SYNC WARN (push rejected, attempt {attempt}): {push.stderr.strip()[:200]}"
        )
    return 3


def main() -> int:
    ap = argparse.ArgumentParser(
        description="KR 지수 인트라데이 수집 → kr_indices.json"
    )
    ap.add_argument(
        "--final", action="store_true", help="15:40 final fire 라벨 (동작 동일)"
    )
    ap.add_argument("--no-sync", action="store_true", help="git add/commit/push skip")
    args = ap.parse_args()

    now = datetime.now(KST)
    if now.weekday() >= 5:  # launchd 매일 fire — 주말 가드 (휴장일은 데이터 기반 no-op)
        log("SKIP: weekend")
        return 0
    mode = "final" if args.final else "intraday"
    log(f"BEGIN collect_kr_index_intraday mode={mode}")

    try:
        token = get_token(
            KIWOOM_BASE,
            KIWOOM_APPKEY,
            KIWOOM_SECRETKEY,
            key_label="KIWOOM_LIVE_APPKEY/SECRETKEY",
        )
        payload: dict = {}
        for index_code, name, inds_cd in INDEX_TARGETS:
            bars = fetch_index_minutes(token, inds_cd)
            payload[index_code] = build_index_entry(name, bars, index_code)
            e = payload[index_code]
            log(
                f"{index_code}: {e['trade_date']} {e['session']} value={e['value']} "
                f"prev={e['prev_close']} chg={e['change_pct']}% "
                f"candles={len(e['candles_10m'])} range_days={e['range_240d']['days']}"
            )
    except Exception as exc:  # noqa: BLE001 - 수집 실패 = 기존 serving json 보존 종료
        log(f"FAIL collect: {type(exc).__name__}: {exc}")
        return 1

    # top-level as_of — 파일 단위 수집(집계) 시각. macro_indicators.json 대칭
    #   (RND-PM320-063 P1② 신선도 추적 비대칭 해소). 지수별 entry.asof 중 최신값
    #   = 본 fire 의 가장 늦은 수집 시각. 지수 0건 fallback = 현재 KST.
    #   payload_unchanged 의 strip_asof 가 top-level as_of 도 제외하므로 휴장 공회전 무영향.
    _entry_asofs = [
        e["asof"]
        for e in payload.values()
        if isinstance(e, dict) and isinstance(e.get("asof"), str)
    ]
    payload["as_of"] = (
        max(_entry_asofs)
        if _entry_asofs
        else datetime.now(KST).isoformat(timespec="seconds")
    )

    if payload_unchanged(payload):
        log("SKIP: payload unchanged (휴장/개장 전) — write·push 생략")
        return 0
    try:
        write_atomic(payload)
        log(f"WRITE: {OUT_PATH}")
    except Exception as exc:  # noqa: BLE001
        log(f"FAIL write: {type(exc).__name__}: {exc}")
        return 1

    if args.no_sync or os.environ.get("M1S_KR_INDEX_NO_SYNC") == "1":
        log("SYNC SKIP: no-sync 지정")
        return 0
    return sync_push(payload["KOSPI"]["trade_date"])


if __name__ == "__main__":
    sys.exit(main())
