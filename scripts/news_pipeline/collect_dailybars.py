"""키움 ka10081 일봉 차트 수집 — 240영업일 min/max 통계 산출.

REQ-20260420-REQ-001 Phase 2.
ka10081 통째 호출 + UPSERT (정정 정책: 매직 넘버 없음, cron 내 중복 캐시만 방지).

대상: 오늘 daily_picks 종목.
저장: stocks 테이블 6컬럼 UPDATE
  - price_high_240d / _date / pct_from_high_240d
  - price_low_240d  / _date / pct_from_low_240d
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
# kiwoom_cron.sh ENV_FILE = MAIN_ENV 정합. POC_ENV 는 직접 호출 (cron 외) 시 fallback only.
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

from .collect_limit_up_universe import fetch_limit_up_codes  # noqa: E402
from .db import connect
from .ka10081_helper import DailyBar, parse_int_field, parse_ka10081_row
from .kiwoom_client import get_token  # noqa: E402,F401

# 실전 도메인 우선 (실전 키 부재 시 모의 fallback).
# 주의: ka10081의 upd_stkpc_tp(수정주가) 옵션은 모의/실전 모두 동일 raw 응답.
# 액면분할 자동 보정은 키움 API에서 보장되지 않음 — 별도 분할 이력 데이터 필요 시 추가 처리.
KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL") or os.environ.get(
    "KIWOOM_BASE_URL", "https://api.kiwoom.com"
)
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)

# ============================================================================
# Fix-4 (2026-05-22, DOC-20260522-FLR critical recurring protection):
# 3-layer 봉쇄 (5/22 09hr timeout cascade root cause fix)
#
# Layer A 봉쇄: codes 우선순위 정렬 — daily_picks(today) 본질 먼저, 그 뒤 stocks 나머지.
#   사용자 가시 카드는 daily_picks 본문 → 첫 fire가 timeout으로 중단되어도 가시 카드 정상 보장.
#
# Layer B 봉쇄: per-fire batch limit time + batch size 분할.
#   본 fire에서 BATCH_LIMIT_SEC(400s) 초과 시 cache flush + 즉시 종료.
#   다음 fire(10분 후 launchd StartInterval=600s)에 resume cache 본질 정합.
#
# Layer C 봉쇄: _RUN_CACHE 영속화 — /tmp/dailybars-resume-{date}.json.
#   module-level set은 매 process마다 fresh → 동일 종목 cascade.
#   file 영속화 + date suffix → 다음 영업일 자동 무시 (별도 cleanup trigger 불요).
#
# 본 fix 본질 commit 후 push 보류 (lead 결정 대기, lead-meta §11.31).
# ============================================================================

# Layer C: _RUN_CACHE 영속화 본문.
# /tmp/dailybars-resume-{today}.json — set 본문 list serialize.
# date suffix 본문: 다음 영업일 자동 무시 (cache miss → 빈 set), 옛 file 자연 cleanup.
_RUN_CACHE: set[str] = set()
_CACHE_LOADED_FOR: str | None = None  # cache load 본 date (중복 load 회피)

# Fix-6 (2026-05-24): timeout cascade 신호 module-level flag.
# collect() 내부 timeout_break 결과를 __main__ 진입점에서 읽어 exit code 2 cascade.
# 직접 import (테스트/수동) 시 reset 필요 시 호출자가 수동 reset (collect 진입 시 False 초기화).
_LAST_TIMEOUT_BREAK: bool = False


def _cache_path(date_str: str) -> Path:
    """resume cache 파일 path — date suffix 본문."""
    return Path(f"/tmp/dailybars-resume-{date_str}.json")


def _load_run_cache(date_str: str) -> None:
    """resume cache 본질 load (process 시작 시 1회).

    date suffix 본문 → 다음 영업일 자동 빈 set (file mismatch).
    JSON parse 실패 시 빈 set fallback (안전 본질).
    """
    global _RUN_CACHE, _CACHE_LOADED_FOR
    if _CACHE_LOADED_FOR == date_str:
        return  # 이미 load 본질
    p = _cache_path(date_str)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list):
                _RUN_CACHE = set(data)
                print(
                    f"[collect_dailybars] resume cache loaded: "
                    f"{len(_RUN_CACHE)} codes from {p}"
                )
            else:
                _RUN_CACHE = set()
        except Exception as e:
            print(f"[collect_dailybars] resume cache parse fail ({e}) → empty set")
            _RUN_CACHE = set()
    else:
        _RUN_CACHE = set()
    _CACHE_LOADED_FOR = date_str


def _save_run_cache(date_str: str) -> None:
    """resume cache 영속화 (매 N건 + timeout 직전 flush 본질).

    JSON serialize (list 본문). 쓰기 실패 시 silent skip (cache는 best-effort).
    """
    p = _cache_path(date_str)
    try:
        p.write_text(json.dumps(sorted(_RUN_CACHE)))
    except Exception as e:
        print(f"[collect_dailybars] resume cache save fail: {e}")


def _clear_run_cache(date_str: str) -> None:
    """force-recollect 시 resume cache 무효화 (in-memory + persisted file).

    FLR-20260526-TEC-001 (force-recollect no-op cascade, FLR-20260524-TEC-001 변종):
      M1S_FORCE_DAILYBARS_RECOLLECT=1 은 already_today 만 비활성화하고 _RUN_CACHE 는
      그대로 두어 line 460 `if code in _RUN_CACHE: continue` 에서 강제 재호출이 차단됨.
      → 장중(now<16:00) 첫 cron 이 5/26 종목을 UPSERT 성공(cache.add) 했으나 ka10081
      intraday 응답이 5/26 봉을 o==h==lo==c2&vol==0 으로 반환 → intraday-skip 으로 당일
      봉 누락 → 16:00 이후 강제 재호출이 _RUN_CACHE stuck 으로 무력화 → dailybars 당일
      행 영구 누락 (5/26 limit-up 18종목 trend 미반영 사고).
    Fix: force 시 cache 를 명시적으로 비워 모든 종목이 line 460 통과 → 정식 재호출.
    """
    global _RUN_CACHE
    _RUN_CACHE = set()
    p = _cache_path(date_str)
    try:
        if p.exists():
            p.unlink()
    except Exception as e:
        print(f"[collect_dailybars] resume cache clear fail: {e}")


# Layer B 본문 상수
BATCH_FLUSH_INTERVAL = 50  # 매 50건마다 cache flush
BATCH_LIMIT_SEC = 400  # per-fire batch time limit (launchd StartInterval=600s 의 67%)


def _load_search_pass_universe(date_str: str) -> set[str]:
    """검색식 통과 universe 종목코드 set — kiwoom/{date}.json.

    Fix-10 (2026-06-05, 대표 결정 union 확장):
      통합 select_daily_pick 의 PICK 후보 SoT = latest_stocks (trade_amount desc top-5).
      그 종목(삼성전자우 005935·대형주 등)의 dailybars 가 강세/양봉 판정 입력이므로
      carded universe 와 별개로 사전 수집 필요. carded-only 면 판정 입력 결손 → 가짜 PICK.

    source = config.HOMEPAGE/data/kiwoom/{date}.json (build_daily.load_kiwoom_volume_list
    과 동일 source). 세 key union (누락 0):
      - latest_stocks      : 마지막 snapshot (list[dict], select_daily_pick top-5 SoT)
      - accumulated_stocks : 당일 검색식 통과 누적 전 종목 (dict[code] -> item, 예 005935)
      - daily_top          : 일일 누적 거래대금 (list[dict], fallback 호환)

    안전 (FLR-AGT-002 거짓 충실성 회피):
      - 파일 부재 / JSON parse 실패 / key 누락 → 빈 set 반환 (carded-only 로 graceful
        degrade, 단 latest_stocks 가 정상이면 누락 금지). 예외 silent 흡수 안 함 — print.
      - 6자리 숫자 코드만 채택 (A 접두 제거 등 정규화).
    """
    from .config import HOMEPAGE

    path = HOMEPAGE / "data" / "kiwoom" / f"{date_str}.json"
    if not path.exists():
        print(
            f"[collect_dailybars] 검색식 통과 universe 파일 부재 ({path}) "
            f"→ carded-only fallback"
        )
        return set()
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        print(
            f"[collect_dailybars] 검색식 통과 universe parse 실패 ({e}) "
            f"→ carded-only fallback"
        )
        return set()

    out: set[str] = set()

    def _add(code: object) -> None:
        if isinstance(code, str) and len(code) == 6 and code.isdigit():
            out.add(code)

    # latest_stocks / daily_top: list[dict] — ticker 또는 code 필드.
    for key in ("latest_stocks", "daily_top"):
        for it in data.get(key) or []:
            if isinstance(it, dict):
                _add(it.get("ticker") or it.get("code"))
    # accumulated_stocks: dict[code] -> item (당일 검색식 통과 누적 전 종목).
    acc = data.get("accumulated_stocks") or {}
    if isinstance(acc, dict):
        for code, it in acc.items():
            _add(code)
            if isinstance(it, dict):
                _add(it.get("ticker") or it.get("code"))
    return out


# Fix-12 (2026-06-22, 보유 포지션 forward 일봉 누락 근본책 — 대표 "파이프라인 점검 + 백필"):
#   미정산(보유중) 픽의 pick_date 이후 forward 일봉이 수집되지 않는 갭을 봉쇄.
#
# ROOT (DSN-001 §X.213 "근본은 cron universe 정책 별도"의 그 근본책):
#   종전 union(search_pass ∪ carded ∪ 당일상한가)은 전부 **TODAY 기준** universe 였다.
#   carded 의 daily_picks 절도 `WHERE date=today` 라 "오늘 새로 카드에 뜬 종목"만 잡고,
#   과거에 PICK 되어 아직 보유중(만기 미도래)인 픽은 오늘 universe 에 없으면 ka10081 을
#   호출하지 않는다 → 그 종목 dailybars 가 pick_date 에서 freeze (forward 봉 미적재).
#   영향 2종 동일 뿌리: (1) 카드 일봉캔들 차트 stale (emit_dailybars_per_stock 는 DB 를
#   읽으므로 DB 가 안 차면 JSON 도 stale) (2) 백테스트 미정산 (build_card_history
#   compute_result 가 forward 봉 부재 시 None → state running 잔존, D+N 만기 청산 불가).
#   실측 2026-06-22 (메인 DB): 고영 098460 pick 6/12 → dailybars 5/27 (갭 16거래일),
#   디앤디 347850 pick 6/17 → dailybars 6/17 일치(running, history 도 잡음).
#   cf. backfill_dailybars_targeted.py docstring "D+3 만기 dailybars 부재 →
#   compute_result None → state running 잔존" = 동일 사고의 1회성 수동 backfill.
#
# 수집 대상 정의 (2-source union, 미정산 픽 = 만기 미도래):
#   (A) running_codes (scripts.pm320.running_picks) — history JSON 산출물 파생 SSOT.
#       current_state=='running' AND expiry_date >= today 인 픽의 distinct 종목코드.
#       추정·fallback 0 (FLR-AGT-002 — 파일에 running 으로 적힌 것만). best-effort
#       import — history 디렉토리 부재/모듈 부재 시 빈 set (graceful).
#   (B) daily_picks 최근 _OPEN_POSITION_WINDOW 거래일 윈도우 — history 가 끊긴 구간
#       (예 6/18~ 미생성)의 보유중 픽 보완. 윈도우 = D+6(물타기 만기 최대, build_card_
#       history.py:1007) + 버퍼 3거래일 = 9거래일. 픽일이 윈도우 밖이면 이미 만기·정산된
#       과거 픽 → 자연 제외(over-collection 회피, 예 삼화 001820 픽 5/29 는 제외 정상).
#
# 비용: (A) 보유중 통상 ~20종 + (B) 9거래일 윈도우 통상 ~60종, 중복·당일 union 제거 후
#   순증 보유픽 수~십수 종. 전종목(~4288) over-collection 아님 (FLR-20260527-TEC-001
#   cold-start stale 회피). 휴장일은 양쪽 자연 0건.
_OPEN_POSITION_WINDOW = 9  # daily_picks 보유중 픽 윈도우 (D+6 만기 + 버퍼 3거래일)


def _load_open_position_codes(conn, today: str) -> set[str]:
    """미정산(보유중) 픽 종목코드 set — running SSOT ∪ daily_picks 최근 윈도우.

    Args:
        conn: 이미 열린 DB connection (carded 쿼리와 동일 트랜잭션 재사용).
        today: 기준 일자 YYYY-MM-DD.

    Returns:
        6자리 숫자 코드 set. 두 source 모두 graceful (빈 source → 빈 기여).
    """
    out: set[str] = set()

    # (A) running_codes — history JSON 파생 SSOT (best-effort import, 추정 0).
    try:
        from scripts.pm320.running_picks import running_codes

        for code in running_codes(today):
            if isinstance(code, str) and len(code) == 6 and code.isdigit():
                out.add(code)
    except Exception as e:
        # history 디렉토리 부재 / 모듈 경로 차이 → daily_picks 윈도우 단독으로 graceful.
        print(
            f"[collect_dailybars] running_codes 미수집 ({e}) → daily_picks 윈도우 단독"
        )

    # (B) daily_picks 최근 _OPEN_POSITION_WINDOW 거래일 윈도우 — history 끊긴 구간 보완.
    #   윈도우 = daily_picks 에 실재하는 distinct date 의 최근 N개 (휴장·결측 자동 보정).
    rows = conn.execute(
        """SELECT DISTINCT stock_code AS code FROM daily_picks
           WHERE date IN (
             SELECT date FROM (SELECT DISTINCT date FROM daily_picks
                               ORDER BY date DESC LIMIT ?)
           )
           AND stock_code IS NOT NULL AND length(stock_code)=6""",
        (_OPEN_POSITION_WINDOW,),
    ).fetchall()
    for r in rows:
        if r[0] and len(r[0]) == 6 and r[0].isdigit():
            out.add(r[0])

    return out


def _get_token() -> str:
    # SSOT: kiwoom_client.get_token 위임 (본문 byte-identical, 기존 문구 보존).
    return get_token(KIWOOM_BASE, KIWOOM_APPKEY, KIWOOM_SECRETKEY)


# _pint → ka10081_helper.parse_int_field 위임 (Q-20260519-CYCLE19-001 SSOT 통합).
# verbatim 복제 봉쇄. 하위 호환 alias (외부 직접 호출 잔존 시 안전).
_pint = parse_int_field


def fetch_daily_chart(code: str, token: str) -> list[dict] | None:
    """ka10081 일봉차트. 키움 API 최대치 반환 (추정 240+ 영업일).

    헤더: api-id=ka10081
    body: stk_cd=A{code}, base_dt=오늘(YYYYMMDD), upd_stkpc_tp=1
    응답: stk_dt_pole_chart_qry: [{dt, open_pric, high_pric, low_pric, cur_prc, ...}, ...]
    cont-yn 응답 시 next-key로 연속조회 (최대 5페이지 = 약 600+ 영업일)
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10081",
    }
    base_dt = datetime.now().strftime("%Y%m%d")
    body = {"stk_cd": f"A{code}", "base_dt": base_dt, "upd_stkpc_tp": "1"}

    all_rows: list[dict] = []
    cont = False
    next_key = ""
    # 1페이지로 충분 (실측: 1페이지 = 약 600+ 행, 240영업일 윈도우 충족)
    # 추가 페이지는 rate limit만 유발하고 사용 안 됨
    for _page in range(1):
        if cont:
            headers["cont-yn"] = "Y"
            headers["next-key"] = next_key
        for attempt in range(4):
            try:
                r = requests.post(
                    f"{KIWOOM_BASE}/api/dostk/chart",
                    json=body,
                    headers=headers,
                    timeout=20,
                )
            except Exception as e:
                print(f"[collect_dailybars] {code} exception: {e}")
                return all_rows or None
            if r.status_code == 429:
                back = 2 ** (attempt + 1)
                print(f"[collect_dailybars] {code} 429, backoff {back}s")
                time.sleep(back)
                continue
            if r.status_code != 200:
                print(
                    f"[collect_dailybars] {code} http {r.status_code}: {r.text[:200]}"
                )
                return all_rows or None
            try:
                data = r.json()
            except Exception:
                return all_rows or None
            if data.get("return_code") == 5:
                back = 2 ** (attempt + 1)
                print(f"[collect_dailybars] {code} rc=5, backoff {back}s")
                time.sleep(back)
                continue
            if data.get("return_code") != 0:
                return all_rows or None
            rows = (
                data.get("stk_dt_pole_chart_qry")
                or data.get("output")
                or data.get("data")
                or []
            )
            if not isinstance(rows, list):
                return all_rows or None
            all_rows.extend(rows)
            cont_yn = (r.headers.get("cont-yn") or "").upper() == "Y" or (
                str(data.get("cont_yn") or "").upper() == "Y"
            )
            nk = r.headers.get("next-key") or data.get("next_key") or ""
            if cont_yn and nk:
                cont = True
                next_key = nk
                break  # next page
            else:
                return all_rows or None
        else:
            return all_rows or None
    return all_rows or None


def _normalize_row(row: dict) -> DailyBar | None:
    """ka10081 row → DailyBar (NamedTuple, tuple unpack 호환).

    Q-20260519-CYCLE19-001 (2026-05-19 후행 SSOT 통합):
      cycle12 Fix-3 (commit 83e967d) → backfill_dailybars_bulk verbatim 복제 봉쇄 위해
      ka10081_helper.parse_ka10081_row 단일 출처로 위임. NamedTuple 반환이지만 7-tuple
      unpack (d, o, h, lo, c2, vol, ta = n) 및 인덱스 접근 (n[4]) 호환 — 회귀 0.
    """
    return parse_ka10081_row(row)


WINDOW_DAYS = 240  # 240영업일 윈도우 (REQ-001 컬럼명 일치)


def _calc_240d_stats(rows: list[dict]) -> dict | None:
    """ka10081 응답 → {high, high_date, low, low_date, current, pct_from_high, pct_from_low}.

    최신 240영업일만 사용 (가용 일수가 240 미만이면 가용 일수 기준 — REQ-001 각주).
    high/low는 일봉 high/low 기준 (장중 최고/최저).
    실측: ka10081은 5페이지 호출 시 약 3000행(12년+) 반환 → 240으로 클리핑 필수.
    """
    parsed: list[tuple[str, int, int, int, int, int, int]] = []
    for r in rows:
        n = _normalize_row(r)
        if n:
            parsed.append(n)
    if not parsed:
        return None
    # 최신→과거 정렬 + 240영업일로 윈도우 클리핑
    parsed.sort(key=lambda x: x[0], reverse=True)
    parsed = parsed[:WINDOW_DAYS]
    current_close = parsed[0][4]
    if current_close <= 0:
        return None

    high_val = -1
    high_date = ""
    low_val = 10**12
    low_date = ""
    for date_str, _o, hi, lo, _c, _vol, _ta in parsed:
        if hi > 0 and hi > high_val:
            high_val = hi
            high_date = date_str
        if lo > 0 and lo < low_val:
            low_val = lo
            low_date = date_str

    if high_val <= 0 or low_val >= 10**12:
        return None

    pct_from_high = round((current_close - high_val) / high_val * 100, 2)
    pct_from_low = round((current_close - low_val) / low_val * 100, 2)
    return {
        "high": high_val,
        "high_date": high_date,
        "low": low_val,
        "low_date": low_date,
        "current": current_close,
        "pct_from_high": pct_from_high,
        "pct_from_low": pct_from_low,
        "available_days": len(parsed),
    }


def collect(date_str: str | None = None) -> int:
    """전체 stocks 종목 순회 + ka10081 통째 호출 + stocks UPSERT.

    REQ-001 S1 today-skip 정책 (2026-04-22):
      stocks.last_updated 날짜가 오늘이면 ka10081 호출 skip.
      과거 240영업일 일봉은 장중 재조회해도 동일하므로 비용 낭비.
      당일 신규 편입 종목(last_updated IS NULL or != today)만 호출.
      첫 장중 cron은 대부분 호출, 이후 cron은 대부분 skip → dur 22s → ~1s.

    Fix-1 (2026-05-13, DOC-20260512-FLR-001 정합):
      대상 종목을 daily_picks(today)에서 stocks 전체로 확장.
      기존: daily_picks(today)만 → 75/2797 = 2.7%만 갱신, 96%+ 누락 누적.
      신규: stocks 전체 → today-skip 로직(dailybars 존재 검사)이 자동 필터링.
      첫 cron만 부담, 이후 cron은 skip → 회귀 방지.

    Fix-4 (2026-05-22, 5/22 09hr timeout cascade root cause 봉쇄):
      Layer A: codes 우선순위 정렬 — daily_picks(today) 본질 먼저 → 사용자 가시 카드 우선 갱신.
      Layer B: per-fire BATCH_LIMIT_SEC(400s) 초과 시 cache flush + 즉시 종료 → 다음 fire resume.
      Layer C: _RUN_CACHE 영속화 → process 재시작 후에도 진행 상태 유지 (cascade 봉쇄).
    """
    from .config import is_market_holiday, pipeline_date

    today = date_str or pipeline_date()

    # Fix-8 (2026-05-26, FLR-20260526-TEC-001 근본 fix): 거래일 판정.
    # 경계 = "오늘 vs 과거" 의 핵심 분기. today 가 거래일이면 당일 종가 봉 적재 여부로
    # cache lock 을 게이트 (당일 데이터 매 cycle 재수집 보장). 휴장일(주말·공휴일)이면
    # 당일 봉 자체가 존재하지 않으므로 종전대로 cache lock 허용 (키움 호출 폭증 회피).
    today_is_trading_day = not is_market_holiday(today)

    # Layer C: resume cache 본질 load (process 시작 시 1회, date suffix 본문).
    _load_run_cache(today)

    # Fix-10 (2026-06-05, 대표 결정): 수집 대상 = **latest_stocks(검색식 통과 전 종목)
    #   ∪ carded universe**. 5/27 carded-universe 한정(Fix-9, FLR-20260527-TEC-001)을
    #   통합 모델에 맞게 확장. 전종목(2585) 복원 아님.
    #
    # WHY (확장 사유 — 통합 모델 top-5 대형주 데이터 필수):
    #   통합 select_daily_pick(scripts/pm320/select_daily_pick.py)은 latest_stocks 를
    #   거래대금(trade_amount) desc 정렬한 top-5 를 PICK 후보로 삼고, 그 위에서 강세·양봉·
    #   우선주 판정을 한다. 이 판정은 dailybars 의 OHLC/trade_amount 를 입력으로 쓴다.
    #   그러나 latest_stocks top-5 후보(삼성전자우 005935·주성엔지니어링 등 대형/중형주)는
    #   carded universe(daily_picks ∪ intraday_snapshot ∪ 상한가 = 소형주 위주)에 대부분
    #   부재 → 그 종목의 dailybars 가 수집되지 않음 → 강세/양봉 판정이 빈 데이터로 무력화
    #   → 가짜 PICK (FLR-AGT-002 거짓 충실성 동형, 표면 PICK PASS vs 실 판정 입력 결손).
    #   따라서 PICK 후보가 될 수 있는 검색식 통과 종목 전체의 dailybars 를 사전 수집해야
    #   판정이 정상 동작한다.
    #
    # ROOT cross-ref (FLR-20260527-TEC-001): 5/27 carded-universe 한정은 "rest 비-carded
    #   dailybars 소비처 0" 라는 당시 진단에 근거했으나, 통합 select 모델 도입으로
    #   latest_stocks 가 새로운 소비처가 됨 → "수집 범위는 소비처(SSOT)에 묶인다"는 동일
    #   원칙이 이제 union 확장을 요구. carded-only 는 소비처 누락(under-collection)이 되어
    #   가짜 PICK 위험. 5/27 over-collection(전종목 2585)도, 6/5 under-collection(carded-only)도
    #   둘 다 소비처-수집범위 불일치. 정답 = 소비처 정확히 = latest_stocks ∪ carded.
    #
    # 수집 대상 정의 (union, 누락 0):
    #   (A) 검색식 통과 universe — kiwoom/{today}.json:
    #         latest_stocks (마지막 snapshot, select_daily_pick top-5 SoT) ∪
    #         accumulated_stocks (당일 검색식 통과 누적 전 종목, 예 삼성전자우 005935) ∪
    #         daily_top (일일 누적 거래대금 fallback). build_daily.load_kiwoom_volume_list
    #         과 동일 source(config.HOMEPAGE/data/kiwoom). latest_stocks 누락 금지.
    #   (B) carded universe (DB) — 기존 Fix-9 유지:
    #         daily_picks(today) ∪ intraday_snapshot(today) ∪ stock_status_badges(상한가).
    #   union = (A) ∪ (B). build_daily 카드 + PICK 후보 둘 다 dailybars 누락 0.
    #
    # 비용: latest+acc+daily_top 통상 8~50종 + carded 37~60종, 중복 제거 후 ~50~100종.
    #   전종목 2585 보다 훨씬 적음 → ka10081 ~100콜 × 0.3s ≈ 30s → BATCH_LIMIT 400s 무관.
    #   휴장일은 양쪽 모두 비어 자연 0건.
    search_pass_codes = _load_search_pass_universe(today)
    with connect() as c:
        carded_rows = c.execute(
            """SELECT DISTINCT code FROM (
                 SELECT stock_code AS code FROM daily_picks WHERE date=?
                 UNION
                 SELECT stock_code AS code FROM intraday_snapshot WHERE date=?
                 UNION
                 SELECT stock_code AS code FROM stock_status_badges
                   WHERE date=? AND badge_type='상한가'
               )
               WHERE code IS NOT NULL AND length(code)=6""",
            (today, today, today),
        ).fetchall()
        carded_codes = {
            r[0] for r in carded_rows if r[0] and len(r[0]) == 6 and r[0].isdigit()
        }

        # Fix-12 (2026-06-22): 미정산(보유중) 픽 universe — 동일 connect 재사용.
        # running SSOT ∪ daily_picks 최근 9거래일 윈도우. 상세 docstring 위 helper 참조.
        # 할당 끝의 noqa F841 = 아래 union(line ~550)에서 소비하므로 autoflake/ruff
        # 자동제거 금지 (FLR-20260422-FLR-004 import/할당 재제거 함정 동형).
        open_position_codes = _load_open_position_codes(c, today)  # noqa: F841

    # Fix-11 (2026-06-18, 상한가 과소수집 근본책 — 대표 직접 지시):
    #   당일 상한가 종목(ka10027 등락률 desc >= 29.79%)을 universe 에 직접 편입.
    #   ROOT: v1 조건검색(거래대금 500억 ∪ 상한가)이 거래대금/조건식 미달 상한가
    #   소형주를 누락(예: 6/11 상한가 8종 중 서빙 dailybars 1종, 297090 +29.79%)
    #   → 그 종목 dailybars 가 cron 서빙 DB 에 처음부터 미적재 → limit-up-trend
    #   chain 과소수집. 본 fix 로 상한가 종목이 매 cycle universe 에 포함 → ka10081
    #   dailybars 적재 → chain 자체 포착(빌더 측 백필 ATTACH 보강에 대한 의존 제거).
    #   비용(대표 결정 — 고민 불요): ka10027 코스피+코스닥 통합 1~2콜 + 상한가 N종
    #   (통상 수~수십)의 ka10081. 전종목 일봉(~4288콜) over-collection 아님
    #   (FLR-20260527-TEC-001 cold-start stale 재현 회피).
    #   안전(FLR-AGT-002): fetch 실패 시 빈 set → carded ∪ search_pass 로 graceful.
    limit_up_codes = fetch_limit_up_codes(today)

    # union = 검색식 통과 ∪ carded ∪ 당일 상한가 ∪ 보유중(미정산) 픽. 정렬 stable.
    # Fix-12 (2026-06-22): open_position_codes 추가 — 보유 픽 forward 일봉 freeze 봉쇄.
    codes = sorted(
        search_pass_codes | carded_codes | limit_up_codes | open_position_codes
    )

    # picks_set = union 전체 (사용자 가시 카드 + PICK 후보 + 상한가 + 보유중 픽) → 매 cycle 재수집.
    picks_set = set(codes)
    print(
        f"[collect_dailybars] union universe: "
        f"{len(codes)} stocks (latest_stocks∪검색식통과 {len(search_pass_codes)} "
        f"∪ carded {len(carded_codes)} ∪ 상한가 {len(limit_up_codes)} "
        f"∪ 보유중픽 {len(open_position_codes)}, today={today})"
    )

    if not codes:
        print(
            "[collect_dailybars] 대상 종목 없음 "
            "(latest_stocks ∪ carded universe 0 — 휴장일/장 시작 전)"
        )
        return 0

    # S1 today-skip: stocks.last_updated 날짜가 오늘인 종목 조회 (skip 대상).
    # last_updated는 ISO8601 TEXT (예: "2026-04-22T14:28:03") → 앞 10자 date prefix 매치.
    # NULL 또는 다른 날짜는 skip 대상 아님 (정상 호출 필요).
    # FLR-20260429: stocks.last_updated가 오늘이어도 dailybars에 today 행이 없으면
    # ka10081을 재호출 (장중 키움이 4/29 봉 미반영한 채 응답하는 일시적 지연 사례).
    # 두 검사 AND 충족 시에만 skip — daily_20 시각 불일치 방지.
    #
    # Fix E-1-V2 (2026-05-14, Q-20260514-058):
    #   "장중 재호출 비용 낭비" 정책 유지 + 마감 후 1회 정식 fetch 강제 봉쇄.
    #   본질: 장중 첫 호출 → dailybars row 적재 (OHLC 4값 동일 cur_prc 가능) +
    #   stocks.last_updated 갱신 → 후속 cron이 already_today 충족 → 장 마감 후 cron도 skip
    #   → dailybars 오늘 row가 장중 stale OHLC 그대로 누적 (5/13 8건 사고).
    #
    # Fix E-1-V3 (2026-05-22, DOC-20260522 critical recurring 22종목 mismatch 본문):
    #   15:30 임계 너무 이름 → 시간외 단일가(15:40~16:00 종가만 거래 + 16:00~18:00 ±10%)
    #   본문 본질 미반영. ka10081 응답이 15:30~15:47 호출 시 incomplete day row 반환
    #   (장 중 마지막 tick 거래대금 → 단조 증가 누적 본질에서 시간외 단일가 거래대금 누락).
    #   대표 (A) ack 2026-05-22 18:23 KST: 16:00 임계 변경.
    #   외부 spec 검증 (WebSearch 2회 + KRX 공식 문서):
    #     - 정규장: 09:00~15:30 (15:20~15:30 종가 동시호가)
    #     - 시간외 종가: 15:40~16:00 (당일 종가로만 거래)
    #     - 시간외 단일가: 16:00~18:00 (±10%, 10분 단위)
    #     → 16:00은 시간외 종가 종료 시점 = 일봉 trde_prica 본질 누적 마감 임계.
    #     → 시간외 단일가(16:00~18:00)는 익일 기준가 영향 없음 (KRX 공식).
    #     → 본 임계 16:00로 변경 = 시간외 종가 시간대 trde_prica 누적 본질 fix.
    #
    #   환경변수 M1S_FORCE_DAILYBARS_RECOLLECT=1 시:
    #     after_close 무조건 True + already_today 무시 → 강제 재호출 (catch-up trigger 본문 본질).
    #     사용 예: kiwoom_cron.sh catch-up trigger 분기에서 set + pipeline.sh 호출.
    #
    #   해결 (16:00 임계): 현재 시각이 16:00 이후인데 last_updated가 16:00 이전이면 skip 해제.
    #   장중(now < 16:00): 기존 정책 — 첫 호출 후 매 cron skip (비용 낭비 회피).
    #   마감 후(now >= 16:00): last_updated >= 16:00 종목만 skip (시간외 종가 정식 OHLC 갱신 완료).
    #                          last_updated < 16:00 종목은 skip 해제 → 1회 강제 fetch.
    #   장중/시간외 종가 종목은 16:00 후 첫 cron(16:00~)에서 INSERT OR REPLACE로 정합 OHLC 정정.
    #   회귀 안전: 16:00 후 정식 갱신 후 last_updated >= 16:00 → 후속 cron 정상 skip.
    force_recollect = os.environ.get("M1S_FORCE_DAILYBARS_RECOLLECT", "").strip() == "1"
    now_dt = datetime.now()
    today_date = now_dt.strftime("%Y-%m-%d")
    # KRX 일봉 trde_prica 본문 본질 누적 마감 임계 = 16:00 (시간외 종가 종료 시점).
    market_close_dt = now_dt.replace(hour=16, minute=0, second=0, microsecond=0)
    after_close = force_recollect or (now_dt >= market_close_dt)
    # 마감 후이면 last_updated >= 16:00 필터 적용, 마감 전이면 종일 모두 skip 대상.
    # force 시: skip 자체 비활성화 (already_today 무시 본문은 line 추가 fix 본문 본질).
    threshold_clause = "  AND s.last_updated >= ? " if after_close else ""
    threshold_args = (f"{today_date}T16:00:00",) if after_close else ()
    if force_recollect:
        # FLR-20260526-TEC-001: force 시 _RUN_CACHE 도 무효화 (in-memory + file).
        # 미적용 시 line 460 cache check 가 강제 재호출을 차단 → force 가 no-op 화.
        _clear_run_cache(today)
        print(
            "[collect_dailybars] M1S_FORCE_DAILYBARS_RECOLLECT=1 — "
            "already_today skip 비활성화 + market_close_dt threshold 무시 + "
            "_RUN_CACHE 무효화 (resume cache clear)"
        )
    with connect() as c:
        placeholders = ",".join("?" * len(codes))
        already_rows = c.execute(
            f"SELECT s.code FROM stocks s "
            f"WHERE s.code IN ({placeholders}) "
            f"  AND s.last_updated IS NOT NULL "
            f"  AND substr(s.last_updated, 1, 10) = ? "
            f"{threshold_clause}"
            f"  AND s.price_high_240d IS NOT NULL "
            f"  AND EXISTS (SELECT 1 FROM dailybars d WHERE d.code=s.code AND d.date=?)",
            (*codes, today_date, *threshold_args, today_date),
        ).fetchall()
        already_today = {r[0] for r in already_rows}
        # Fix E-1-V3 (2026-05-22): force_recollect 본문 already_today 무시.
        # catch-up trigger 본문 본질 — 모든 종목 강제 재호출.
        if force_recollect:
            already_today = set()
        # Fix-8 (2026-05-26, FLR-20260526-TEC-001 근본 fix): 당일 picks 매 cycle 재수집.
        # 대표 결정 — 당일 데이터(사용자 가시 종목)는 매 10분 cycle 무조건 재수집, cache skip
        # 금지. daily_picks(today) 43종목은 카드/상한가추이/테마트리/거래대금추이 4 출력의
        # 당일 본문 → already_today 에서 제외 → 매 cycle ka10081 재호출 → 당일 봉 OHLC 최신
        # 반영 (장중 가격 변동 + 시간외 종가 + 마감 종가 모두 cycle 단위 갱신).
        # 비용: picks 43콜 × 0.3s ≈ 13s (BATCH_LIMIT_SEC=400s 의 3%). 키움 호출 폭증 무.
        # 과거(immutable) 봉만 보유한 rest 2760종목은 already_today 유지 → 키움 호출 절감
        # (16:00 마감 후 cron 에서 정식 종가 1회 갱신). 휴장일은 picks 자체가 비어 영향 0.
        already_today -= picks_set

    try:
        token = _get_token()
    except Exception as e:
        print(f"[collect_dailybars] token FAIL: {e}")
        return 0

    saved = 0
    # Fix-6 (2026-05-24): module-level flag reset (재진입 시 stale 신호 회피).
    global _LAST_TIMEOUT_BREAK
    _LAST_TIMEOUT_BREAK = False

    skipped = 0
    skipped_today = 0
    failed = 0
    timeout_break = False  # Layer B: batch limit break 본질 catch (line 522 marker용)
    batch_start_ts = time.monotonic()  # Layer B: per-fire 진행 시각
    now_iso = datetime.now().isoformat(timespec="seconds")
    for i, code in enumerate(codes):
        # Layer B: per-fire batch time limit 검사 (매 종목 진입 시점).
        # BATCH_LIMIT_SEC(400s) 초과 시 cache flush + 즉시 종료.
        # 다음 fire(launchd StartInterval=600s)에 resume cache 본질 정합 진입.
        elapsed = time.monotonic() - batch_start_ts
        if elapsed > BATCH_LIMIT_SEC:
            print(
                f"[collect_dailybars] Layer B batch limit reached "
                f"(elapsed={elapsed:.1f}s > {BATCH_LIMIT_SEC}s) — "
                f"flush cache + break for resume (next fire)"
            )
            _save_run_cache(today)
            timeout_break = True
            break
        # Fix-8 (2026-05-26, FLR-20260526-TEC-001 근본 fix): 거래일 picks 는 _RUN_CACHE
        # skip 면제 — 당일 데이터 매 cycle 재수집 보장 (대표 결정). picks 는 Layer A 우선
        # 정렬로 항상 첫 fire 에서 처리되므로 within-day resume(rest 2760) 동작에 영향 0.
        # 휴장일(today_is_trading_day=False)이면 picks 도 정상 cache skip (호출 절감).
        is_today_pick = today_is_trading_day and code in picks_set
        if code in _RUN_CACHE and not is_today_pick:
            skipped += 1
            continue
        # Fix-7 (2026-05-24, FLR-20260524-TEC-001 cache add-before-fetch stuck cascade 봉쇄):
        # 기존: line 462에서 fetch 진입 전 cache.add → "no data" / "parse fail" / 예외 시
        #       cache mark stuck → 다음 fire에서도 영구 skip (재시도 차단).
        # 본 fix: cache.add 위치를 (a) today-skip 분기 + (b) UPSERT commit 성공 분기 후로 이동.
        # 의미: 실패 분기(no data / parse fail / INVARIANT 예외)는 cache 0 add → 다음 fire 재시도 가능.
        # 영구 cache 의도 정합 (성공한 종목만 cache, 실패는 retry semantic).
        # flush trigger도 cache.add와 짝지어 같은 위치로 이동.
        # S1 today-skip: DB에 오늘 갱신 기록이 있으면 ka10081 호출 생략.
        if code in already_today:
            skipped_today += 1
            # today-skip = "처리 완료" semantic — cache add OK (다음 fire iteration 절약).
            _RUN_CACHE.add(code)
            if len(_RUN_CACHE) % BATCH_FLUSH_INTERVAL == 0:
                _save_run_cache(today)
            continue
        rows_resp = fetch_daily_chart(code, token)
        if not rows_resp:
            print(f"  [{i + 1}/{len(codes)}] {code} SKIP (no data)")
            failed += 1
            # Fix-7: cache add 0 — "no data" 는 일시 사고 (kiwoom rate limit / 종목 일시 응답 누락 등)
            # 다음 fire 재시도 정합. 영구 stuck 회피.
            if i < len(codes) - 1:
                time.sleep(0.3)
            continue
        stats = _calc_240d_stats(rows_resp)
        if not stats:
            print(f"  [{i + 1}/{len(codes)}] {code} SKIP (parse fail)")
            failed += 1
            # Fix-7: cache add 0 — parse fail 도 일시 사고 (응답 schema 일시 누락 등) 재시도 정합.
            if i < len(codes) - 1:
                time.sleep(0.3)
            continue
        # v4: dailybars 시계열 적재 (옵션 B — 시점별 240일 재계산용)
        # ka10081 응답 600+ 행 전부 INSERT OR REPLACE. 비용 거의 무시 (47종목 × 600행 ≈ 28k)
        bars_rows = []
        skipped_intraday = 0
        # Fix-8 (2026-05-26, FLR-20260526-TEC-001 근본 fix): 당일 봉 적재 여부 추적.
        # 당일(today_date) 봉이 실제 종가 봉으로 bars_rows 에 들어갔는지 플래그.
        # 경계 = "오늘 vs 과거": 오늘 봉이 intraday-skip(o==h==lo==c2&vol==0) 으로 빠지면
        # today_bar_persisted=False → 본 종목을 _RUN_CACHE 에 add 하지 않음 → 다음 cycle
        # 자연 재수집. 과거 봉은 이미 적재되었거나 변동 없으므로 cache lock 무해.
        today_bar_persisted = False
        for r in rows_resp:
            n = _normalize_row(r)
            if n:
                d, o, h, lo, c2, vol, ta = n
                # Fix-2 (2026-05-13): OHLC 4값이 모두 동일하고 cur_prc와 같으면 INSERT skip.
                # ka10081이 장중 또는 봉 미확정 시 OHLC 4 필드를 cur_prc로 채워 응답하는
                # 사양. 이 행을 적재하면 240일 range_240d 통계가 잘못 산출됨.
                # (010170 5/13 OHLC=29100/29100/29100/29100 catch).
                # 다음 cron에서 키움이 정상 OHLC를 반환하면 INSERT OR REPLACE로 갱신됨.
                #
                # Fix-5 (2026-05-22, FLR-20260406-TEC-001 SSOT 비대칭 동형 재발 critical):
                #   `vol == 0` 조건 추가 → backfill_dailybars_bulk.py:186-193 SSOT 정합.
                #   ROOT: collect 측은 vol 무관 skip / backfill 측은 vol==0 추가 조건 비대칭
                #   본문 → IPO 첫날 점상 + vol>0 (예: 마키나락스 477850 5/20 OHLC=60000/60000/
                #   60000/60000 따따블 점상, vol>0) 정상 데이터 catch 누락 사고. 본 fix는
                #   기존 cycle12 5/13 사고 (010170 OHLC=29100 동일 vol=0) 회귀 0 (vol==0
                #   조건 충족 → skip 유지). 따상·하한가·IPO 점상 (vol>0) 정상 적재.
                if (
                    o > 0
                    and h > 0
                    and lo > 0
                    and c2 > 0
                    and o == h == lo == c2
                    and vol == 0
                ):
                    skipped_intraday += 1
                    continue
                # REQ-008 Invariant 1 (data, dailybars 적재): high >= max(open, close, low).
                # 일봉 정의상 high가 OHLC 전체의 최댓값이어야 함. 위반 시 ka10081 응답
                # 부분 데이터(intraday 시점) 또는 파싱 오류 — 적재 차단해서 후행 잘못된
                # range_240d 산출 방지. low 0 행은 수정주가 보정 분할 흔적이므로 본 검사
                # 면제 (low<=0 케이스).
                if h > 0:
                    valid_others = [v for v in (o, c2) if v > 0]
                    if lo > 0:
                        valid_others.append(lo)
                    if valid_others and h < max(valid_others):
                        raise RuntimeError(
                            f"INVARIANT_VIOLATION dailybars.high < max(o,c,lo): "
                            f"code={code} date={d} o={o} h={h} lo={lo} c={c2}"
                        )
                # Fix-3 (2026-05-19, Q-20260519-CYCLE12-001):
                # volume + trade_amount 추가 적재 (기존 NULL 누락 본질 fix).
                bars_rows.append((code, d, o, h, lo, c2, vol, ta))
                # Fix-8 (2026-05-26, FLR-20260526-TEC-001): 당일 종가 봉 적재 확인.
                # intraday-skip(위 continue) 을 통과한 = 실제 종가 봉. 이 봉의 date 가
                # 오늘이면 당일 데이터가 dailybars 에 정상 landing → cache lock 허용.
                if d == today_date:
                    today_bar_persisted = True
        with connect() as c:
            c.execute(
                """UPDATE stocks
                     SET price_high_240d=?, price_high_240d_date=?,
                         price_low_240d=?,  price_low_240d_date=?,
                         pct_from_high_240d=?, pct_from_low_240d=?,
                         last_updated=?
                   WHERE code=?""",
                (
                    stats["high"],
                    stats["high_date"],
                    stats["low"],
                    stats["low_date"],
                    stats["pct_from_high"],
                    stats["pct_from_low"],
                    now_iso,
                    code,
                ),
            )
            if bars_rows:
                # Fix-3 (2026-05-19, Q-20260519-CYCLE12-001):
                # 8 column INSERT (기존 6 columns → volume/trade_amount NULL 누락 fix).
                # ON CONFLICT DO UPDATE: backfill_dailybars_bulk.py:204-213와 동형 UPSERT.
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
        # Fix-7 (2026-05-24, FLR-20260524-TEC-001):
        # UPSERT commit 성공 후에만 cache add — "완료" semantic 정합.
        # 실패(no data / parse fail / INVARIANT 예외) 분기는 cache 0 add → 다음 fire 재시도 가능.
        #
        # Fix-8 (2026-05-26, FLR-20260526-TEC-001 근본 fix): "오늘 vs 과거" 경계 게이트.
        # 대표 결정 — 당일 데이터는 매 10분 cycle 무조건 재수집, cache skip 절대 금지.
        # 과거(immutable) 봉만 cache OK. ka10081 은 종목 1콜로 전체 봉을 반환하므로
        # "과거만 재수집" 은 불가능 → cache lock 자체를 당일 봉 적재 성공에 종속시킴.
        #   - 거래일 + 당일 종가 봉 적재 성공(today_bar_persisted=True): cache lock (완료).
        #   - 거래일 + 당일 봉 intraday-skip(today_bar_persisted=False): cache lock 안 함 →
        #     다음 cycle 자연 재수집 → 종가 봉 landing 시점에 비로소 lock (stale 0).
        #   - 휴장일(today_is_trading_day=False): 당일 봉 부재가 정상 → cache lock 허용
        #     (주말·공휴일 매 cycle 전 종목 재호출 = 키움 호출 폭증 회귀 회피).
        # FLR-20260524-TEC-001 success-only semantic 의 "성공" 정의를 "당일 봉 적재 성공"
        # 으로 정밀화 (과거 봉만 적재된 부분 성공은 당일 관점에서 미완료).
        #   - 거래일 picks(당일 가시 종목): cache lock 절대 금지 → 매 cycle 재수집 (대표 결정).
        #     persisted resume file 에도 미기록 → 다음 cycle(별 process) 도 재수집.
        cache_lockable = (not is_today_pick) and (
            today_bar_persisted or not today_is_trading_day
        )
        if cache_lockable:
            _RUN_CACHE.add(code)
            if len(_RUN_CACHE) % BATCH_FLUSH_INTERVAL == 0:
                _save_run_cache(today)
        today_tag = "" if cache_lockable else " [TODAY-INTRADAY:re-fetch-next-cycle]"
        print(
            f"  [{i + 1}/{len(codes)}] {code} OK{today_tag} "
            f"days={stats['available_days']} high={stats['high']}@{stats['high_date']} "
            f"low={stats['low']}@{stats['low_date']} "
            f"cur={stats['current']} ({stats['pct_from_high']}% / +{stats['pct_from_low']}%)"
        )
        if i < len(codes) - 1:
            time.sleep(0.3)

    # Layer C: 정상 완료 시에도 final cache flush (다음 fire에 resume 상태 정합).
    _save_run_cache(today)

    timeout_marker = " [BATCH_LIMIT_BREAK]" if timeout_break else ""
    print(
        f"[collect_dailybars] {saved} saved / {skipped} cached / "
        f"{skipped_today} today-skip / {failed} failed "
        f"(of {len(codes)}) for {today}{timeout_marker}"
    )

    # =========================================================================
    # Fix-6 (2026-05-24, DOC-20260522-MIN-001 §21.4 #2 추가 fix):
    # timeout cascade alert cascade — stderr marker + exit code 2 cascade.
    #
    # 5/22 사고 (wall 8h 46min + 18회 SIGTERM cascade) 동안 alert 0건 → 대표
    # 직접 catch (08:25 KST) 전까지 인지 부재. alert 부재가 ROOT chain 일부
    # (DOC-20260522-MIN-001 §12).
    #
    # 본 fix:
    #   (a) timeout_break=True 시 stderr에 "TIMEOUT_CASCADE_DETECTED" marker
    #       prepend → pipeline.sh가 log grep 으로 catch + osascript 발화.
    #   (b) __main__ 분기에서만 sys.exit(2) — 직접 호출 (테스트/수동) 회귀 0,
    #       cron context (pipeline.sh _stage 경유) 에서만 exit code cascade.
    #   (c) 정상 종료는 기존 동작 유지 (return saved).
    # =========================================================================
    if timeout_break:
        ts = datetime.now().isoformat(timespec="seconds")
        sys.stderr.write(
            f"TIMEOUT_CASCADE_DETECTED at {ts} "
            f"(elapsed={time.monotonic() - batch_start_ts:.1f}s > "
            f"{BATCH_LIMIT_SEC}s, saved={saved}, total={len(codes)})\n"
        )
        sys.stderr.flush()
        # Fix-6: module-level flag set → __main__ 진입점에서 exit code 2 cascade.
        _LAST_TIMEOUT_BREAK = True

    return saved


if __name__ == "__main__":
    result = collect()
    # Fix-6 (2026-05-24, DOC-20260522-MIN-001 §21.4 #2):
    # cron context (pipeline.sh _stage 경유) 전용 exit code cascade.
    #   - exit 0: 정상 종료 (timeout cascade 미발생)
    #   - exit 2: timeout cascade 발생 (BATCH_LIMIT_SEC 초과 + batch flush + break)
    #             → pipeline.sh가 _stage rc=2 catch → osascript notification 발화
    #   - exit 1: collect() 자체 예외 (None 반환) — 기존 동작 호환
    # 직접 import 호출 (테스트/수동) 회귀 0 — __main__ 분기만 적용.
    if _LAST_TIMEOUT_BREAK:
        sys.exit(2)
    sys.exit(0 if result is not None else 1)
