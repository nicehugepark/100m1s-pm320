#!/usr/bin/env python3
"""
PM320 카드 universe pm320_history 본문 wrapper.

flow:
  1. 본일 picks JSON read (projects/pm320/data/daily/picks/{date}.json) → picked.code = PICK 종목
  2. 본일 카드 universe read (~/company/100m1s-homepage-cron/data/interpreted/stock-{date}.json)
     → stocks[].code list (모든 카드 종목, 옵션 C 대표 결정 정합)
  3. 각 카드 종목별 dailybars D 종가 → P0
  4. send_kakao_message.py verbatim 패턴 복제 (계산식 자의적 변경 0건):
     - watering_target_price = round(P0 * 0.936)
     - take_profit_target_price = round(P0 * 1.032)
     - avg_after_watering = (P0 + 1배 * P0 * 0.936) / 2  (물타기 1배, 총 2유닛 — 대표 2026-06-09)
     - take_profit_after_watering_price = round(avg_after_watering * 1.032)
     - expiry_date = 물타기 거래 D+6 / 물타기 안 한 거래 D+3 (holidays.json SoT, 대표 2026-06-09)
  5. 시점 분기 (DSN-001 §1 4종 영어 enum verbatim, lead 옵션 B early-exit):
     - today <= pick_date → "running"; today > pick_date → 결과 판정 시도(미확정 시 running)
     - 윈도우 내 익절/물타기/만기청산 확정 → "taken_profit" / "expired_gain" / "expired_loss"
     - 백테스트 enum 6종 → DSN 3종 매핑 (compound_account_bt.py:72-201 simulate_minute_unit verbatim 패턴):
       * phase1_profit + phase2_profit → "taken_profit" (장중 익절 도달)
       * phase2_close_profit + hold_close_profit → "expired_gain" (만기 평단 상회)
       * phase2_close_loss + hold_close_loss → "expired_loss" (만기 평단 하회)
  6. DSN-001 §1 schema verbatim:
     pm320_history = {
       "date": "YYYY-MM-DD",
       "generated_at": "ISO8601",
       "picked_code": "108490" | null,
       "stocks": [
         { "code": "...", "name": "...",
           "pm320_pick": {
             "is_pick": bool, "pick_date": str, "entry_price": int,
             "watering_target_price": int, "take_profit_target_price": int,
             "watering_weight": "첫 매수와 동일 수량(1배)",
             "take_profit_after_watering_price": int,
             "expiry_date": str,
             "current_state": "running" | "taken_profit" | "expired_loss" | "expired_gain",
             "current_pnl_pct": float, "d_offset": int,
             "result": null | { "final_price": int, "final_pnl_pct": float,
                                 "watered": bool, "result_date": "YYYY-MM-DD" }
           }
         }, ...
       ]
     }
  7. 메인 worktree write: projects/pm320/data/history/{date}.json
     (cron pipeline 영역 외 = race condition 0건, §11.32 정합)
     기존 cron worktree 산출물 (~/100m1s-homepage-cron/data/pm320_history/{date}.json) = 보존,
     본 wrapper는 메인 worktree path만 신축 (sub-agent §16 옵션 γ 영향 0건)

rules:
- send_kakao_message.py 계산식 verbatim 복제, 자의적 변경 0건
- compound_account_bt.py simulate_minute_unit verbatim 양터치 패턴 복제
- picks 미존재 / cards 미존재 / dailybars 미존재 시 graceful exit
- DSN-001 §1 schema 정합 (4종 영어 enum verbatim, "expired" 단일 사용 금지)
- 비-PICK 종목 가상 시뮬레이션 = is_pick:false, schema 동일 (frontend §3.3 muted 차등 처리)
- minute touch 분봉 미커버 시 → result null + current_state running 유지 (graceful)
- 메인 worktree write only, cron pipeline 영역 0건 touch

usage:
  python3 scripts/pm320/build_card_history.py [--date YYYY-MM-DD] [--dry-run]
  --dry-run: 파일 write skip, stdout으로 JSON preview

doc_id: feat(pm320,P0,card-recommendation,minute-touch,historical,DSN-001)
generated: 2026-06-03 (대표 A+A ack — path 메인 worktree + DSN §1 정정 + minute touch + Phase 3 reconstruction wrapper)
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- paths (env 우선 패턴, DSN-arch-pm320 §7 정합) ---
REPO_ROOT = Path(__file__).resolve().parents[2]
# S5 자립화 (DOC-20260707-REQ-001): pm320 레포에는 projects/ 부재 → 레포 로컬 data/daily/picks.
PICKS_DIR = REPO_ROOT / "data" / "daily" / "picks"

# S5 자립화 (DOC-20260707-REQ-001): pm320 레포에는 projects/ 부재 → 레포 로컬 data/ 하위.
HISTORY_OUT_DIR = REPO_ROOT / "data" / "history"
PUBLIC_HISTORY_OUT_DIR = REPO_ROOT / "data" / "deploy_history"

# 데이터 홈 — M1S_HOMEPAGE env 우선, 미설정 시 pm320 레포 루트로 자립 fallback.
HOMEPAGE_DIR = Path(os.environ.get("M1S_HOMEPAGE", str(REPO_ROOT)))
# dailybars / interpreted 개별 override (재백필 시 다중 worktree 데이터 분산 대응).
# 기본은 HOMEPAGE_DIR 하위. 단, 재백필은 fresh dailybars(백테스트 SoT DB)와 완비된
# interpreted 가 서로 다른 worktree 에 있을 수 있어 개별 지정 가능 (false-fidelity 차단:
# stale dailybars 로 잘못된 결과 산출 금지, 백테스트 +11.80% 와 동일 데이터 사용 의무).
STOCKS_DB = Path(
    os.environ.get("M1S_PM320_DAILYBARS_DB", str(HOMEPAGE_DIR / "data" / "stocks.db"))
)
INTERPRETED_DIR = Path(
    os.environ.get(
        "M1S_PM320_INTERPRETED_DIR", str(HOMEPAGE_DIR / "data" / "interpreted")
    )
)
HOLIDAYS_JSON = Path(
    os.environ.get("M1S_PM320_HOLIDAYS", str(HOMEPAGE_DIR / "data" / "holidays.json"))
)

# 판정용 1분봉 SoT = processed snapshot.
# raw 수집 DB(minutes.db / minutes_nxt.db)를 직접 판정에 쓰지 않는다. 정규장·장외 raw를
# scripts/pm320/build_judgement_minutes.py 가 병합·정규화한 단일 DB만 읽어 카드/백테스트
# 데이터 소스 이원화를 차단한다.
JUDGE_MINUTES_DB = Path(
    os.environ.get(
        "M1S_PM320_JUDGE_MINUTES_DB",
        # S5 자립화 (DOC-20260707-REQ-001): pm320 레포에는 projects/ 부재 →
        # 레포 로컬 data/processed (PICKS_DIR·HISTORY_OUT_DIR 자립화와 동일 패턴).
        str(REPO_ROOT / "data" / "processed" / "pm320_judge_minutes.db"),
    )
)
JUDGE_MINUTES_MAX_AGE_MINUTES = int(
    os.environ.get("M1S_PM320_JUDGE_MINUTES_MAX_AGE_MINUTES", "20")
)
# 판정 시간대 윈도우 (HH:MM, dt='YYYY-MM-DD HH:MM').
JUDGE_WINDOWS = (("08:00", "08:50"), ("09:00", "15:30"), ("15:40", "20:00"))
ENTRY_DAY_AFTERHOURS_START = "15:20"
ENTRY_DAY_AFTERHOURS_END = "20:00"


def _in_judge_window(hhmm: str) -> bool:
    """장전 08:00~08:50 + 정규장 09:00~15:30 + 장후 15:40~20:00 1분봉만 판정 대상."""
    return any(lo <= hhmm <= hi for lo, hi in JUDGE_WINDOWS)


def _minute_dt_bounds(date_str: str) -> tuple[str, str]:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return f"{date_str} 00:00", f"{(d + timedelta(days=1)).strftime('%Y-%m-%d')} 00:00"


KST = timezone(timedelta(hours=9))

_CONN_CACHE: dict[str, sqlite3.Connection] = {}


def _connect_cached(path: Path) -> sqlite3.Connection:
    key = str(path)
    conn = _CONN_CACHE.get(key)
    if conn is None:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        _CONN_CACHE[key] = conn
    return conn


def _close_cached_connections() -> None:
    for conn in list(_CONN_CACHE.values()):
        try:
            conn.close()
        except Exception:
            pass
    _CONN_CACHE.clear()


atexit.register(_close_cached_connections)

# --- 계산식 상수 ---
# 비가변 공통 (모든 프로파일 동일): 익절 +3.2% / 물타기 -6.4%.
WATERING_RATIO = 0.936  # P0 × 0.936 = -6.4%
TAKE_PROFIT_RATIO = 1.032  # P0 × 1.032 (또는 평단 × 1.032)


def _load_strategy() -> Any:
    """전략 프로파일 active + 만기 핸들러 레지스트리 로드 (profiles.json SSOT).

    함수-로컬 import 로 strategy_profiles 를 가져온다 — autoflake 모듈-레벨 import 오제거 회피.
    return: (active_profile, EXPIRY_MODES_registry).
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from strategy_profiles import load_active_profile
    from strategy_profiles.registry import EXPIRY_MODES

    return load_active_profile(), EXPIRY_MODES


# ── 가변 4축 ③④ (전략 프로파일 SSOT, profiles.json active) ──────────────────
# 직전 하드코딩(WATERING_WEIGHT=1.0 / FORWARD_D3=3 / FORWARD_D6=6 / expiry 분기)을
# 프로파일 참조로 교체. active 미변경 시 동일 값 산출 (회귀 byte-identical 의무).
_PROFILE, EXPIRY_MODES = _load_strategy()
# 축③ 물타기 추가매수 비중 (1.0=1배 → 총 2유닛 / 2.0=2배 → 총 3유닛).
#   평단 = TOTAL_UNITS / (1/P0 + WATERING_WEIGHT/P_water).
WATERING_WEIGHT = float(_PROFILE["watering_weight"])
WATERING_WEIGHT_LABEL = _PROFILE.get("watering_weight_label") or (
    f"첫 매수의 {WATERING_WEIGHT:g}배"
)
# 축④ 만기 enum: "all_d3"(전종목 D+base) | "water_d6"(물타기만 D+water, 그외 D+base) |
#   "all_d6"(전종목 D+water). forward 일수는 forward_d_base / forward_d_water.
EXPIRY_MODE = _PROFILE["expiry_mode"]
FORWARD_D3 = int(_PROFILE["forward_d_base"])  # 기본(물타기 안 한 거래) 만기 윈도우
FORWARD_D6 = int(_PROFILE["forward_d_water"])  # 연장(물타기 거래) 만기 윈도우
# add 후 총 유닛 = 1 + WATERING_WEIGHT. 평단 = total_units/(1/P0 + WATERING_WEIGHT/P_water).
TOTAL_UNITS_AFTER_WATER = 1.0 + WATERING_WEIGHT

# 백테스트 enum 6종 → DSN-001 §1 3종 영어 enum 매핑 (sub-agent §16 catch verbatim)
BUCKET_TO_STATE: dict[str, str] = {
    "phase1_profit": "taken_profit",  # 장중 1차 익절
    "phase2_profit": "taken_profit",  # 물타기 후 평단 익절
    "phase2_close_profit": "expired_gain",  # D+3 만기 + 물타기 후 평단 상회
    "hold_close_profit": "expired_gain",  # D+3 만기 + 물타기 없이 평단 상회
    "phase2_close_loss": "expired_loss",  # D+3 만기 + 물타기 후 평단 하회
    "hold_close_loss": "expired_loss",  # D+3 만기 + 물타기 없이 평단 하회
}


def log(msg: str) -> None:
    """stderr 로그."""
    print(f"[build_card_history] {msg}", file=sys.stderr, flush=True)


def load_market_closed_set() -> set[str]:
    """한국 거래소 휴장일 set (holidays.json SoT, send_kakao_message.py L207~L228 verbatim 복제).

    schema:
      { "year": 2026, "market_closed": { "YYYY-MM-DD": "사유", ... } }
    """
    if not HOLIDAYS_JSON.exists():
        log(f"WARN: holidays.json not found: {HOLIDAYS_JSON} (weekday-only fallback)")
        return set()
    try:
        d = json.loads(HOLIDAYS_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"FAIL: holidays.json parse: {type(exc).__name__}")
        return set()
    mc = d.get("market_closed")
    if isinstance(mc, dict):
        return set(mc.keys())
    if isinstance(mc, list):
        return {str(x) for x in mc}
    return set()


def add_trading_days(start_date: str, n: int) -> str | None:
    """start_date에서 n번째 다음 거래일 (휴장일/주말 skip).

    send_kakao_message.py L231~L262 verbatim 복제 (자의적 변경 0건).
    """
    closed = load_market_closed_set()
    try:
        dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        log(f"FAIL: start_date parse: {start_date}")
        return None
    added = 0
    cur = dt
    # 최대 30일 탐색 (연속 휴장 cascade 봉쇄)
    for _ in range(30):
        cur = cur + timedelta(days=1)
        iso = cur.strftime("%Y-%m-%d")
        if cur.weekday() >= 5:
            continue
        if iso in closed:
            continue
        added += 1
        if added == n:
            return iso
    log(f"WARN: add_trading_days 30-day cap exceeded (start={start_date} n={n})")
    return None


def prev_trading_day(ref_date: str) -> str | None:
    """ref_date 직전 거래일 (휴장일/주말 skip). add_trading_days 역방향.

    회귀 게이트용 — 정산 summary 의 last_settled_date 가 이 값보다 오래되면 stale.
    """
    closed = load_market_closed_set()
    try:
        dt = datetime.strptime(ref_date, "%Y-%m-%d").date()
    except ValueError:
        log(f"FAIL: prev_trading_day parse: {ref_date}")
        return None
    cur = dt
    for _ in range(30):
        cur = cur - timedelta(days=1)
        iso = cur.strftime("%Y-%m-%d")
        if cur.weekday() >= 5 or iso in closed:
            continue
        return iso
    return None


def count_trading_days_between(start_date: str, end_date: str) -> int:
    """start_date (exclusive) ~ end_date (inclusive) 사이 거래일 수.

    d_offset 계산용 (오늘이 D 기준 몇 번째 거래일인지).
    end_date <= start_date 시 0 반환.
    """
    closed = load_market_closed_set()
    try:
        s = datetime.strptime(start_date, "%Y-%m-%d").date()
        e = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return 0
    if e <= s:
        return 0
    n = 0
    cur = s
    for _ in range(60):
        cur = cur + timedelta(days=1)
        if cur > e:
            break
        iso = cur.strftime("%Y-%m-%d")
        if cur.weekday() >= 5:
            continue
        if iso in closed:
            continue
        n += 1
    return n


def load_picks(date_str: str) -> dict[str, Any] | None:
    """본일 picks JSON read (send_kakao_message.py L168~L178 verbatim 복제 패턴)."""
    fp = PICKS_DIR / f"{date_str}.json"
    if not fp.exists():
        log(f"INFO: picks not found: {fp.name}")
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"FAIL: picks parse: {type(exc).__name__}")
        return None


def require_fresh_judge_minutes(date_str: str) -> bool:
    """라이브 판정용 processed minute DB freshness gate.

    조건별 raw fallback 은 금지한다. 오늘 또는 과거 일자를 build 할 때도 processed DB가
    해당 일자를 포함하고, generated_at 이 허용 시간 안쪽이어야 한다. 실패하면 카드 생성 자체를
    중단해 stale/partial 데이터가 라이브로 배포되지 않게 한다.
    """
    if not JUDGE_MINUTES_DB.exists() or JUDGE_MINUTES_DB.stat().st_size == 0:
        log(f"FAIL: judge minutes DB missing: {JUDGE_MINUTES_DB}")
        return False
    try:
        conn = _connect_cached(JUDGE_MINUTES_DB)
        meta = dict(conn.execute("SELECT key, value FROM build_meta").fetchall())
        lo, hi = _minute_dt_bounds(date_str)
        row_count = conn.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE dt>=? AND dt<?",
            (lo, hi),
        ).fetchone()[0]
    except Exception as exc:
        log(f"FAIL: judge minutes freshness query: {type(exc).__name__}")
        return False
    if not row_count:
        log(f"FAIL: judge minutes has no rows for {date_str}")
        return False
    processed_at = meta.get("processed_at")
    if not isinstance(processed_at, str):
        log("FAIL: judge minutes processed_at missing")
        return False
    try:
        dt = datetime.fromisoformat(processed_at)
    except ValueError:
        log(f"FAIL: judge minutes processed_at invalid: {processed_at}")
        return False
    age_sec = (datetime.now(KST) - dt.astimezone(KST)).total_seconds()
    max_age_sec = JUDGE_MINUTES_MAX_AGE_MINUTES * 60
    if age_sec < -60 or age_sec > max_age_sec:
        log(
            "FAIL: judge minutes stale "
            f"processed_at={processed_at} age_sec={age_sec:.0f} max_sec={max_age_sec}"
        )
        return False
    last_range = meta.get("last_range", "")
    if "~" in last_range:
        start, end = last_range.split("~", 1)
        if not (start <= date_str <= end):
            log(f"FAIL: judge minutes range {last_range} does not include {date_str}")
            return False
    return True


def load_card_universe(date_str: str) -> list[dict[str, Any]] | None:
    """본일 interpreted/stock-{date}.json read → stocks[] list."""
    fp = INTERPRETED_DIR / f"stock-{date_str}.json"
    if not fp.exists():
        log(f"INFO: card universe not found: {fp.name}")
        return None
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        stocks = d.get("stocks", [])
        if not isinstance(stocks, list):
            log("FAIL: stocks key not list")
            return None
        return stocks
    except Exception as exc:
        log(f"FAIL: card universe parse: {type(exc).__name__}")
        return None


def load_close_price(code: str, date_str: str) -> int | None:
    """dailybars D 종가 = entry_price(P0) SSOT (send_kakao_message.py L181~L204 verbatim 복제, read-only).

    Q-20260606-111 (대표 2026-06-06 02:11/02:15) — entry_price = **마감 후 dailybars close** 가 정본.
      15:20 pick 시점에 build 가 돌면 dailybars D close 가 아직 잠정(장중 last)일 수 있어 official close 와
      괴리(240810: 잠정 131400 vs 마감 132700). 본 build 는 dailybars close 가 finalize 된 뒤(장 마감 backfill
      후) 재실행되어야 entry_price·물타기·익절이 정본으로 갱신된다(재실행 시 전 필드 재계산, 보존 0 = idempotent
      overwrite). 카톡 진입가 제거(02fe565)와 동일 본질 — pick 시점 잠정가의 구조적 괴리 해소.
    """
    if not STOCKS_DB.exists():
        log(f"WARN: stocks.db not found: {STOCKS_DB}")
        return None
    try:
        conn = _connect_cached(STOCKS_DB)
        row = conn.execute(
            "SELECT close FROM dailybars WHERE code=? AND date=?",
            (code, date_str),
        ).fetchone()
    except Exception as exc:
        log(f"FAIL: stocks.db query: {type(exc).__name__}")
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def load_forward_dailybars(
    code: str, pick_date: str, n: int = FORWARD_D6
) -> list[tuple[str, int, int, int, int]] | None:
    """code의 pick_date 다음 최대 n 거래일 일봉 (있는 만큼).

    return: [(date, open, high, low, close)] (1~n개) or None (0개 = forward 일봉 전무).
    martingale_bt.py:180-186 forward_window 패턴 정합. 단 윈도우 미완성(len < n)도
    반환 — 윈도우 내 확정된 결과(익절 등)는 forward 일봉 미완비여도 표시 (lead 옵션 B,
    7ef5c39 재포함). 완성 여부(len == 만기일수)는 호출부(compute_result)가 만기청산 확정
    vs 미확정 분기에 사용.
    """
    if not STOCKS_DB.exists():
        return None
    try:
        conn = _connect_cached(STOCKS_DB)
        rows = conn.execute(
            "SELECT date, open, high, low, close FROM dailybars "
            "WHERE code=? AND date>? ORDER BY date ASC LIMIT ?",
            (code, pick_date, n),
        ).fetchall()
    except Exception as exc:
        log(f"FAIL: forward dailybars query: {type(exc).__name__}")
        return None
    # 윈도우 미완성(len < n) 이어도 있는 만큼 반환 (early-exit 인정, lead 옵션 B).
    # rows 0건(forward 일봉 전무 = 익절 판정 불가)일 때만 None.
    if not rows:
        return None
    return [(r[0], int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows]


def load_forward_minutes(
    code: str, fwd_dates: list[str]
) -> list[tuple[str, float, float, float]] | None:
    """code의 fwd_dates 분봉 시계열 평탄화.

    return: [(dt, high, low, close)] 시각순 or None (한 거래일이라도 분봉 누락 시).
    processed judge DB 단일 SoT 사용. raw minutes.db / minutes_nxt.db direct read 금지.
    판정 윈도우 밖 분봉은 배제.
    """
    if not JUDGE_MINUTES_DB.exists() or JUDGE_MINUTES_DB.stat().st_size == 0:
        return None
    try:
        conn = _connect_cached(JUDGE_MINUTES_DB)
        day_bars: dict[str, list[tuple[str, float, float, float]]] = defaultdict(list)
        fwd_set = set(fwd_dates)
        start_dt = f"{fwd_dates[0]} 00:00"
        end_dt = _minute_dt_bounds(fwd_dates[-1])[1]
        cur = conn.execute(
            "SELECT dt, high, low, close FROM minute_bars "
            "WHERE code=? AND dt>=? AND dt<? ORDER BY dt ASC",
            (code, start_dt, end_dt),
        )
        for dt, h, lo, c in cur:
            if dt[:10] not in fwd_set:
                continue
            if not _in_judge_window(dt[11:16]):
                continue
            day_bars[dt[:10]].append((dt, float(h), float(lo), float(c)))
    except Exception as exc:
        log(f"FAIL: judge minutes query: {type(exc).__name__}")
        return None

    flat: list[tuple[str, float, float, float]] = []
    for fd in fwd_dates:
        if not day_bars.get(fd):
            return None  # 한 거래일 분봉 누락 = 미커버
        flat.extend(day_bars[fd])
    return flat


def load_entry_day_afterhours_minutes(
    code: str, pick_date: str
) -> list[tuple[str, float, float, float]] | None:
    """진입일 15:20 이후 장후/NXT 분봉.

    D0 판정은 일봉 fallback 을 쓰지 않는다. 일봉 high/low 는 15:20 이전 가격을 섞기
    때문에 당일 진입 후 익절 판단에는 부적합하다. 실 NXT 분봉이 있을 때만 D0 익절을
    확정한다.
    """
    if not JUDGE_MINUTES_DB.exists() or JUDGE_MINUTES_DB.stat().st_size == 0:
        return None
    start_dt = f"{pick_date} {ENTRY_DAY_AFTERHOURS_START}"
    end_dt = f"{pick_date} {ENTRY_DAY_AFTERHOURS_END}"
    try:
        conn = _connect_cached(JUDGE_MINUTES_DB)
        rows = conn.execute(
            "SELECT dt, high, low, close FROM minute_bars "
            "WHERE code=? AND dt>=? AND dt<=? ORDER BY dt ASC",
            (code, start_dt, end_dt),
        ).fetchall()
    except Exception as exc:
        log(f"WARN: entry-day judge minutes query skip: {type(exc).__name__}")
        return None

    out: list[tuple[str, float, float, float]] = []
    for dt, h, lo, c in rows:
        if not _in_judge_window(dt[11:16]):
            continue
        out.append((dt, float(h), float(lo), float(c)))
    return out or None


def _outcome_from_sim(
    sim: dict[str, Any], *, same_day_afterhours: bool = False
) -> dict[str, Any]:
    """simulate_* 결과를 compute_result outcome schema 로 변환."""
    current_state = BUCKET_TO_STATE.get(sim["bucket"], "expired_loss")
    result = {
        "final_price": int(round(sim["exit_price"])),
        "final_pnl_pct": round(sim["pnl_pct"], 2),
        "watered": bool(sim["martingaled"]),
        "result_date": sim["result_date"],
        "mdd_pct": sim.get("mdd_pct"),
        "mdd_peak_pct": sim.get("mdd_peak_pct"),
    }
    if same_day_afterhours:
        result["same_day_afterhours"] = True
    return {
        "current_state": current_state,
        "result": result,
        "provisional": None,
        "bucket_internal": sim["bucket"],
    }


def simulate_minute_touch(
    p0: int, flat: list[tuple[str, float, float, float]], window_complete: bool = True
) -> dict[str, Any]:
    """compound_account_bt.py:72-201 simulate_minute_unit verbatim 패턴 복제.

    같은 분봉 양터치 = low(물타기) 보수 우선. 다른 분봉이면 dt 이른 쪽.
    bucket ∈ {phase1_profit, phase2_profit, phase2_close_profit,
              phase2_close_loss, hold_close_profit, hold_close_loss}

    pnl_pct 본문 = 백테스트 SoT `gap75_rule_bt.py:491-492` ret_pct 정합:
      ret_pct = pnl / invested_won × 100 = pnl_per_unit / invested_units × 100
    - 1차 익절 (invested_units=1): pnl_pct = (TAKE_PROFIT_RATIO - 1) × 100 = +3.2%
    - 물타기 익절 (invested_units=2, 1배): pnl_pct = 2 × (1.032 - 1) / 2 × 100 = +3.2%
    - 물타기 만기청산: pnl_pct = (last_close / avg - 1) × 100 (유닛 약분)
    - 미물타기 만기청산: pnl_pct = (last_close / p0 - 1) × 100

    return:
      bucket: str
      pnl_pct: float (실질 ROI %, 백테스트 SoT 정합)
      martingaled: bool
      avg: float | None (물타기 평단)
      exit_dt: str (분봉 dt or 마지막 분봉 dt)
      exit_price: float
      result_date: str (exit_dt[:10])
    """
    target1 = p0 * TAKE_PROFIT_RATIO  # 1차 익절선 +3.2%
    add_trigger = p0 * WATERING_RATIO  # 물타기 트리거 -6.4%
    add_price = p0 * WATERING_RATIO  # 물타기 체결가 (트리거 = 체결, spec)

    # ROI 본문 익절 % = TAKE_PROFIT_RATIO - 1 (백테스트 SoT 정합, invested_units 약분 후 동일)
    take_profit_roi_pct = (TAKE_PROFIT_RATIO - 1.0) * 100.0

    martingaled = False
    avg: float | None = None
    target2: float | None = None

    result_bucket: str | None = None
    result_pnl_pct: float | None = None
    exit_dt: str | None = None
    exit_price: float | None = None
    last_dt = flat[-1][0]
    last_close = flat[-1][3]

    # MDD (Q-20260608-132): 진입평단(P0/avg) 기준 + 보유 중 고점(running peak) 기준 두 정의.
    # compound_account_bt.simulate_minute_unit 정합. 음수=낙폭, 0=무낙폭.
    worst_avg = 0.0  # 진입평단(물타기 전 P0, 후 avg) 대비 최저 미실현 낙폭%
    peak_high: float | None = None
    mdd_peak = 0.0  # 보유 중 고점 대비 최대 낙폭%(표준 MDD)

    for dt, h, lo, _c in flat:
        # 보유 중 고점 대비 낙폭 (break 前 전 분봉 누적)
        if peak_high is None or h > peak_high:
            peak_high = h
        if peak_high:
            dd_peak = lo / peak_high - 1.0
            if dd_peak < mdd_peak:
                mdd_peak = dd_peak
        # 진입평단 기준 낙폭 (물타기 후 avg, 전 P0)
        ref = avg if (martingaled and avg) else p0
        dd_avg = lo / ref - 1.0
        if dd_avg < worst_avg:
            worst_avg = dd_avg
        if not martingaled:
            hit_take = h >= target1
            hit_add = lo <= add_trigger
            if hit_take and hit_add:
                # 동일 분봉 양터치 → 보수적 low(물타기) 우선
                martingaled = True
                # 물타기 1배: avg = 2 / (1/p0 + 1/add_price) (unit 무관, 총 2유닛)
                avg = TOTAL_UNITS_AFTER_WATER / (1.0 / p0 + WATERING_WEIGHT / add_price)
                target2 = avg * TAKE_PROFIT_RATIO
                if h >= target2:
                    # 물타기 후 익절 ROI: 3×(1.032-1)/3 = +3.2% (백테스트 SoT 정합)
                    result_bucket = "phase2_profit"
                    result_pnl_pct = take_profit_roi_pct
                    exit_dt = dt
                    exit_price = target2
                    break
                continue
            elif hit_take:
                # +3.2% 먼저 → 1차 익절 ROI: (1.032-1) = +3.2%
                result_bucket = "phase1_profit"
                result_pnl_pct = take_profit_roi_pct
                exit_dt = dt
                exit_price = target1
                break
            elif hit_add:
                # -6.4% 먼저 → 물타기
                martingaled = True
                avg = TOTAL_UNITS_AFTER_WATER / (1.0 / p0 + WATERING_WEIGHT / add_price)
                target2 = avg * TAKE_PROFIT_RATIO
                if h >= target2:
                    result_bucket = "phase2_profit"
                    result_pnl_pct = take_profit_roi_pct
                    exit_dt = dt
                    exit_price = target2
                    break
                continue
            # 미터치 → 다음 분봉
        else:
            # Phase2: avg 기준 target2 도달 체크
            if target2 is not None and h >= target2:
                result_bucket = "phase2_profit"
                result_pnl_pct = take_profit_roi_pct
                exit_dt = dt
                exit_price = target2
                break

    if result_bucket is None:
        # 익절·물타기 미트리거. 윈도우(D+1~만기일)가 완성됐으면 만기일 종가 만기청산 확정.
        # 미완성(forward < 만기일수)이면 아직 결판 안 남 = 미확정 → "unsettled" 시그널
        # (호출부 compute_result 가 final 결과는 내지 않고 running 유지 — lead 옵션 B).
        # 단 보유 중 잠정 미실현 ROI(현재까지 마지막 종가 기준) + MDD 는 함께 반환 —
        # 실데이터(실 분봉/일봉)만, 추정 0 (FLR-AGT-002). 손실 회피 아닌 실현 미룸 정직 노출.
        if not window_complete:
            ref = avg if (martingaled and avg) else p0
            prov_pnl = (last_close / ref - 1.0) * 100.0
            return {
                "bucket": "unsettled",
                "provisional_pnl_pct": round(prov_pnl, 3),
                "martingaled": martingaled,
                "avg": avg,
                "last_close": float(last_close),
                "mdd_pct": round(worst_avg * 100.0, 3),
                "mdd_peak_pct": round(mdd_peak * 100.0, 3),
            }
        # 윈도우 끝(만기 종가) 만기청산
        exit_dt = last_dt
        exit_price = last_close
        if martingaled and avg is not None:
            # 물타기 만기청산 ROI: pnl_per_unit/invested_units = 3×(last_close/avg-1)/3
            # = (last_close/avg - 1) × 100 (3 약분, 백테스트 SoT 정합)
            pnl_pct = (last_close / avg - 1.0) * 100.0
            result_bucket = (
                "phase2_close_profit" if pnl_pct > 0 else "phase2_close_loss"
            )
            result_pnl_pct = pnl_pct
        else:
            # 미물타기 만기청산 ROI: (last_close/p0 - 1) × 100
            pnl_pct = (last_close / p0 - 1.0) * 100.0
            result_bucket = "hold_close_profit" if pnl_pct > 0 else "hold_close_loss"
            result_pnl_pct = pnl_pct

    assert result_bucket is not None
    assert result_pnl_pct is not None
    assert exit_dt is not None
    assert exit_price is not None
    return {
        "bucket": result_bucket,
        "pnl_pct": result_pnl_pct,
        "martingaled": martingaled,
        "avg": avg,
        "exit_dt": exit_dt,
        "exit_price": float(exit_price),
        "result_date": exit_dt[:10],
        "mdd_pct": round(worst_avg * 100.0, 3),  # 진입평단 기준 최대 낙폭%
        "mdd_peak_pct": round(mdd_peak * 100.0, 3),  # 보유 중 고점 대비 최대 낙폭%
    }


def simulate_dailybar_touch(
    p0: int, fwd: list[tuple[str, int, int, int, int]], window_complete: bool = True
) -> dict[str, Any]:
    """분봉 부재 시 일봉 forward N일 fallback (martingale_bt.py:254-348 verbatim 패턴).

    분봉 minute touch 정확도가 가장 높지만 minutes.db 미커버 시 일봉 H/L touch로 대체.
    bucket / pnl_pct schema는 simulate_minute_touch와 동일.
    pnl_pct = ROI % (gap75_rule_bt.py:491-492 ret_pct 정합).
    """
    target1 = p0 * TAKE_PROFIT_RATIO
    add_trigger = p0 * WATERING_RATIO
    add_price = p0 * WATERING_RATIO

    # ROI 본문 익절 % (백테스트 SoT 정합, invested_units 약분 후 동일)
    take_profit_roi_pct = (TAKE_PROFIT_RATIO - 1.0) * 100.0

    martingaled = False
    avg: float | None = None
    target2: float | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    result_bucket: str | None = None
    result_pnl_pct: float | None = None

    # MDD (Q-20260608-132 일봉 fallback 보강): 분봉 부재 시 일봉 H/L 기준 MDD 2종 산출.
    # simulate_minute_touch 정의 정합 — 진입평단(P0/avg) 기준 + 보유 중 고점 기준.
    # 일봉 granularity 라 분봉보다 거칠지만 정직(추정 아님, 실 일봉 H/L). 보유일(exit 포함)까지만 누적.
    worst_avg = 0.0  # 진입평단(물타기 전 P0, 후 avg) 대비 최저 미실현 낙폭%
    peak_high: float | None = None
    mdd_peak = 0.0  # 보유 중 고점 대비 최대 낙폭%(표준 MDD)

    def _track_mdd(h: int, lo: int) -> None:
        nonlocal worst_avg, peak_high, mdd_peak
        # 보유 중 고점 대비 낙폭 (당일 고점 갱신 후 당일 저점으로 낙폭 측정)
        if peak_high is None or h > peak_high:
            peak_high = h
        if peak_high:
            dd_peak = lo / peak_high - 1.0
            if dd_peak < mdd_peak:
                mdd_peak = dd_peak
        # 진입평단 기준 낙폭 (물타기 후 avg, 전 P0)
        ref = avg if (martingaled and avg) else p0
        dd_avg = lo / ref - 1.0
        if dd_avg < worst_avg:
            worst_avg = dd_avg

    for _di, (date, _o, h, lo, _c) in enumerate(fwd):
        # 당일 H/L 로 MDD 누적 (touch-exit 당일 포함 — 일봉 보수적 정직).
        _track_mdd(h, lo)
        if not martingaled:
            hit_take = h >= target1
            hit_add = lo <= add_trigger
            if hit_take and hit_add:
                # 같은 날 양터치 → 물타기 보수 우선
                martingaled = True
                avg = TOTAL_UNITS_AFTER_WATER / (1.0 / p0 + WATERING_WEIGHT / add_price)
                target2 = avg * TAKE_PROFIT_RATIO
                if h >= target2:
                    result_bucket = "phase2_profit"
                    result_pnl_pct = take_profit_roi_pct
                    exit_date = date
                    exit_price = target2
                    break
                continue
            elif hit_take:
                result_bucket = "phase1_profit"
                result_pnl_pct = take_profit_roi_pct
                exit_date = date
                exit_price = float(target1)
                break
            elif hit_add:
                martingaled = True
                avg = TOTAL_UNITS_AFTER_WATER / (1.0 / p0 + WATERING_WEIGHT / add_price)
                target2 = avg * TAKE_PROFIT_RATIO
                if h >= target2:
                    result_bucket = "phase2_profit"
                    result_pnl_pct = take_profit_roi_pct
                    exit_date = date
                    exit_price = target2
                    break
                continue
        else:
            if target2 is not None and h >= target2:
                result_bucket = "phase2_profit"
                result_pnl_pct = take_profit_roi_pct
                exit_date = date
                exit_price = target2
                break

    if result_bucket is None:
        # 익절·물타기 미트리거. 윈도우 미완성(forward < 만기일수)이면 미확정 → "unsettled"
        # 시그널 (compute_result 가 final 결과는 내지 않고 running 유지 — lead 옵션 B).
        # 보유 중 잠정 미실현 ROI(마지막 일봉 종가 기준) + MDD 함께 반환 — 실 일봉만,
        # 추정 0 (FLR-AGT-002). 손실 회피 아닌 실현 미룸 정직 노출.
        last_date, _o, _h, _lo, last_close = fwd[-1]
        if not window_complete:
            ref = avg if (martingaled and avg) else p0
            prov_pnl = (last_close / ref - 1.0) * 100.0
            return {
                "bucket": "unsettled",
                "provisional_pnl_pct": round(prov_pnl, 3),
                "martingaled": martingaled,
                "avg": avg,
                "last_close": float(last_close),
                "mdd_pct": round(worst_avg * 100.0, 3),
                "mdd_peak_pct": round(mdd_peak * 100.0, 3),
            }
        # 만기 종가 만기청산
        exit_date = last_date
        exit_price = float(last_close)
        if martingaled and avg is not None:
            # 물타기 만기청산 ROI: (last_close/avg - 1) × 100 (유닛 약분)
            pnl_pct = (last_close / avg - 1.0) * 100.0
            result_bucket = (
                "phase2_close_profit" if pnl_pct > 0 else "phase2_close_loss"
            )
            result_pnl_pct = pnl_pct
        else:
            # 미물타기 만기청산 ROI: (last_close/p0 - 1) × 100
            pnl_pct = (last_close / p0 - 1.0) * 100.0
            result_bucket = "hold_close_profit" if pnl_pct > 0 else "hold_close_loss"
            result_pnl_pct = pnl_pct

    assert result_bucket is not None
    assert result_pnl_pct is not None
    assert exit_date is not None
    assert exit_price is not None
    return {
        "bucket": result_bucket,
        "pnl_pct": result_pnl_pct,
        "martingaled": martingaled,
        "avg": avg,
        "exit_dt": exit_date,
        "exit_price": float(exit_price),
        "result_date": exit_date,
        # 일봉 H/L 기준 MDD (분봉보다 거칠지만 추정 아닌 실 일봉 H/L — Q-20260608-132 보강).
        "mdd_pct": round(worst_avg * 100.0, 3),  # 진입평단 기준 최대 낙폭%
        "mdd_peak_pct": round(mdd_peak * 100.0, 3),  # 보유 중 고점 대비 최대 낙폭%
    }


def _simulate_window(
    code: str, pick_date: str, p0: int, forward_n: int
) -> dict[str, Any] | None:
    """단일 윈도우(forward_n 거래일) sim. 분봉 우선, 일봉 fallback.

    window_complete = (forward 일봉이 forward_n 까지 다 찼는지). 미완성이면 익절·물타기
    확정만 결과로 내고, 만기청산은 미확정(unsettled bucket)으로 보류 (lead 옵션 B).
    return: sim dict 또는 None (forward 일봉 전무 = 판정 불가).
    """
    fwd_daily = load_forward_dailybars(code, pick_date, forward_n)
    if not fwd_daily:
        return None
    window_complete = len(fwd_daily) >= forward_n
    fwd_dates = [r[0] for r in fwd_daily]

    flat = load_forward_minutes(code, fwd_dates)
    if flat:
        return simulate_minute_touch(p0, flat, window_complete)
    return simulate_dailybar_touch(p0, fwd_daily, window_complete)


# ── 만기(축④) 핸들러 — 레지스트리 등록형 (open-for-extension) ────────────────
# 새 만기 모드(예 "water_d5" / "all_d10" / 신규 연장 규칙)는 @EXPIRY_MODES.register("name")
# 핸들러 1개 추가만으로 엔진 무수정 동작. 핸들러 시그니처: (sim, reached) -> bool
# (D+water 윈도우로 exit 연장할지). 이미 익절(reached) / D+water<=D+base 인 경우는
# _should_extend_to_water 공통 가드가 먼저 False 차단 (핸들러는 순수 정책만).


@EXPIRY_MODES.register("all_d3")
def _expiry_all_d3(sim: dict[str, Any], reached: bool) -> bool:
    """[all_d3] 전종목 D+base — 연장 0건."""
    return False


@EXPIRY_MODES.register("water_d6")
def _expiry_water_d6(sim: dict[str, Any], reached: bool) -> bool:
    """[water_d6] 물타기 거래 AND D+base 만기(미익절) 만 D+water 연장."""
    return bool(sim.get("martingaled"))


@EXPIRY_MODES.register("all_d6")
def _expiry_all_d6(sim: dict[str, Any], reached: bool) -> bool:
    """[all_d6] D+base 만기(미익절) 전종목 D+water 연장 (물타기 무관)."""
    return True


def _should_extend_to_water(sim: dict[str, Any], reached: bool) -> bool:
    """D+water 연장 여부 — 공통 가드 + 만기 핸들러(EXPIRY_MODE) dispatch.

    공통 가드: 이미 익절(reached) 또는 D+water<=D+base 면 무연장 (어느 모드든).
    그 외는 프로파일 EXPIRY_MODE 키로 레지스트리 핸들러 lookup (미등록 시 ValueError,
    silent fallback 금지 — FLR-AGT-002).
    """
    if reached or FORWARD_D6 <= FORWARD_D3:
        return False
    return bool(EXPIRY_MODES.get(EXPIRY_MODE)(sim, reached))


def _display_forward_n(watered: bool) -> int:
    """만기일 표시용 forward 일수 — 연장 정책과 동일 핸들러 재사용 (정합).

    만기(reached=False) 가정 + martingaled=watered 로 만기 핸들러에 물어, D+water 까지
    연장되는 거래면 FORWARD_D6, 아니면 FORWARD_D3. compute_result 의 실제 연장과 정합.
    """
    if _should_extend_to_water({"martingaled": watered}, reached=False):
        return FORWARD_D6
    return FORWARD_D3


def compute_result(
    code: str, pick_date: str, p0: int, *, include_entry_day_afterhours: bool = False
) -> dict[str, Any] | None:
    """만기 도달 시 result 산출 — 만기 enum(EXPIRY_MODE) 별 D+base/D+water 분기.

    2-pass (백테스트 build_picks_d6 정합):
      1) D+base 윈도우 sim → martingaled / 익절 판정.
      2) _should_extend_to_water 가 True 면 D+water 윈도우로 exit 재시뮬:
           * D+water 완비 → 결과 채택 (exit 연장).
           * D+water 미완비 → unsettled → running 유지 (추정 0, 보유중).
         False 면 D+base 결과 그대로.

    1순위: minutes.db 분봉 양터치 / 2순위: dailybars H/L fallback.
    실패·미확정 시 None (current_state running 유지, graceful).
    """

    def _running(sim_unsettled: dict[str, Any]) -> dict[str, Any]:
        """미확정(보유중) → current_state running + 잠정 미실현 ROI/MDD (실데이터만)."""
        return {
            "current_state": "running",
            "result": None,
            "provisional": {
                "provisional_pnl_pct": sim_unsettled.get("provisional_pnl_pct"),
                "watered": bool(sim_unsettled.get("martingaled")),
                "mdd_pct": sim_unsettled.get("mdd_pct"),
                "mdd_peak_pct": sim_unsettled.get("mdd_peak_pct"),
                "last_close": sim_unsettled.get("last_close"),
            },
            "bucket_internal": "unsettled",
        }

    # ── pass 0: D0 15:20 이후 장후/NXT 분봉 ────────────────────────────────
    # 백테스트의 D0 장외 익절 정책과 운영 카드 판정을 일치시킨다. 일봉 fallback 금지.
    if include_entry_day_afterhours:
        sim0_bars = load_entry_day_afterhours_minutes(code, pick_date)
        if sim0_bars:
            sim0 = simulate_minute_touch(p0, sim0_bars, window_complete=False)
            if sim0.get("bucket") != "unsettled":
                return _outcome_from_sim(sim0, same_day_afterhours=True)
            # D0 미확정이면 아래 D+base 판정으로 계속 간다. D+base 일봉이 아직 없으면
            # 마지막 return None 이 아니라 D0 실데이터 기반 provisional 로 running 유지.
            sim0_unsettled = sim0
        else:
            sim0_unsettled = None
    else:
        sim0_unsettled = None

    # ── pass 1: D+base 윈도우 ───────────────────────────────────────────────
    sim = _simulate_window(code, pick_date, p0, FORWARD_D3)
    if sim is None:
        if sim0_unsettled is not None:
            return _running(sim0_unsettled)
        return None
    if sim.get("bucket") == "unsettled":
        # D+base 윈도우 미완성 + 익절·물타기 미트리거 = 미확정 → running + 잠정 미실현.
        return _running(sim)

    reached = sim["bucket"] in ("phase1_profit", "phase2_profit")

    # ── pass 2: 만기 enum 별 D+water 연장 ───────────────────────────────────
    if _should_extend_to_water(sim, reached):
        sim6 = _simulate_window(code, pick_date, p0, FORWARD_D6)
        if sim6 is None:
            return None
        if sim6.get("bucket") == "unsettled":
            # D+water forward 미완비(미래·미커버) → 미청산(보유중) + 잠정 미실현 (추정 0).
            return _running(sim6)
        sim = sim6

    return _outcome_from_sim(sim)


def compute_pm320_pick(
    code: str,
    pick_date: str,
    is_pick: bool,
    p0: int,
    today: str,
) -> dict[str, Any]:
    """DSN-001 §1 schema verbatim — pm320_pick 단일 nested 객체 build.

    계산식 source: send_kakao_message.py L310~L314 + 대표 2026-06-09 재확정 (물타기 1배).
    만기: 물타기 거래 D+6 / 물타기 안 한 거래 D+3.
    시점 분기 (lead 옵션 B): today > pick_date 이면 결과 판정 시도. compute_result 가
    미확정(윈도우 미완성 + 미트리거) 시 None 반환 → running graceful 유지.
    """
    # send_kakao_message.py L311 verbatim
    watering_target = round(p0 * WATERING_RATIO)
    # send_kakao_message.py L312 verbatim
    take_profit_target = round(p0 * TAKE_PROFIT_RATIO)
    # 물타기 1배 평단 = (P0 + 1배×P0×0.936) / 2 (총 2유닛). 익절선 = 평단 × 1.032.
    avg_after_watering = (
        p0 + WATERING_WEIGHT * p0 * WATERING_RATIO
    ) / TOTAL_UNITS_AFTER_WATER
    take_profit_after_watering = round(avg_after_watering * TAKE_PROFIT_RATIO)

    # d_offset (오늘이 D 기준 몇 번째 거래일인지)
    d_offset = count_trading_days_between(pick_date, today)

    # outcome 종합 — current_state / pnl / watered / 잠정 MDD 를 한 dict 로 모은다.
    # (개별 지역변수 분산 대입은 lint --unsafe-fixes 가 unused 로 오제거하므로 dict 로 보존.)
    info: dict[str, Any] = {
        "current_state": "running",
        "current_pnl_pct": 0.0,
        "result_obj": None,
        "watered": False,
        "mdd_pct": None,
        "mdd_peak_pct": None,
    }

    # early-exit 인정 (lead 옵션 B): 만기 도달 전이라도 윈도우(D+1~) 내에서 이미
    # 익절/물타기/손절이 확정됐으면 final 결과 표시. compute_result 가 미확정이면
    # running + 잠정 미실현 ROI/MDD 반환 (실데이터만, 추정 0 — FLR-AGT-002).
    # today >= pick_date 일 때 판정 시도. D0 는 15:20 이후 NXT/장후 실분봉만 사용한다.
    if today >= pick_date:
        outcome = compute_result(
            code,
            pick_date,
            p0,
            include_entry_day_afterhours=True,
        )
        if outcome is not None:
            info["current_state"] = outcome["current_state"]
            r = outcome.get("result")
            if r is not None:
                # 확정 결과 (익절/만기청산)
                info["result_obj"] = r
                info["current_pnl_pct"] = float(r["final_pnl_pct"])
                info["watered"] = bool(r.get("watered"))
                info["mdd_pct"] = r.get("mdd_pct")
                info["mdd_peak_pct"] = r.get("mdd_peak_pct")
            else:
                # running + 잠정 미실현 (보유중) — 손실 회피 아닌 실현 미룸 정직 노출.
                prov = outcome.get("provisional") or {}
                pv = prov.get("provisional_pnl_pct")
                info["current_pnl_pct"] = float(pv) if pv is not None else 0.0
                info["watered"] = bool(prov.get("watered"))
                info["mdd_pct"] = prov.get("mdd_pct")
                info["mdd_peak_pct"] = prov.get("mdd_peak_pct")
        # outcome None = forward 일봉 전무 → running pnl 0 (graceful, DSN §1 4종 enum 정합)

    current_state = info["current_state"]
    current_pnl_pct = info["current_pnl_pct"]
    result_obj = info["result_obj"]
    mdd_pct = info["mdd_pct"]
    mdd_peak_pct = info["mdd_peak_pct"]

    # 만기 거래일 (표시용) — compute_result 연장 정책과 동일 핸들러 재사용 (정합).
    # 만기 핸들러(EXPIRY_MODES)에 reached=False(만기 가정) + martingaled=watered 를 주어
    # "이 거래가 D+water 까지 연장되는가" 를 묻고, True 면 D+water 표시 / 아니면 D+base.
    forward_n = _display_forward_n(info["watered"])
    expiry_date = add_trading_days(pick_date, forward_n)

    return {
        "is_pick": is_pick,
        "pick_date": pick_date,
        "entry_price": p0,
        "watering_target_price": watering_target,
        "watering_weight": WATERING_WEIGHT_LABEL,
        "take_profit_target_price": take_profit_target,
        "take_profit_after_watering_price": take_profit_after_watering,
        "expiry_date": expiry_date,
        "current_state": current_state,
        "current_pnl_pct": current_pnl_pct,
        # 보유중(running) 잠정 미실현 MDD (실데이터만, 추정 0). result 확정 시는 result.mdd_pct
        # 가 정본이라 None. 미청산 보유중일 때만 잠정 낙폭 노출 (실현 미룸 정직, FLR-AGT-002).
        "current_mdd_pct": mdd_pct if result_obj is None else None,
        "current_mdd_peak_pct": mdd_peak_pct if result_obj is None else None,
        "d_offset": d_offset,
        "result": result_obj,
    }


def atomic_write_json(path: Path, data: Any) -> None:
    """atomic write (race 봉쇄, send_kakao_message.py L112~L116 패턴 정합)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def build_history(date_str: str) -> dict[str, Any] | None:
    """본일 카드 universe → pm320_history 본문 build.

    옵션 C 본문 (대표 결정 verbatim): 모든 카드 종목에 추천 정보 + PICK 종목엔 별도 강조.
    """
    picks = load_picks(date_str)
    if picks is None:
        log("EXIT: no picks (graceful)")
        return None

    cards = load_card_universe(date_str)
    if cards is None:
        log("EXIT: no card universe (graceful)")
        return None

    picked_code = (picks.get("picked") or {}).get("code")
    if not picked_code:
        log("WARN: picks.picked.code missing")
        picked_code = None

    if not require_fresh_judge_minutes(date_str):
        log("EXIT: judge minutes freshness gate failed")
        return None

    today = datetime.now(KST).strftime("%Y-%m-%d")

    out_stocks: list[dict[str, Any]] = []
    pick_count = 0
    virtual_count = 0
    skip_count = 0
    expired_count = 0
    running_count = 0

    for card in cards:
        code = card.get("code")
        name = card.get("name")
        if not code:
            continue

        p0 = load_close_price(code, date_str)
        if p0 is None or p0 <= 0:
            # P0 미조회 = graceful skip (DSN-001 §1 schema 정합 못 함)
            log(f"SKIP: P0 missing for {code} ({name})")
            skip_count += 1
            continue

        is_pick = code == picked_code
        pm320_pick = compute_pm320_pick(
            code=code,
            pick_date=date_str,
            is_pick=is_pick,
            p0=p0,
            today=today,
        )

        out_stocks.append(
            {
                "code": code,
                "name": name,
                "pm320_pick": pm320_pick,
            }
        )
        if is_pick:
            pick_count += 1
        else:
            virtual_count += 1
        if pm320_pick["current_state"] == "running":
            running_count += 1
        else:
            expired_count += 1

    history = {
        "date": date_str,
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "picked_code": picked_code,
        "stocks": out_stocks,
        "_meta": {
            "pick_count": pick_count,
            "virtual_count": virtual_count,
            "skip_count": skip_count,
            "total_cards": len(cards),
            "expired_count": expired_count,
            "running_count": running_count,
        },
    }
    log(
        f"BUILD: date={date_str} picked={picked_code} "
        f"pick={pick_count} virtual={virtual_count} skip={skip_count} "
        f"expired={expired_count} running={running_count} total={len(cards)}"
    )
    return history


# 청산 완료로 간주하는 current_state (running·미확정 제외). _LIVE_EXIT_KIND 키와 일치.
_LIVE_SETTLED_STATES = frozenset({"taken_profit", "expired_gain", "expired_loss"})


def _live_settled_picks_after(
    cutoff_date: str,
    seen_keys: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """라이브 history 파일에서 *청산일* > cutoff_date 인 *청산 완료* PICK 을 청산일 asc 로 수집.

    backtest_ssot summary 는 검증 백테스트 timeline(4/8~cutoff) 만 담아 cutoff 이후
    라이브 실현 청산을 영원히 누락한다(RND-PM320-064 P1 stale). 본 함수가 그 tail 을
    채운다.

    cutoff 비교 키 = *청산일*(result_date) 이다. cutoff(=base last_settled_date)도 청산일
    이므로 동일 기준으로 비교해야 한다. 진입일 기준으로 비교하면 D+0 당일익절(진입=청산
    =cutoff)이 cutoff 경계(`<=`)에서 탈락한다(RND-PM320-064 2R 신규 P1: 6/15 진입·6/15
    청산 HL만도·후성 누락). running 픽은 청산일이 없으므로 자연 제외(settled 가 아님,
    호출부에서 running 카운트만 별도 반영).

    seen_keys: base backtest table 에 이미 든 (code, 청산일) 집합. 중복 append 차단
    (idempotent 보강). None 이면 dedup 안 함.
    """
    if seen_keys is None:
        seen_keys = set()
    out: list[dict[str, Any]] = []
    for fp in sorted(HISTORY_OUT_DIR.glob("*.json")):
        if fp.name == "summary.json":
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        date = d.get("date") or fp.stem
        for stock in d.get("stocks", []):
            pick = stock.get("pm320_pick") or {}
            if not pick.get("is_pick"):
                continue
            state = pick.get("current_state")
            # *청산 완료* 픽만. running 은 result_date 가 없어 진입일로 fallback 되면
            # cutoff 게이트를 잘못 통과한다(running 을 settled 로 오집계 → win_rate 분모
            # 오염). state 가 settled 3종일 때만 수집(running 은 호출부가 별도 카운트).
            if state not in _LIVE_SETTLED_STATES:
                continue
            result = pick.get("result") or {}
            # 청산일 = result_date. cutoff(=base last_settled_date)도 청산일이므로 동일
            # 기준 비교. result_date 결측 청산은 진입일 fallback(보수적, 거의 없음).
            exit_date = result.get("result_date") or date
            if not exit_date or exit_date <= cutoff_date:
                continue
            code = stock.get("code") or ""
            if (code, exit_date) in seen_keys:
                continue
            out.append(
                {
                    "date": date,
                    "exit_date": exit_date,
                    "code": code,
                    "name": stock.get("name"),
                    "state": state,
                    "pick": pick,
                }
            )
    out.sort(key=lambda r: (r["exit_date"], str(r.get("code") or "")))
    return out


def _live_running_picks_after(cutoff_date: str) -> int:
    """라이브 history 에서 진입일 > cutoff_date 인 *미청산(running)* PICK 수를 센다.

    settled tail 은 청산일 기준으로 잡지만(_live_settled_picks_after), running 은 청산일이
    없으므로 진입일(파일 date) 기준으로 cutoff 이후만 카운트한다. backtest_ssot base 가
    담지 못한 라이브 진행 픽을 summary.running 에 반영(미청산 추적 위젯 정합).
    """
    cnt = 0
    for fp in sorted(HISTORY_OUT_DIR.glob("*.json")):
        if fp.name == "summary.json":
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        date = d.get("date") or fp.stem
        if not date or date <= cutoff_date:
            continue
        for stock in d.get("stocks", []):
            pick = stock.get("pm320_pick") or {}
            if not pick.get("is_pick"):
                continue
            if pick.get("current_state") == "running":
                cnt += 1
    return cnt


def _per_pick_weight(table: list[dict[str, Any]], display_start: float) -> float | None:
    """backtest table 의 (잔고 변화율 / ret_pct) 로 1픽당 투입 비중을 역산.

    magic number(1/12 등) 하드코딩 회피 — profile 변경 시 backtest_detail 가 새 비중을
    담으므로 역산이 항상 정합. watered=False 인 첫 픽으로 산출(2배 비중 오염 회피).

    🔴 L1 가드 (_live 행 skip):
    summary.json self-read 시 이전 fire 에서 append 된 _live 행이 table 앞쪽에
    배치되는데, 해당 행의 balance_after 는 backtest 누적 잔고 위에 live tail 이
    이어붙은 값이므로 prev=display_start 와의 gap 이 수십 배로 벌어져 weight 가
    비정상적으로 폭등함 (예: 3.07 = 1/12 의 ~37배 → pnl 37배 오차).
    _live=True 행은 역산 대상·prev 갱신 모두 skip (잔고 오염 차단).

    🔴 L2 가드 (비중 sanity bound):
    역산 weight 가 합리 범위 [_WEIGHT_MIN, _WEIGHT_MAX] 밖이면 오염 신호 →
    fallback=_WEIGHT_FALLBACK + stderr 경고. 물리 한도:
      하한 0.005: 극소 슬롯(200슬롯 계좌라도 최소 0.5%)
      상한 0.30: 4슬롯 × 물타기 3유닛 / 12 = 1/4=0.25 + 안전마진 → 0.30
    """
    # L2 bound 상수 (profile 변경해도 물리 불가능치 차단)
    _WEIGHT_MIN = 0.005  # 하한: 극소 슬롯
    _WEIGHT_MAX = 0.30  # 상한: 4슬롯×3유닛/12 = 25% + 안전마진
    _WEIGHT_FALLBACK = 1.0 / 12.0  # known-good: 현 profile 기본값

    prev = display_start
    for row in table:
        # 🔴 L1: _live 행 skip — prev 갱신도 금지(잔고 오염 차단)
        if row.get("_live"):
            continue
        ret = row.get("ret_pct")
        bal = row.get("balance_after")
        watered = bool(row.get("watered"))
        if (
            not watered
            and isinstance(ret, (int, float))
            and ret
            and isinstance(bal, (int, float))
            and prev
        ):
            growth = bal / prev - 1.0  # = weight * ret/100
            weight = growth / (ret / 100.0)
            if weight > 0:
                # 🔴 L2: sanity bound — 범위 밖이면 오염 간주, fallback 사용
                if weight < _WEIGHT_MIN or weight > _WEIGHT_MAX:
                    print(
                        f"[build_card_history] 🚨 L2 weight sanity FAIL: "
                        f"역산 weight={weight:.6f} 범위 [{_WEIGHT_MIN}, {_WEIGHT_MAX}] 초과 "
                        f"— _live 오염 가능성. fallback={_WEIGHT_FALLBACK:.6f} 사용.",
                        file=__import__("sys").stderr,
                        flush=True,
                    )
                    return _WEIGHT_FALLBACK
                return weight
        if isinstance(bal, (int, float)):
            prev = bal
    return None


def _backtest_baseline_running(
    base: dict[str, Any], table: list[dict[str, Any]]
) -> int:
    """검증 백테스트의 baseline running(미청산 보유) 수를 오염 무관하게 산출.

    summary.json 은 _source=backtest_ssot 태그를 단 채 매 fire self-read 되므로, base.running
    을 그대로 신뢰하면 직전 fire 의 live running 누적분(오염치)을 물려받는다(FLR-AGT-002
    인플레이션의 원천). 우선순위:
      1) base 에 _backtest_baseline_running 박제값이 있으면 그대로(이미 immutable 고정).
      2) 박제값이 없고 base 가 아직 live 미머지(_live_tail_appended None & table 내 _live 행
         0건)면 base.running 이 곧 검증 백테스트 baseline(backfill_card_history_water1x_d6
         의 n_held_open). 본 값을 박제 시드로 채택.
      3) 박제값이 없고 base 가 이미 live 머지 흔적을 가진 오염 상태면, backtest table 은
         전부 청산(_live=None=settled)이라 baseline running=0(완결 백테스트). 라이브 보유분은
         호출부가 _live_running_picks_after 로 별도 가산하므로 0 이 정합(중복 회피).
    """
    staked = base.get("_backtest_baseline_running")
    if isinstance(staked, int):
        return staked
    has_live_artifact = base.get("_live_tail_appended") is not None or any(
        r.get("_live") for r in table
    )
    if not has_live_artifact:
        return int(base.get("running") or 0)
    return 0


def _merge_live_tail(base: dict[str, Any]) -> dict[str, Any]:
    """backtest_ssot summary(base)에 last_settled_date 이후 라이브 청산 PICK 을 append.

    - backtest_detail.table / equity_curve 에 라이브 청산 행을 잔고 연속으로 이어붙임
    - 집계(total_picks/settled/take_profit/expired_*/running/win_rate/last_settled_date) 갱신
    - 잔고는 직전 누적잔고 × (watered ? 2 : 1) × per_pick_weight × ret/100 으로 산출
      (backtest 와 동일 투입 모델; per_pick_weight 는 table 에서 역산)
    라이브 데이터(history)에 청산 픽이 없으면 base 를 그대로 반환(무변경, idempotent).
    """
    detail = base.get("backtest_detail")
    if not isinstance(detail, dict):
        return base
    table = detail.get("table")
    curve = detail.get("equity_curve")
    if not isinstance(table, list) or not isinstance(curve, list) or not table:
        return base

    cutoff = base.get("last_settled_date")
    if not isinstance(cutoff, str) or not cutoff:
        return base

    # base backtest table 에 이미 든 (code, 청산일) → 중복 append 차단(idempotent).
    seen_keys = {
        (str(r.get("code") or ""), str(r.get("exit_date") or r.get("date") or ""))
        for r in table
        if r.get("code")
    }
    live = _live_settled_picks_after(cutoff, seen_keys)
    running_count = _live_running_picks_after(cutoff)
    if not live and not running_count:
        return base

    display_start = (
        float(base.get("start_balance"))
        if isinstance(base.get("start_balance"), (int, float))
        else 10_000_000.0
    )
    weight = _per_pick_weight(table, display_start)
    if weight is None:
        log("SUMMARY: per-pick weight 역산 실패 — live tail merge skip (base 유지)")
        return base

    merged = dict(base)
    new_table = [dict(r) for r in table]
    new_curve = [dict(r) for r in curve]

    # 검증 백테스트 baseline running 을 immutable 로 박제(아래 _backtest_baseline_running).
    # summary.json 은 _source=backtest_ssot 태그를 단 채 매 fire self-read 되어 base 로
    # 재입력된다(build_summary). running/total_picks 를 base 값에 add_running 가산하면 base
    # 자신이 직전 fire 의 live running 을 이미 머금어 매 fire +1 인플레이션(FLR-AGT-002:
    # running 4→10, total 46→52, RND-PM320-064). settled/take_profit/win_rate 는 seen_keys
    # dedup(add_settled=0) 가드가 있어 idempotent 이지만 running/total_picks 에는 그 가드가
    # 없어 비대칭 누수 → running/total_picks 만 (baseline + 현재 live) 로 재계산(set, 가산 금지).
    baseline_running = _backtest_baseline_running(base, table)

    # 🔴 balance 초기값: 정렬 순서에 의존하지 않고 _live 행만 명시 필터링하여
    # 마지막 live 행의 balance_after 를 사용. 이는 _sort_live_table 이 live 행을 앞에,
    # backtest 행을 뒤에 배치하므로 table[-1] 이 backtest 마지막 행이 되어 잘못된
    # base 에서 live tail 이 누적되는 버그(변종 재발) 를 구조적으로 차단.
    # live 행이 없으면 backtest 마지막 행 fallback (초기 backtest-only 상태).
    _live_rows_with_bal = [
        r
        for r in table
        if r.get("_live") and isinstance(r.get("balance_after"), (int, float))
    ]
    if _live_rows_with_bal:
        # settlement_order 로 마지막 live 행 선택(정렬 순서 독립적)
        _last_live = max(
            _live_rows_with_bal,
            key=lambda r: (
                r.get("settlement_order")
                if isinstance(r.get("settlement_order"), int)
                else -1
            ),
        )
        balance = float(_last_live["balance_after"])
    elif isinstance(table[-1].get("balance_after"), (int, float)):
        balance = float(table[-1].get("balance_after"))
    else:
        balance = display_start
    last_seq = max(
        (
            r.get("settlement_order")
            for r in table
            if isinstance(r.get("settlement_order"), int)
        ),
        default=0,
    )

    add_settled = add_tp = add_loss = add_gain = 0
    add_running = running_count
    last_settled = cutoff
    appended = 0

    for rec in live:
        # live = 청산 완료 픽만(_live_settled_picks_after 가 청산일 게이트로 필터).
        state = rec["state"]
        add_settled += 1
        # last_settled = 청산일(exit_date) 최대값. 진입일(date)이 아니다 — 진입일로
        # 잡으면 D+N 청산이 진입일로 backdate 되어 cutoff 경계 버그 재발(2R 신규 P1).
        if rec["exit_date"] > last_settled:
            last_settled = rec["exit_date"]
        if state == "taken_profit":
            add_tp += 1
        elif state == "expired_loss":
            add_loss += 1
        elif state == "expired_gain":
            add_gain += 1

        pick = rec["pick"]
        result = pick.get("result") or {}
        ret = result.get("final_pnl_pct")
        watered = bool(result.get("watered"))
        if not isinstance(ret, (int, float)):
            # 청산인데 ret 결측 = false-fidelity 위험 → 잔고 미반영(카운트만)
            continue
        invested = balance * weight * (2.0 if watered else 1.0)
        pnl = round(invested * (ret / 100.0), 2)
        balance = round(balance + pnl, 2)
        exit_class = _live_exit_class_label(
            state,
            rec["date"],
            rec["exit_date"],
            same_day_afterhours=bool(result.get("same_day_afterhours")),
        )
        last_seq += 1
        new_table.append(
            {
                "date": rec["date"],
                "code": rec["code"],
                "name": rec["name"],
                "entry_price": pick.get("entry_price"),
                "exit_date": rec["exit_date"],
                "exit_class": exit_class,
                "ret_pct": ret,
                "pnl": pnl,
                "balance_after": balance,
                "watered": watered,
                "settlement_order": last_seq,
                "_live": True,
            }
        )
        # equity_curve 시점 = 잔고 변동(청산) 시점 = 청산일.
        new_curve.append(
            {"date": rec["exit_date"], "balance": balance, "name": rec["name"]}
        )
        appended += 1

    add_total = add_settled + add_running
    if add_total == 0:
        return base

    # equity_curve 끝점 재배치(fix 2): 본 fix 이전(구버전 d512118d, exit-date cutoff 도입 전)에
    # 머지된 _live 청산 행은 curve point 가 *진입일*에 baked 됐다(예: 고영 098460 진입 06-12 ·
    # 청산 06-15 인데 curve 끝점 date=06-12). abb25eac 이후 신규 머지는 exit_date 로 append
    # 하지만(L1480-1482), 이미 base.table 에 든 _live 행은 seen_keys dedup 으로 재머지에서
    # 빠져 stale 진입일 curve point 가 영구 고착된다. → new_table 의 _live 청산 행을 권위로
    # 삼아 balance_after 지문이 일치하는 curve point 의 date 를 exit_date 로 재기록(idempotent:
    # 이미 exit_date 면 변경 0). last_settled_date 도 _live 청산 최대 exit_date 로 전진.
    _live_settled_rows = [
        r
        for r in new_table
        if r.get("_live")
        and r.get("exit_date")
        and isinstance(r.get("balance_after"), (int, float))
    ]
    for r in _live_settled_rows:
        want_date = r["exit_date"]
        bal = r["balance_after"]
        for pt in new_curve:
            if pt.get("balance") == bal and pt.get("name") == r.get("name"):
                if pt.get("date") != want_date:
                    pt["date"] = want_date
                break
        if r["exit_date"] > last_settled:
            last_settled = r["exit_date"]
    new_curve.sort(key=lambda c: str(c.get("date") or ""))

    new_detail = dict(detail)
    new_detail["table"] = _sort_live_table(new_table)
    new_detail["equity_curve"] = new_curve
    merged["backtest_detail"] = new_detail

    # settled/take_profit/expired_* 는 seen_keys dedup 으로 add_*=0 게이트가 있어 가산이
    # idempotent(동일 청산 픽 재가산 0). exit_class 라벨('만기청산')은 gain/loss 를 구분하지
    # 못하므로 table 재집계가 불가 → base+add 가산 유지(가산이 이미 무누수).
    merged["settled"] = int(base.get("settled") or 0) + add_settled
    merged["take_profit"] = int(base.get("take_profit") or 0) + add_tp
    merged["expired_loss"] = int(base.get("expired_loss") or 0) + add_loss
    merged["expired_gain"] = int(base.get("expired_gain") or 0) + add_gain
    # running/total_picks 만 dedup 가드가 없어 매 fire +1 인플레이션(FLR-AGT-002: running
    # 4→10, total 46→52). base.running 가산 사슬을 절단하고 **재계산(set)**:
    #   running     = 검증 백테스트 baseline running + 현재 live running(running_count)
    #   total_picks = settled + running (동일 모집단 재합산)
    # baseline 을 immutable 박제(_backtest_baseline_running)해 self-read 누적분을 차단 →
    # 동일 입력이면 동일 출력(idempotent).
    merged["running"] = baseline_running + running_count
    merged["total_picks"] = merged["settled"] + merged["running"]
    merged["_backtest_baseline_running"] = baseline_running
    merged["last_settled_date"] = last_settled
    settled_total = merged["settled"]
    merged["win_rate"] = (
        round(100.0 * merged["take_profit"] / settled_total, 1)
        if settled_total
        else None
    )

    final_balance = round(balance, 2)
    merged["final_balance"] = final_balance
    merged["total_pnl"] = round(final_balance - display_start, 2)
    merged["total_return_pct"] = round((final_balance / display_start - 1.0) * 100.0, 4)
    if isinstance(merged.get("backtest_detail"), dict):
        # as_of = 집계 기준 데이터 마지막일(=최종 청산일). 빌드 시각(now)이 아니라
        # last_settled 로 정합 — 최상위 last_settled_date 와 동일 파일 내 일치
        # (RND-PM320-064 2R 권고3: as_of vs last_settled_date 불일치 해소).
        merged["backtest_detail"]["as_of"] = last_settled
    merged["generated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    merged["_live_tail_appended"] = appended

    # 🔴 L3: 산출 후 sanity 검증 — 불가능치 발견 시 경고 + production 출하 차단
    _l3_errors = _sanity_check_merged(merged, weight, display_start)
    if _l3_errors:
        import sys as _sys

        for _err in _l3_errors:
            print(
                f"[build_card_history] 🚨 L3 sanity FAIL: {_err}",
                file=_sys.stderr,
                flush=True,
            )
        # 출하 차단: merged 대신 base(오염 전) 반환 + 경고 누적
        print(
            f"[build_card_history] 🚨 L3 BLOCK: {len(_l3_errors)}건 불가능치 — "
            f"live tail merge 결과 폐기, base 보존 반환.",
            file=_sys.stderr,
            flush=True,
        )
        return base

    log(
        f"SUMMARY: live tail merge — base settled={base.get('settled')} "
        f"→ {settled_total} (+{add_settled} settled, +{add_running} running, "
        f"appended {appended} rows, last_settled={last_settled}, "
        f"win={merged['win_rate']}%)"
    )
    return merged


def _sanity_check_merged(
    merged: dict[str, Any],
    weight: float,
    display_start: float,
) -> list[str]:
    """🔴 L3 산출 후 불가능치 assertion (3종).

    (a) balance_after 음수 0건 — long-only 계좌는 음수 불가.
    (b) 단일 |pnl| ≤ (물타기:3유닛, 단순:1유닛) × |ret_pct| × 직전잔고.
        한도 위반 = weight 오염 신호.
    (c) 내재포지션(pnl÷ret_pct) ≤ 직전잔고 × (물타기?2:1) × weight × 1.05 (5% 허용오차).

    반환: 오류 메시지 list (빈 list = OK).
    """
    errors: list[str] = []
    table: list[dict[str, Any]] = merged.get("backtest_detail", {}).get("table") or []
    if not table:
        return errors

    for row in table:
        bal = row.get("balance_after")
        pnl = row.get("pnl")
        ret = row.get("ret_pct")
        watered = bool(row.get("watered"))
        name = row.get("name", "")
        date = row.get("date", "")

        # (a) balance_after 음수
        if isinstance(bal, (int, float)) and bal < 0:
            errors.append(
                f"(a) balance_after 음수: {date} {name} bal={bal:.0f} — long-only 불가"
            )

        # (b)(c) pnl·내재포지션 한도
        # prev_bal = balance_after - pnl (항등식 bal = prev + pnl, 순서-무관·정확)
        # merged table이 settlement_order 정렬로 live 픽이 앞에 올 수 있어
        # sequential 추적(display_start 묶임) 시 false positive 발생 → 행별 직접 산정.
        if (
            isinstance(pnl, (int, float))
            and isinstance(ret, (int, float))
            and ret
            and isinstance(bal, (int, float))
        ):
            prev_bal = bal - pnl
            if prev_bal > 0:
                n_units = 3.0 if watered else 1.0
                max_pnl = abs(prev_bal * weight * n_units)  # 최대 투입
                # (b) |pnl| ≤ max_invested × |ret/100|
                max_abs_pnl = max_pnl * (abs(ret) / 100.0) * 1.05  # 5% 허용오차
                if abs(pnl) > max_abs_pnl and max_abs_pnl > 0:
                    errors.append(
                        f"(b) |pnl| 한도 초과: {date} {name} |pnl|={abs(pnl):.0f} "
                        f"> max={max_abs_pnl:.0f} (weight={weight:.4f}, n_units={n_units}, "
                        f"ret={ret}%, prev_bal={prev_bal:.0f})"
                    )
                # (c) 내재포지션 = pnl ÷ (ret/100)
                implied_pos = abs(pnl) / (abs(ret) / 100.0) if ret else 0
                max_pos = prev_bal * weight * n_units * 1.05
                if implied_pos > max_pos and max_pos > 0:
                    errors.append(
                        f"(c) 내재포지션 초과: {date} {name} implied={implied_pos:.0f} "
                        f"> max={max_pos:.0f} (weight={weight:.4f}, n_units={n_units}, "
                        f"prev_bal={prev_bal:.0f})"
                    )

    # (d) settlement_order 연속성 검증 (live 행 한정)
    # live 행을 settlement_order 순 정렬 후 인접 행 간
    # |balance_after[i] - balance_after[i-1] - pnl[i]| ≤ 1원 항등식 점검.
    # 이번 버그(backtest base 혼입으로 인한 balance 점프)를 정확히 탐지하는 가드.
    live_rows_sorted = sorted(
        [
            r
            for r in table
            if r.get("_live")
            and isinstance(r.get("settlement_order"), int)
            and isinstance(r.get("balance_after"), (int, float))
            and isinstance(r.get("pnl"), (int, float))
        ],
        key=lambda r: r["settlement_order"],
    )
    for i in range(1, len(live_rows_sorted)):
        prev_r = live_rows_sorted[i - 1]
        curr_r = live_rows_sorted[i]
        expected = prev_r["balance_after"] + curr_r["pnl"]
        actual = curr_r["balance_after"]
        if abs(actual - expected) > 1:
            errors.append(
                f"(d) live 잔고 연속성 위반: so={curr_r['settlement_order']} "
                f"{curr_r.get('name', '')} "
                f"expected={expected:.2f} (prev_bal={prev_r['balance_after']:.2f} "
                f"+ pnl={curr_r['pnl']:.2f}) actual={actual:.2f} "
                f"diff={actual - expected:.2f} — balance 초기값 오염 의심"
            )

    return errors


_LIVE_EXIT_KIND = {
    "taken_profit": "익절",
    "expired_gain": "만기익절",
    "expired_loss": "만기손실",
}


def _live_exit_class_label(
    state: str,
    pick_date: str,
    exit_date: str,
    *,
    same_day_afterhours: bool = False,
) -> str:
    """라이브 청산 픽 exit_class 라벨을 본체 백테스트 포맷으로 산출.

    본체 table 은 'D+1 익절' / 'D0 장외 익절' / 'D+6 만기청산' 처럼 보유 거래일(D+N)을
    접두로 붙인다. 직전(1R) 구현은 'D+N' 없는 '익절' 단일 라벨이라 전수표 범례
    ('D+N=진입일로부터 N번째 거래일')와 행이 불일치했다(RND-PM320-064 2R P2).

    D+N = 진입일(exclusive) ~ 청산일(inclusive) 거래일 수(count_trading_days_between,
    market_closed_set 정합). same_day_afterhours(당일 장외 익절)면 본체와 동일하게
    'D0 장외 익절'.
    """
    kind = _LIVE_EXIT_KIND.get(state, state)
    if same_day_afterhours:
        return f"D0 장외 {kind}"
    d_n = count_trading_days_between(pick_date, exit_date)
    return f"D+{d_n} {kind}" if d_n > 0 else f"D0 {kind}"


def _sort_live_table(table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """settlement_order asc 로 정렬(없으면 exit_date). backfill _sort_table_by_settlement 정합."""
    return sorted(
        table,
        key=lambda r: (
            r.get("settlement_order")
            if isinstance(r.get("settlement_order"), int)
            else 10_000_000,
            str(r.get("exit_date") or r.get("date") or ""),
        ),
    )


def build_summary() -> dict[str, Any]:
    """Rebuild the served PM320 track-record summary from pm320_history files."""
    existing_ssot = HISTORY_OUT_DIR / "summary.json"
    if existing_ssot.exists():
        try:
            d = json.loads(existing_ssot.read_text(encoding="utf-8"))
            if d.get("_source") == "backtest_ssot":
                # backtest_ssot = 검증 백테스트 timeline(4/8~last_settled). 그 이후 라이브
                # 청산 PICK 을 tail 로 이어붙여 stale 차단(RND-PM320-064 P1). 라이브 청산
                # 0건이면 base 그대로(idempotent).
                merged = _merge_live_tail(d)
                if merged is d:
                    log(
                        "SUMMARY: preserve backtest_ssot summary "
                        "(live tail 0건, scan recompute skipped)"
                    )
                return merged
        except (OSError, json.JSONDecodeError):
            pass

    history_src = HOMEPAGE_DIR / "data" / "pm320_history"
    source_name = "serving"
    if not history_src.exists():
        history_src = HISTORY_OUT_DIR
        source_name = "main"

    total_picks = 0
    settled = 0
    take_profit = 0
    expired_loss = 0
    expired_gain = 0
    running = 0
    first_date: str | None = None
    last_settled_date: str | None = None
    settled_mdds: list[float] = []
    worst_mdd_pct: float | None = None
    worst_mdd_name: str | None = None
    worst_mdd_date: str | None = None
    tp_target_pcts: set[float] = set()

    for fp in sorted(history_src.glob("*.json")):
        if fp.name == "summary.json":
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        date = d.get("date") or fp.stem
        for stock in d.get("stocks", []):
            pick = stock.get("pm320_pick") or {}
            if not pick.get("is_pick"):
                continue

            total_picks += 1
            if first_date is None or date < first_date:
                first_date = date

            state = pick.get("current_state")
            if state == "running":
                running += 1
                continue

            settled += 1
            if last_settled_date is None or date > last_settled_date:
                last_settled_date = date
            if state == "taken_profit":
                take_profit += 1
            elif state == "expired_loss":
                expired_loss += 1
            elif state == "expired_gain":
                expired_gain += 1

            result = pick.get("result") or {}
            mdd = result.get("mdd_pct")
            if isinstance(mdd, (int, float)):
                settled_mdds.append(float(mdd))
                if worst_mdd_pct is None or mdd < worst_mdd_pct:
                    worst_mdd_pct = float(mdd)
                    worst_mdd_name = stock.get("name")
                    worst_mdd_date = date

            entry = pick.get("entry_price")
            tp_price = pick.get("take_profit_target_price")
            if (
                isinstance(entry, (int, float))
                and entry
                and isinstance(tp_price, (int, float))
            ):
                tp_target_pcts.add(round((tp_price / entry - 1.0) * 100.0, 1))

    win_rate = round(100.0 * take_profit / settled, 1) if settled else None
    avg_mdd_pct = (
        round(sum(settled_mdds) / len(settled_mdds), 1) if settled_mdds else None
    )
    take_profit_target_pct = (
        next(iter(tp_target_pcts)) if len(tp_target_pcts) == 1 else None
    )

    return {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "since": "2026-04-08",
        "first_pick_date": first_date,
        "last_settled_date": last_settled_date,
        "total_picks": total_picks,
        "settled": settled,
        "running": running,
        "take_profit": take_profit,
        "expired_loss": expired_loss,
        "expired_gain": expired_gain,
        "win_rate": win_rate,
        "_basis": "청산 완료 PICK 기준 (보유중 제외). 승률 = 익절 / 청산완료. 수익률 미표시.",
        "_source": source_name,
        "worst_mdd_pct": round(worst_mdd_pct, 1) if worst_mdd_pct is not None else None,
        "worst_mdd_name": worst_mdd_name,
        "worst_mdd_date": worst_mdd_date,
        "avg_mdd_pct": avg_mdd_pct,
        "take_profit_target_pct": take_profit_target_pct,
        "_mdd_basis": (
            "장중 최대낙폭 = 보유 중 진입평단 대비 최저 평가손실. "
            "청산 완료 픽 기준 (승률 분모와 동일 모집단)."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PM320 카드 universe pm320_history wrapper (Phase 2 통합 v2)"
    )
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: 본일 KST)")
    parser.add_argument(
        "--dry-run", action="store_true", help="파일 write skip + stdout preview"
    )
    parser.add_argument(
        "--require-sync",
        action="store_true",
        help=(
            "homepage 배포(push) 실패 시 non-zero exit (wrapper cascade 카톡 보류용). "
            "기본(미지정)은 graceful 0 return (backfill 호환)."
        ),
    )
    args = parser.parse_args()

    date_str = args.date or datetime.now(KST).strftime("%Y-%m-%d")
    log(f"START: date={date_str} dry_run={args.dry_run}")

    # 휴장일 / 주말 본일 catch (send_kakao_message.py L402~L413 verbatim 복제 패턴):
    # picks JSON 자체가 존재하면 휴장 cron 0건 trigger 정합 (수동 run 안전장치).
    market_closed = load_market_closed_set()
    try:
        wd = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    except ValueError:
        wd = -1
    if date_str in market_closed or wd >= 5:
        # picks 부재 시 graceful exit (cron 정합)
        if not (PICKS_DIR / f"{date_str}.json").exists():
            log(f"HOLIDAY: skip (date={date_str})")
            return 0

    history = build_history(date_str)
    if history is None:
        # FLR-20260612-TEC-001 ① — STEP2 거짓 PASS 봉쇄 (#29 완결 조건).
        # 평일 judge/picks 부재 = build 불가인데 무조건 return 0 → wrapper cascade가
        # 매일 deterministic PASS 오인 → 미완성 카드 카톡 발사. --require-sync
        # (wrapper STEP2)는 exit 2 → 카톡 보류 trigger. 미지정(backfill/수동)은
        # 기존 graceful 0 유지 (호환).
        if args.require_sync:
            log(
                "FAIL: build_history None — --require-sync 모드, exit 2 (카톡 보류 trigger)"
            )
            return 2
        log("EXIT: build_history returned None (graceful)")
        return 0

    if args.dry_run:
        print(json.dumps(history, ensure_ascii=False, indent=2))
        log("DONE: dry-run")
        return 0

    out_path = HISTORY_OUT_DIR / f"{date_str}.json"
    try:
        atomic_write_json(out_path, history)
    except Exception as exc:
        log(f"FAIL: write: {type(exc).__name__}")
        return 1
    log(f"DONE: written {out_path}")

    # --- homepage main repo sync hook (6/4 사고 ROOT 봉쇄) ---
    # 6/4 build_card_history fire (15:25) → 메인 적재 PASS, homepage main repo 동기화 책임 0건
    # 결과: 라이브 deploy 누락 → 본 lead 직접 cp+commit+push 수습 (17:13 KST a7e4ac5f3)
    # 6/5 (금) 부터 자동 cascade 보장 의무 — 메인 write 직후 homepage main repo로 sync
    sync_rc = sync_to_homepage_main(out_path, date_str)
    if sync_rc != 0:
        log(f"FAIL: homepage sync rc={sync_rc} (data write 자체는 PASS)")
        # --require-sync (wrapper cascade): 배포 실패 = non-zero exit
        #   → wrapper가 카톡 보류 (미완성/미배포 카드 푸시 금지, 대표 결정 2026-06-05).
        # 미지정 (backfill / 수동 run): data write 자체는 성공이므로 0 return (graceful).
        #   sync 실패는 alert만, 다음 fire 시 재시도 가능 (idempotent).
        if args.require_sync:
            log(
                "FAIL: --require-sync 모드 — 배포 미완료, exit 2 (wrapper 카톡 보류 trigger)"
            )
            return 2
        # graceful: 다음 fire 시 재시도 가능 (idempotent)

    try:
        summary = build_summary()
        summary_path = HISTORY_OUT_DIR / "summary.json"
        atomic_write_json(summary_path, summary)
        log(
            f"SUMMARY: picks={summary['total_picks']} settled={summary['settled']} "
            f"win={summary['take_profit']} loss={summary['expired_loss']} "
            f"rate={summary['win_rate']}%"
        )
        _assert_summary_not_stale(summary)
        sync_summary_rc = sync_to_homepage_main(summary_path, "summary")
        if sync_summary_rc != 0:
            log(f"WARN: summary sync rc={sync_summary_rc} (다음 fire 재시도, graceful)")
    except Exception as exc:
        log(f"WARN: summary build/sync 실패: {type(exc).__name__} (graceful)")

    return 0


def _assert_summary_not_stale(summary: dict[str, Any]) -> None:
    """회귀 게이트(RND-PM320-064 P1) — summary.last_settled_date 가 라이브 history 의
    실제 최신 청산 진입일보다 오래되면 알람.

    이번 사고 패턴(6/12 고영 taken_profit 인데 summary 는 6/11 고정)을 직접 탐지한다.
    backtest_ssot base + live tail merge 가 정상이면 둘이 일치 → 무알람. 거짓 경보 회피:
    running 픽·보류일은 settled 가 아니므로 분모에서 제외(실 청산 진입일만 비교).
    """
    summ_last = summary.get("last_settled_date")
    actual_last: str | None = None
    for fp in sorted(HISTORY_OUT_DIR.glob("*.json")):
        if fp.name == "summary.json":
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        date = d.get("date") or fp.stem
        for stock in d.get("stocks", []):
            pick = stock.get("pm320_pick") or {}
            if not pick.get("is_pick"):
                continue
            if pick.get("current_state") in (
                "taken_profit",
                "expired_gain",
                "expired_loss",
            ):
                if actual_last is None or date > actual_last:
                    actual_last = date
    if (
        isinstance(summ_last, str)
        and isinstance(actual_last, str)
        and actual_last > summ_last
    ):
        log(
            "🔴 STALE ALERT: summary.last_settled_date="
            f"{summ_last} < 라이브 실제 최신 청산 진입일={actual_last} "
            "— 정산 집계 누락(승률 카드 stale). build_summary live tail merge 점검 요망 "
            "(RND-PM320-064 회귀)"
        )


# 🔴 자기 산출물 경로 prefix — 이 prefix 충돌은 진짜 push 실패 (FLR-AGT-002: 회복 위장 금지).
#    그 외(타 cron 산출물 = nxt_roster / kiwoom json / us-indices 등) 충돌만 회복 대상.
_SELF_ARTIFACT_PREFIX = "data/pm320_history/"


def _classify_dirty(porcelain: str) -> tuple[list[str], list[str]]:
    """`git status --porcelain` 출력을 자기 산출물 / 타 cron 산출물로 분류.

    return: (self_paths, foreign_paths). 자기 산출물 = data/pm320_history/ 하위.
    rename(`R old -> new`)·copy 는 화살표 우측(목적지)을 경로로 본다.
    """
    self_paths: list[str] = []
    foreign_paths: list[str] = []
    for raw in porcelain.splitlines():
        if not raw.strip():
            continue
        # porcelain: XY<space>path  (rename 시 'old -> new')
        path_part = raw[3:] if len(raw) > 3 else raw
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        path = path_part.strip().strip('"')
        if path.startswith(_SELF_ARTIFACT_PREFIX):
            self_paths.append(path)
        else:
            foreign_paths.append(path)
    return self_paths, foreign_paths


def _unmerged_paths(porcelain: str) -> list[str]:
    """`git status --porcelain` 출력에서 unmerged(충돌) 경로만 추출.

    porcelain 충돌 코드: DD AU UD UA DU AA UU (양측 중 하나 이상이 U/혹은 AA/DD).
    """
    out: list[str] = []
    for raw in porcelain.splitlines():
        if len(raw) < 3:
            continue
        xy = raw[:2]
        if "U" in xy or xy in ("DD", "AA"):
            path_part = raw[3:]
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1]
            out.append(path_part.strip().strip('"'))
    return out


def _resolve_foreign_conflicts(
    homepage_main: Path, foreign_paths: list[str], _git, log
) -> bool:
    """타 cron 산출물 충돌 경로를 upstream(theirs) 으로 강제 해소.

    autostash pop 충돌은 working tree 에 마커만 남기고 stash 는 보존된다(git 2.50).
    해당 파일을 `checkout --theirs`(rebase 후 upstream 측) + `git add` 로 마킹 해소.
    그 후 호출측이 push 하면 우리 commit(자기 산출물)은 그대로 반영되고, 타 cron
    산출물은 upstream 상태를 따른다(해당 cron 이 다음 fire 에 자기 값으로 재생성).
    """
    try:
        for p in foreign_paths:
            # rebase/merge 충돌에서 --theirs = 적용 중이던 변경(여기선 autostash pop 의
            # 반대편 = upstream 트리). 실패해도 add 로 마킹 시도.
            _git(["checkout", "--theirs", "--", p], check=False)
            _git(["add", "--", p])
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"SYNC WARN (foreign conflict 해소 실패): {type(exc).__name__}")
        return False


def _drop_dangling_autostash(_git) -> None:
    """autostash pop 충돌 시 보존된 stash(맨 위 'autostash')를 폐기(best-effort).

    타 cron 산출물만 관여하므로 폐기 안전(해당 cron 이 다음 fire 에 재생성).
    """
    try:
        top = _git(["stash", "list"], check=False, timeout=30).stdout.splitlines()
        if top and "autostash" in top[0]:
            _git(["stash", "drop"], check=False)
    except Exception:  # noqa: BLE001
        pass  # best-effort


def _safe_rebase_push(homepage_main: Path, date_str: str, log) -> int:
    """`pull --rebase --autostash` + `push HEAD:main` 을 conflict 회복과 함께 수행.

    🔴 본 함수 신설 사유 (FLR-20260615-FLR-001 critical 4회차, lead-meta §11.32):
    15:20 cron 의 build_card_history 는 `M1S_HOMEPAGE`(=100m1s-homepage-cron) 공유
    작업트리에서 push 한다. 타 cron(nxt-roster / kiwoom-scraper 등)이 같은 트리에 남긴
    dirty tracked data (pm320/data/nxt_roster.json 등)와 `pull --rebase --autostash`
    의 autostash pop 이 충돌할 때:

      ⚠️ git 2.50 실측: autostash pop 충돌 시 `pull` 은 rc=0 ("Successfully rebased")
         으로 끝나되 working tree 에 충돌 마커(UU)를 남기고 stash 를 보존한다.
         → 호출측은 그 상태로 push(성공)하여 cron worktree 를 UU+dangling autostash 로
         오염시키고, **다음 fire** 의 `pull --rebase` 가 "unmerged files" rc≠0 →
         build_card_history exit 2 → PM320 픽 push 누락 + 카톡 보류 (본일 15:20 사고).

    따라서 회복은 rc 가 아니라 **working tree unmerged 여부**를 기준으로 한다:
      0) 사전: 트리가 이미 unmerged 상태(직전 fire 잔재)면 먼저 분류·해소(cascade 차단).
      1) pull --rebase --autostash 실행 (rc 무관).
      2) unmerged 경로 분류:
         - 자기 산출물(data/pm320_history/**) 포함 → **회복 위장 금지**, return 3
           (FLR-AGT-002: 진짜 push 실패를 회복으로 가장 안 함).
         - 전부 타 cron 산출물 → upstream(theirs) 으로 해소 + dangling autostash 폐기.
      3) push HEAD:main. 성공 0, 실패 정직 return 3.

    return: 0=PASS, 3=push FAIL(정직).
    """
    import subprocess

    def _git(args: list[str], check: bool = True, timeout: int = 120):
        return subprocess.run(
            ["git", *args],
            cwd=str(homepage_main),
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _recover_or_fail(stage_label: str) -> int | None:
        """현재 working tree 의 unmerged 를 분류·해소. self 충돌이면 3, 회복 가능하면
        None(계속 진행), 조회 실패면 3. foreign 만이면 해소 후 None."""
        try:
            porcelain = _git(["status", "--porcelain"], timeout=30).stdout
        except Exception as exc:  # noqa: BLE001
            log(
                f"SYNC FAIL (status 조회 {stage_label}): {type(exc).__name__} → 정직 exit"
            )
            _notify_sync_fail(date_str, "push")
            return 3
        unmerged = _unmerged_paths(porcelain)
        if not unmerged:
            return None  # clean — 진행
        self_paths, foreign_paths = _classify_dirty(
            "\n".join(f"UU {p}" for p in unmerged)
        )
        if self_paths:
            # 🔴 자기 산출물 충돌 = 진짜 push 실패. 회복 위장 금지 (FLR-AGT-002).
            log(
                f"SYNC FAIL (push, {stage_label}): 자기 산출물 충돌 {self_paths} — "
                f"정직 exit (FLR-AGT-002 회복 위장 금지)"
            )
            _notify_sync_fail(date_str, "push")
            return 3
        # 전부 타 cron 산출물 → upstream 해소 + autostash 폐기.
        log(
            f"SYNC: 타 cron 산출물 충돌 {foreign_paths} upstream 해소({stage_label}) "
            f"— 자기 산출물 무관, 해당 cron 다음 fire 재생성"
        )
        _resolve_foreign_conflicts(homepage_main, foreign_paths, _git, log)
        _drop_dangling_autostash(_git)
        return None

    # 0) 사전: 직전 fire 잔재(UU + dangling autostash)면 먼저 해소 (cascade 차단).
    pre = _recover_or_fail("pre")
    if pre is not None:
        return pre

    # 1) fetch + rebase 2단계 분리: cron WT upstream(cron-isolation)과 origin/main 모호성 제거
    # (pull --rebase origin main 는 upstream 추적 브랜치가 별도 설정된 경우
    #  "Cannot rebase onto multiple branches" fatal 발생 — 2026-06-29 근본 수정)
    # autostash pop 충돌은 rc=0 으로 끝날 수 있으므로 rc 무관으로 진행.
    try:
        _git(["fetch", "origin", "main"], timeout=30)
    except subprocess.CalledProcessError as exc:
        log(
            f"SYNC WARN (fetch rc={exc.returncode}) — abort 후 회복 평가 "
            f"(stderr: {(exc.stderr or '').strip()[:160]})"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"SYNC WARN (fetch 예외: {type(exc).__name__}) — 회복 평가로 진행")
    try:
        _git(["rebase", "--autostash", "origin/main"])
    except subprocess.CalledProcessError as exc:
        # rc≠0 (예: rebase 중단·non-ff) → abort 후 회복 평가로 진행.
        log(
            f"SYNC WARN (rebase rc={exc.returncode}) — abort 후 회복 평가 "
            f"(stderr: {(exc.stderr or '').strip()[:160]})"
        )
        _git(["rebase", "--abort"], check=False)
    except Exception as exc:  # noqa: BLE001
        log(f"SYNC WARN (rebase 예외: {type(exc).__name__}) — abort 후 회복 평가")
        _git(["rebase", "--abort"], check=False)

    # 2) pull 후 unmerged 분류·해소 (rc=0+UU = 본일 사고 경로).
    post = _recover_or_fail("post-pull")
    if post is not None:
        return post

    # 3) push.
    try:
        _git(["push", "origin", "HEAD:main"])
        log(f"SYNC: push done → origin main (date={date_str})")
        return 0
    except subprocess.CalledProcessError as exc:
        log(f"SYNC FAIL (push): rc={exc.returncode} → 정직 exit")
        _notify_sync_fail(date_str, "push")
        return 3
    except Exception as exc:  # noqa: BLE001
        log(f"SYNC FAIL (push 예외): {type(exc).__name__} → 정직 exit")
        _notify_sync_fail(date_str, "push")
        return 3


def sync_to_homepage_main(out_path: Path, date_str: str) -> int:
    """메인 write 산출물 → 서빙 homepage 레포(cron worktree) 동기화.

    🔴 6/8 사고 fix (FLR-20260608-TEC-001): 기존 코드는 ~/company/100m1s-homepage
    (divergent 메인 레포, origin/main 보다 223 커밋 ahead, GitHub Pages 서빙 안 됨)로
    cp+commit+push 했으나 라이브에 안 닿아 6/8 history 404 → 보류 안내·매매버튼 미표시.
    실제 서빙 레포 = HOMEPAGE_DIR (M1S_HOMEPAGE, 기본 100m1s-homepage-cron, = origin/main).
    cron worktree 는 cron-isolation 브랜치 위에 있으므로 로컬 `main` 이 없다. push 는
    force 제거 교정(§6, 2026-06-08): `git pull --rebase --autostash origin main` 으로 로컬 HEAD 를
    origin/main 위로 올린 뒤 `git push origin HEAD:main` (plain, force 없음). non-ff /
    rebase 실패 시 다음 fire 재시도 (idempotent, race 안전). dirty tracked data는
    autostash 로 보존한다.

    flow:
      1. private history → deploy_history 공개판 sanitize → {HOMEPAGE_DIR}/data/pm320_history/{date}.json
      2. cd HOMEPAGE_DIR; git add data/pm320_history/{date}.json
      3. status 0건이면 skip (untracked 최초 파일 포함, idempotent)
      4. commit + pull --rebase --autostash origin main + push HEAD:main (plain, force 없음)
      5. 실패 catch → osascript display notification (pipeline.sh L88 verbatim 패턴)

    return: 0=PASS, 1=cp FAIL, 2=git FAIL, 3=push FAIL
    """
    import shutil
    import subprocess

    # 🔴 per-day push 억제 가드 (40일 backfill 일괄 배포용, 2026-06-08 overlay).
    # M1S_HISTORY_NO_SYNC=1 지정 시 cp+commit+push 전체 skip → 메인 worktree write 만 수행.
    # 40일 재산출 루프 동안 매 일자 push (40회) 폭발 방지. 루프 종료 후 cron worktree 에서
    # data/pm320_history/*.json 변경분만 명시 add → 1 commit → safe rebase push 로 일괄 배포.
    if os.environ.get("M1S_HISTORY_NO_SYNC") == "1":
        log(f"SYNC SKIP: M1S_HISTORY_NO_SYNC=1 (per-day push 억제, date={date_str})")
        return 0

    # 서빙 레포 = HOMEPAGE_DIR (M1S_HOMEPAGE env, 기본 cron worktree = origin/main).
    homepage_main = HOMEPAGE_DIR
    target_dir = homepage_main / "data" / "pm320_history"
    target_path = target_dir / f"{date_str}.json"
    source_path = out_path

    if not homepage_main.exists() or not (homepage_main / ".git").exists():
        log(f"SYNC SKIP: homepage main repo absent ({homepage_main})")
        return 0  # graceful — 환경 미구성 시 skip

    try:
        if date_str != "summary":
            from export_public_history import export_public

            export_public(dates=[date_str])
            public_path = PUBLIC_HISTORY_OUT_DIR / f"{date_str}.json"
            if not public_path.exists():
                log(f"SYNC FAIL (public): missing deploy_history/{date_str}.json")
                _notify_sync_fail(date_str, "public")
                return 1
            source_path = public_path
        elif date_str == "summary":
            from export_public_history import export_public

            export_public()
            public_summary = PUBLIC_HISTORY_OUT_DIR / "summary.json"
            if not public_summary.exists():
                log("SYNC FAIL (public): missing deploy_history/summary.json")
                _notify_sync_fail(date_str, "public")
                return 1
            source_path = public_summary
        if source_path.stat().st_size == 0:
            log(f"SYNC FAIL (public): empty deploy file ({source_path})")
            _notify_sync_fail(date_str, "public")
            return 1

        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source_path), str(target_path))
        log(f"SYNC: cp {source_path.name} → {target_path}")
    except Exception as exc:
        log(f"SYNC FAIL (cp): {type(exc).__name__}: {exc}")
        _notify_sync_fail(date_str, "cp")
        return 1

    try:
        rel = str(target_path.relative_to(homepage_main))
        pre_staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(homepage_main),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
        if pre_staged:
            log(f"SYNC SKIP: pre-existing staged changes ({len(pre_staged)} files)")
            _notify_sync_fail(date_str, "staged")
            return 2

        # git diff HEAD 는 untracked 신규 파일을 놓치므로 status 를 기준으로 본다.
        status_out = subprocess.run(
            ["git", "status", "--porcelain", "--", rel],
            cwd=str(homepage_main),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if not status_out:
            log(f"SYNC SKIP: no diff (idempotent, {rel})")
            return 0

        subprocess.run(
            ["git", "add", rel],
            cwd=str(homepage_main),
            check=True,
            timeout=30,
        )
        commit_msg = (
            f"data(pm320,P0,history,{date_str},DSN-001): "
            f"pm320_history 자동 sync (build_card_history.py hook)"
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(homepage_main),
            check=True,
            timeout=60,
        )
        log(f"SYNC: commit done ({rel})")
        post_staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(homepage_main),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
        if post_staged:
            log(
                f"SYNC FAIL (staged): commit left staged changes ({len(post_staged)} files)"
            )
            _notify_sync_fail(date_str, "staged")
            return 2
    except subprocess.CalledProcessError as exc:
        log(f"SYNC FAIL (git): rc={exc.returncode}")
        _notify_sync_fail(date_str, "git")
        return 2
    except Exception as exc:
        log(f"SYNC FAIL (git unexpected): {type(exc).__name__}: {exc}")
        _notify_sync_fail(date_str, "git")
        return 2

    # cron worktree 는 cron-isolation 브랜치 위에 있어 로컬 `main` 이 없다.
    # 🔴 force 제거 (§6 후속 교정 2026-06-08): 기존 --force-with-lease=main 은
    # 다른 actor 변경을 (lease 통과 시) 덮어쓸 위험이 있어, 안전한 rebase 기반으로 교정.
    # 🔴 conflict 회복 보강 (FLR-20260615-FLR-001 critical 4회차, 2026-06-15): autostash pop
    # 이 타 cron(nxt-roster 등) 산출물과 충돌해 명령 전체가 rc≠0 → PM320 픽 push 누락하던
    # 본일 15:20 ROOT 를 봉쇄. _safe_rebase_push 가 (a) 자기 산출물 충돌 = 정직 exit(return 3,
    # FLR-AGT-002 회복 위장 금지), (b) 타 cron 산출물 충돌 = 폐기 후 push 재시도로 분기.
    push_rc = _safe_rebase_push(homepage_main, date_str, log)
    if push_rc != 0:
        return push_rc

    # S2 dual-write (DOC-20260614-DSN-002): 홈 push 성공 후 독립 repo 로도 동기화.
    # 🔴 source_path = sanitize 된 공개판 (홈 push 와 동일 파일). failure isolated —
    #    독립 repo 실패해도 return 0 불변 (카드·카톡 cascade 무중단 최우선).
    #    M1S_PM320_INDEPENDENT 미설정 시 무동작 (S2 비활성, 현행 0 변화).
    #    함수-로컬 import (export_public_history 동형, autoflake 모듈-레벨 오제거 회피).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dual_sync import dual_write

    rel_name = "summary.json" if date_str == "summary" else f"{date_str}.json"
    dual_write(
        source_path=source_path,
        rel_data_path=f"pm320_history/{rel_name}",
        date_str=date_str,
        log=log,
        commit_label="history",
    )

    return 0


def _notify_sync_fail(date_str: str, stage: str) -> None:
    """sync 실패 macOS notification (best-effort, pipeline.sh L88 verbatim 패턴)."""
    import subprocess

    try:
        msg = (
            f"pm320 history sync FAIL (stage={stage}, date={date_str}) "
            f"— build_card_history.py hook 점검 필요"
        )
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{msg}" with title "100m1s PM320 SYNC FAIL"',
            ],
            timeout=10,
        )
    except Exception:
        pass  # notification 자체 실패는 silent


if __name__ == "__main__":
    sys.exit(main())
