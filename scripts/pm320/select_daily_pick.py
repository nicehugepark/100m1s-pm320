#!/usr/bin/env python3
"""PM320 매일 15:15 KST 거래대금 선정 스크립트 (통합 선정 모델, UNIFIED_SELECT).

대표 2026-06-05 확정 — 전체 결정트리 재정의 (unified_select_bt.py select_pick_for_day
verbatim 포팅, 토구사 검증 PASS). 직전 A안(volmax_priority)은 상한가/하한가/강세/우선주를
'갭 판정 後 1위탈락 + Step4 2위룰 제거'로 분산하고 거래대금터진음봉만 선제거한 부분 모델.
본 통합 모델은 **5종 전부 한 단계 OR 선제거로 통일**:

  매일 latest_stocks trade_amount desc 정렬, 원순위 top-5만:

  [선제거 — 5종 OR, 한 단계 동시] top-5 중 하나라도 해당하면 제거:
    - 상한가 (change_pct >= 29.0)
    - 하한가 (change_pct <= -29.0)
    - 강세 (최근 10영업일 중 3회+ 급등)
    - 우선주 (종목코드 끝자리 != 0)
    - 거래대금터진음봉 (당일 음봉 close<open AND
                       (당일 trade_amount >= 3천억 절대값 OR 최근 10영업일 거래대금 rank<=5))

  잔존 거래대금 desc 재정렬 → 새1위 / 새2위:
    IF 잔존 >= 2:
        IF 새1위 독보적(새T1 >= 새T2 * 1.75) AND 새1위 양봉(close>open):
            PICK = 새1위                                      [분기 1위픽]
        ELSE (독보적 아니거나 새1위 음봉/도지):
            PICK = 새2위                                      [분기 2위픽]
    ELSE IF 잔존 == 1:
        PICK = 단독 잔존 종목                                 [분기 단독픽]
    ELSE (잔존 == 0): 보류(미거래)                             [분기 보류]

  양봉 = 당일 종가 > 시가. 도지(==)는 양봉 아님 → 2위.
  봉색 결측 시 양봉 아님 처리 (FLR-AGT-002 거짓 충실성 차단 — 추정 금지) → 2위.

매매 파라미터 (대표 2026-06-09 재확정): 슬롯4 / -6.4% 물타기(1배=첫 매수와 동일 수량) /
+3.2% 익절 / 만기 = 물타기 거래 D+6, 물타기 안 한 거래 D+3.
(선정 로직 자체는 만기·물타기 비중 무관 — 만기/물타기 비중은 build_card_history.py 결과 판정용.)
매수 게이트 = 1천억(직전 3천억), 거래대금터진음봉 =
  음봉 AND (≥3천억 절대값 OR 최근 10영업일 거래대금 rank<=5).

직전 모델 이력:
  - 1f1ff80 (2026-05-28): 단순 RANK_TARGET=2 (GAP75 트리·우선주 제외 부재)
  - GAP75 (2026-06-01): T1/T2 1.75 갭 분기 + 우선주 제외 (원순위 1위 기준)
  - A안 volmax_priority / b5fbee6 (2026-06-05): 신규필터만 Step1 선제거, 나머지 4종 갭後 분산
  - **통합 (본 fix, 2026-06-05)**: 5종 전부 한 단계 선제거 통일 + 잔존풀 독보+양봉 1위 else 2위
출처 verbatim: projects/pm320/research/backtest-3d-3.2pct/unified_select_bt.py:199-364.

우선주(비보통주) 판정 규칙 (DOMINANT1ST_PREF_SUMMARY.md §1 verbatim):
  종목코드 6자리 마지막 자리가 '0'이 아니면 우선주.
    예 005935(삼성전자우) 끝자리 5 = 우선주.
  종목명 '우'/'우B'/'(전환)'/'우C' 접미 cross-check (불일치 시 code 규칙 우선).

강세 정의 (REQ-039 키움 수식 verbatim):
  어떤 영업일이 강세 = prev_close*1.15 <= high AND low*1.15 <= high
                     AND open*1.09 <= close.
  5개 가격 필드 모두 truthy + prev_close/open/low 양수일 때만 평가, 아니면 거짓
  (FLR-AGT-002 거짓 충실성 차단). 출처:
  projects/pm320/research/backtest-3d-3.2pct/bullish_filter_bt.py:53-69.

최근 10영업일 = 선정일 D 포함 직전 10거래일 [D-9 .. D].
  dailybars 거래일 시퀀스 기준.

입력:
  - latest_stocks: data/kiwoom/{오늘}.json (거래대금 상위 종목 스냅샷)
  - dailybars: stocks.db (강세 카운트용 일봉)

출력:
  - 콘솔: 분기 + 1위/2위 산출 + 상한가/강세/우선주 제외 동작 요약
  - JSON: projects/pm320/data/daily/picks/{오늘}.json
    (claude routine 이 읽어서 푸시)

사용:
  python3 scripts/pm320/select_daily_pick.py [--date YYYY-MM-DD] [--dry-run]
  --date 미지정 시 오늘(KST) 자동.
  --dry-run 시 JSON 미저장, 콘솔 출력만.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ── 경로 (env M1S_HOMEPAGE 우선, fallback 옛 home — 5/31 qa-stale-fix 동형) ──
HOMEPAGE = (
    os.environ.get("M1S_HOMEPAGE") or "/Users/seongjinpark/company/100m1s-homepage"
)
KIWOOM_DIR = os.path.join(HOMEPAGE, "data/kiwoom")
DB_PATH = os.path.join(HOMEPAGE, "data/stocks.db")
COMPANY = os.environ.get("M1S_COMPANY") or "/Users/seongjinpark/company/100m1s"
OUT_DIR = os.path.join(COMPANY, "projects/pm320/data/daily/picks")

# ── 선정 기준 상수 (GAP75_RULE_SUMMARY.md §1 verbatim) ───────────────────────
# 비가변 공통 룰 (모든 프로파일 동일, 코드 상수 유지):
GAP_MULT = 1.75  # 독보적 갭: T1 >= T2 * 1.75 (원1위가 원2위보다 75%+ 큰 갭)
TOP_N = 5  # Step1 원순위 top-5
LIMIT_PCT = 29.0  # 상한가 판정 (change_pct >= 29.0)
LIMIT_DOWN_PCT = -29.0  # 하한가 판정 (change_pct <= -29.0)
BULL_RECENT_N = 10  # 최근 N 영업일 (D 포함), bullish_filter_bt.py:45
BULL_MIN_COUNT = 3  # 강세 >= 이 수 면 제외, bullish_filter_bt.py:46

# ── 가변 4축 ①② (전략 프로파일 SSOT, profiles.json active) ──────────────────
# 직전 하드코딩(MIN_TRADE_AMOUNT=1e11 / BEAR_AMOUNT_MIN=3e11)을 프로파일 참조로 교체.
# active 미변경 시 동일 값 산출 (회귀 byte-identical 의무). env M1S_PM320_PROFILE 로 강제 가능.


def _load_strategy():
    """전략 프로파일 active + 핸들러 레지스트리 로드 (profiles.json SSOT).

    함수-로컬 import 로 strategy_profiles 를 가져온다 — autoflake 가 모듈-레벨 import 를
    "미사용"으로 오제거하는 문제 회피. 패키지(-m)/직접 실행 양쪽 호환 (path 선삽입).
    return: (active_profile, BEAR_FILTERS_registry).
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from strategy_profiles import load_active_profile
    from strategy_profiles.registry import BEAR_FILTERS

    return load_active_profile(), BEAR_FILTERS


_PROFILE, BEAR_FILTERS = _load_strategy()
# 축① 매수 게이트(Step0 거래대금 하한, 원): top-5 후보 풀 진입 前 미달 종목 제외(선제거보다 先).
MIN_TRADE_AMOUNT = _PROFILE.min_trade_amount_won
# 축② 거래대금터진음봉 핸들러 키 (registry.BEAR_FILTERS 등록 핸들러명). 새 필터 = 핸들러 등록만.
BEAR_FILTER = _PROFILE["bear_filter"]
# abs3000 계열 절대값 임계(원). 비-abs 핸들러는 None (각 핸들러가 자기 파라미터 사용).
BEAR_AMOUNT_MIN = _PROFILE.bear_amount_min_won
# rank5_10d 윈도우 파라미터 (origin baseline 정합: 최근 10영업일 당일 포함, rank<=5).
VOLMAX_RECENT_N = int(_PROFILE.get("bear_recent_n", 10))
VOLMAX_RANK_MAX = int(_PROFILE.get("bear_rank_max", 5))
BEAR_FILTER_LABELS = {
    "abs3000": "음봉 AND 거래대금 >= 3천억",
    "rank5_10d": "음봉 AND 최근 10영업일 거래대금 rank<=5",
    "abs3000_or_rank5_10d": "음봉 AND (거래대금 >= 3천억 OR 최근 10영업일 거래대금 rank<=5)",
}


def _bear_filter_label():
    return BEAR_FILTER_LABELS.get(BEAR_FILTER, BEAR_FILTER)


# 종목명이 우선주를 시사하는 접미 (code 규칙과 cross-check 용)
_PREF_NAME_SUFFIX = ("우", "우B", "(전환)", "우C")

KST = timezone(timedelta(hours=9))


class KiwoomInputError(Exception):
    """kiwoom 입력 JSON 파싱 실패 (손상/conflict marker/인코딩) 신호 (FLR-20260615-FLR-001).

    select() 가 json.load 실패 시 raise → main() 이 포착 → exit 2 (파일 부재와 동일
    "데이터 없음" 신호로 통일, exit 1 uncaught traceback 회피) + stale 동격 비상 알람.
    cascade #3 (build_card_history autostash 가 kiwoom json 에 `<<<<<<< Updated upstream`
    conflict marker 박음 → malformed JSON → select uncaught JSONDecodeError 크래시) 봉쇄.
    """


# ── 입력 freshness 게이트 (FLR-20260615-FLR-001 재발 방지 P1) ─────────────────
# 거래대금 입력 data/kiwoom/{date}.json 의 내부 last_snapshot_at 이 픽 선정 시각 대비
# N분 초과 stale 이면 경고 로그 + osascript 비상 알람. 픽은 진행하되 picks JSON 에
# input_freshness 메타로 stale 명시 (조용한 오선정 차단 — 화면 라벨/추후 진단용).
# 근본: cron 인프라 git tree 공유 → SSOT-guard SKIP → 거래대금 cron 4h 정지 →
# 메인 repo 입력 stale(11:35, 4종목) → HL 오선정(장중 15:34 정상은 테크윙).
# mtime 은 cron 재배포로 부정확(전 파일 동일 시각 flip) → 내부 last_snapshot_at 사용.
# env M1S_PICK_FRESHNESS_MIN 으로 임계 조정 가능 (기본 30분). 0 이하면 게이트 비활성.
FRESHNESS_THRESHOLD_MIN = int(os.environ.get("M1S_PICK_FRESHNESS_MIN", "30"))


def _is_bullish(prev_close, open_p, high, low, close) -> bool:
    """REQ-039 키움 강세 수식. bullish_filter_bt.py:53-69 verbatim.

    5개 가격 필드 모두 truthy + prev_close/open/low 양수일 때만 평가, 아니면 거짓.
    """
    try:
        if not all([prev_close, open_p, high, low, close]):
            return False
        prev_close = float(prev_close)
        open_p = float(open_p)
        high = float(high)
        low = float(low)
        close = float(close)
        if prev_close <= 0 or open_p <= 0 or low <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return prev_close * 1.15 <= high and low * 1.15 <= high and open_p * 1.09 <= close


def load_dailybars(conn):
    """code -> [(date, open, high, low, close)] date 오름차순 + date->idx map.

    martingale_bt.py:66-77 verbatim.
    """
    bars = defaultdict(list)
    cur = conn.execute(
        "SELECT code, date, open, high, low, close FROM dailybars ORDER BY code, date"
    )
    for code, date, o, h, low, c in cur:
        bars[code].append((date, o, h, low, c))
    idx = {}
    for code, series in bars.items():
        idx[code] = {row[0]: i for i, row in enumerate(series)}
    return bars, idx


def load_trade_amounts(conn):
    """code -> {date: trade_amount}. dailybars 의 trade_amount 컬럼 (기존 컬럼).

    volmax_bear_filter_bt.py:120-128 verbatim. baseline 과 동일 dailybars 테이블,
    새 소스 도입 없음.
    """
    ta = defaultdict(dict)
    cur = conn.execute(
        "SELECT code, date, trade_amount FROM dailybars ORDER BY code, date"
    )
    for code, date, amt in cur:
        ta[code][date] = amt
    return ta


def _bear_today(bars, idx, d, code):
    """당일 음봉(close<open) 여부 + i0/series 반환. 데이터 결측 시 (None, ...).

    return: (is_bear: bool|None, i0: int|None, series: list|None).
    is_bear None = 데이터 부족(해당 종목 일봉 D 부재 / open·close 결측) → 추정 금지.
    """
    if code not in idx or d not in idx[code]:
        return None, None, None
    i0 = idx[code][d]
    series = bars[code]  # (date, open, high, low, close)
    _date, o0, _h0, _l0, c0 = series[i0]
    if o0 is None or c0 is None:
        return None, None, None
    return (c0 < o0), i0, series


# ── 거래대금터진음봉(축②) 핸들러 — 레지스트리 등록형 (open-for-extension) ────────
# 새 필터(예 "abs5000" / "rank3_5d" / 신규 정의)는 @BEAR_FILTERS.register("name") 핸들러
# 1개 추가만으로 엔진 무수정 동작. is_volmax_bear 는 프로파일 키로 핸들러 lookup 만 한다.
# 핸들러 시그니처: (bars, idx, ta, d, code) -> bool. 데이터 결측 시 False (추정 금지).


@BEAR_FILTERS.register("abs3000")
def _bear_abs3000(bars, idx, ta, d, code):
    """[abs3000] 당일 음봉 AND 당일 trade_amount >= 절대값 임계(BEAR_AMOUNT_MIN).

    순위·윈도우·기간 무관. 결측 시 거짓. 백테스트 bear3000_is_volmax_bear verbatim.
    """
    is_bear, _i0, _series = _bear_today(bars, idx, d, code)
    if not is_bear:
        return False
    today_amt = ta.get(code, {}).get(d)
    if today_amt is None:
        return False
    return bool(today_amt >= BEAR_AMOUNT_MIN)


@BEAR_FILTERS.register("rank5_10d")
def _bear_rank5_10d(bars, idx, ta, d, code):
    """[rank5_10d] 당일 음봉 AND 당일 거래대금 최근 N영업일(당일포함) rank<=MAX.

    rank = strict-greater 개수 + 1 (동률 미가산). 결측 시 거짓.
    origin baseline volmax_10d_top5_bt.py:187-190 verbatim.
    """
    is_bear, i0, series = _bear_today(bars, idx, d, code)
    if not is_bear:
        return False
    lo = max(0, i0 - (VOLMAX_RECENT_N - 1))
    code_ta = ta.get(code, {})
    today_amt = code_ta.get(d)
    if today_amt is None:
        return False
    window_amts = []
    for i in range(lo, i0 + 1):
        a = code_ta.get(series[i][0])
        if a is None:
            continue
        window_amts.append(a)
    if not window_amts:
        return False
    rank = sum(1 for a in window_amts if a > today_amt) + 1
    return bool(rank <= VOLMAX_RANK_MAX)


@BEAR_FILTERS.register("abs3000_or_rank5_10d")
def _bear_abs3000_or_rank5_10d(bars, idx, ta, d, code):
    """[abs3000_or_rank5_10d] 당일 음봉 AND (3천억 이상 OR 10영업일 거래대금 rank<=5)."""
    return bool(
        _bear_abs3000(bars, idx, ta, d, code) or _bear_rank5_10d(bars, idx, ta, d, code)
    )


def is_volmax_bear(bars, idx, ta, d, code):
    """거래대금터진음봉 제외 조건 — 프로파일 BEAR_FILTER 키로 레지스트리 핸들러 dispatch.

    미등록 키는 BEAR_FILTERS.get() 가 ValueError (silent fallback 금지, FLR-AGT-002).
    라이브 시그니처는 bool 반환 (_remove_reason 가 직접 평가).
    """
    return BEAR_FILTERS.get(BEAR_FILTER)(bars, idx, ta, d, code)


def bullish_count_recent(bars, idx, code, d):
    """선정일 D 포함 직전 10거래일 중 강세 True 일수.

    bullish_filter_bt.py:72-89 verbatim 로직.
    반환 (count, window_days) 또는 None (해당 종목 일봉에 D 부재).
    """
    if code not in idx or d not in idx[code]:
        return None
    i0 = idx[code][d]
    series = bars[code]
    lo = max(0, i0 - (BULL_RECENT_N - 1))
    cnt = 0
    days = 0
    for i in range(lo, i0 + 1):
        days += 1
        _date, o, h, low, c = series[i]
        prev_close = series[i - 1][4] if i >= 1 else None
        if _is_bullish(prev_close, o, h, low, c):
            cnt += 1
    return cnt, days


def db_change_pct(bars, idx, d, code, kch):
    """당일 change_pct: dailybars (close[D]/close[D-1]-1)*100 우선, 없으면 kiwoom kch.

    filter_first_bt.is_limit_up 내부 계산과 verbatim 동일.
    """
    db_ch = None
    if code in idx and d in idx[code]:
        i0 = idx[code][d]
        if i0 >= 1:
            pc = bars[code][i0 - 1][4]
            p0 = bars[code][i0][4]
            db_ch = (p0 / pc - 1.0) * 100.0 if pc else None
    return db_ch if db_ch is not None else kch


def is_limit_up(bars, idx, d, code, kch):
    """당일 상한가 판정 (change_pct >= 29.0)."""
    ch = db_change_pct(bars, idx, d, code, kch)
    return ch is not None and ch >= LIMIT_PCT


def is_limit_down(bars, idx, d, code, kch):
    """당일 하한가 판정 (change_pct <= -29.0). is_limit_up 대칭."""
    ch = db_change_pct(bars, idx, d, code, kch)
    return ch is not None and ch <= LIMIT_DOWN_PCT


def is_bullish_strong(bars, idx, code, d):
    """강세 10영업일 3회+ (bullish_filter_bt verbatim)."""
    bc = bullish_count_recent(bars, idx, code, d)
    return bc is not None and bc[0] >= BULL_MIN_COUNT


def is_bullish_candle(bars, idx, d, code):
    """당일 양봉 판정 — 종가 > 시가. 도지(==)는 양봉 아님. 결측=거짓(추정 금지).

    unified_select_bt.py:90-107 verbatim. 반환 (is_bull: bool, candle: str|None).
    candle = '양봉'|'음봉'|'도지'|None(봉색결측). 봉색 결측 시 (False, None).
    """
    if code not in idx or d not in idx[code]:
        return False, None
    i0 = idx[code][d]
    _date, o0, _h0, _l0, c0 = bars[code][i0]  # (date, open, high, low, close)
    if o0 is None or c0 is None:
        return False, None
    if c0 == o0:
        return False, "도지"
    if c0 > o0:
        return True, "양봉"
    return False, "음봉"


def is_preferred(code, name=None):
    """우선주(비보통주) 판정. 종목코드 6자리 마지막 자리가 '0'이 아니면 우선주.

    GAP75_RULE_SUMMARY.md §1 verbatim. 보통주는 끝자리 0.
    예 005935(삼성전자우) 끝자리 5 = 우선주.
    종목명 cross-check (우/우B/(전환)/우C 접미). 불일치 시 code 규칙 우선.
    """
    if not code:
        return False
    return code[-1] != "0"


def _amt(it):
    """latest_stocks item → trade_amount (None → 0)."""
    return it.get("trade_amount") or 0


def _remove_reason(bars, idx, ta, d, code, name, kch):
    """통합 모델 5종 OR 선제거 사유. 없으면 None.

    상한가 → 하한가 → 강세 → 우선주 → 거래대금터진음봉 순으로 평가 (OR, 한 단계).
    unified_select_bt.py:199-214 (remove_reason) verbatim 순서.
    """
    if is_limit_up(bars, idx, d, code, kch):
        return "상한가"
    if is_limit_down(bars, idx, d, code, kch):
        return "하한가"
    if is_bullish_strong(bars, idx, code, d):
        bc = bullish_count_recent(bars, idx, code, d)
        return f"강세{bc[0]}회"
    if is_preferred(code, name):
        return "우선주"
    if is_volmax_bear(bars, idx, ta, d, code):
        return "거래대금터진음봉"
    return None


def _row_brief(rank, it, bars, idx, d):
    """ranked / survivors 출력용 brief row."""
    bc = bullish_count_recent(bars, idx, it["ticker"], d)
    return {
        "rank": rank,
        "code": it["ticker"],
        "name": it.get("name", ""),
        "trade_amount": it.get("trade_amount"),
        "change_pct": it.get("change_pct"),
        "bullish_count": bc[0] if bc is not None else None,
    }


def select(date_str, bars, idx, ta, kiwoom_path):
    """단일 거래일 통합 선정 로직 (UNIFIED_SELECT, 5종 OR 선제거 통일).

    대표 2026-06-05 확정 — 전체 결정트리 재정의.
    unified_select_bt.py:217-364 (select_pick_for_day) verbatim 구조:

      1. raw 거래대금 desc top-5.
      2. [선제거 — 5종 OR, 한 단계 동시] top-5 중 상한가|하한가|강세|우선주|
         거래대금터진음봉 인 종목 제거 → survivors.
      3. 잔존 거래대금 desc 재정렬 → 새1위 / 새2위.
      4. IF 잔존 >= 2:
             IF 새1위 독보적(T1>=T2*1.75) AND 새1위 양봉 → PICK=새1위 [1위픽]
             ELSE → PICK=새2위                                     [2위픽]
         ELSE IF 잔존 == 1: PICK=단독 잔존 종목                     [단독픽]
         ELSE (잔존 == 0): 보류(미거래)                              [보류]

    양봉 = 종가 > 시가. 도지(==)·음봉·봉색결측 = 양봉 아님 → 2위.
    """
    # ── 입력 JSON 파싱 (Finding A, FLR-20260615-FLR-001 cascade #3 봉쇄) ──────
    # malformed / conflict marker(`<<<<<<< Updated upstream`) / 인코딩 깨진 입력은
    # uncaught JSONDecodeError 로 exit 1 traceback 크래시. KiwoomInputError 로 변환 →
    # main() 이 파일 부재와 동일 exit 2 ("데이터 없음") + 비상 알람으로 통일.
    try:
        with open(kiwoom_path, encoding="utf-8") as fh:
            kiwoom_raw = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # 메시지는 path + 원인만 — 사람용 prefix 는 _emit_parse_alarm 이 1회 부여(중복 회피).
        raise KiwoomInputError(f"{kiwoom_path} — {exc}") from exc
    ls = kiwoom_raw.get("latest_stocks") or []
    # 입력 신선도 계산 (FLR-20260615-FLR-001) — 선정 로직 비훼손, 메타로만 박제.
    input_freshness = _compute_input_freshness(kiwoom_raw)  # noqa: F841 (return dict 사용)

    # ── Step0a falsy ticker 선제 drop (Finding B, FLR-20260615-FLR-001) ───────
    # ticker 키 부재/빈문자열 종목은 downstream it["ticker"] 직접 접근에서 KeyError
    # 또는 empty-ticker garbage 단독픽을 유발. "데이터 부족"으로 간주해 입력단에서 제거.
    # 이후 모든 ticker 접근(_row_brief / pre_rank_map / _remove_reason 등)은 안전.
    # src_count 는 raw 입력 크기 의미 유지 (정상 입력 회귀 byte-동일) — drop 수는 별도 메타.
    src_count = len(ls)
    ls = [it for it in ls if isinstance(it, dict) and it.get("ticker")]
    ticker_dropped = src_count - len(ls)

    # ── Step0 거래대금 하한 게이트 (Q-20260608-131 → 대표 2026-06-09 재확정) ──
    # 매수 게이트 = **1천억** (직전 3천억 → 하향). top-5 후보 풀 진입 前
    # (5종 OR 선제거보다 先) trade_amount >= MIN_TRADE_AMOUNT(1천억) 미달 종목 제외.
    # 단위: 원(won), 1천억 = 1e11. gated_out 은 drop 後 모집단 기준(금액게이트 탈락만).
    ls_gated = [it for it in ls if _amt(it) >= MIN_TRADE_AMOUNT]
    gated_out = len(ls) - len(ls_gated)

    # ── Step1 raw 거래대금 desc 정렬 → 원순위 top-5 (선제거 前, 기록용) ──────
    pre_sorted = sorted(ls_gated, key=_amt, reverse=True)
    top5 = pre_sorted[:TOP_N]
    ranked = [_row_brief(r, it, bars, idx, date_str) for r, it in enumerate(top5, 1)]
    pre_rank_map = {it["ticker"]: r for r, it in enumerate(top5, 1)}

    orig1 = top5[0] if len(top5) >= 1 else None
    orig2 = top5[1] if len(top5) >= 2 else None
    orig_T1 = _amt(orig1) if orig1 else 0
    orig_T2 = _amt(orig2) if orig2 else 0
    o1c = orig1["ticker"] if orig1 else None
    o1n = orig1.get("name", "") if orig1 else None

    # ── Step2 [5종 OR 선제거] top-5 중 상한가|하한가|강세|우선주|거래대금터진음봉 ──
    removed = []
    survivors = []
    for it in top5:
        code = it["ticker"]
        name = it.get("name", "")
        kch = it.get("change_pct")
        reason = _remove_reason(bars, idx, ta, date_str, code, name, kch)
        if reason:
            removed.append(
                {
                    "code": code,
                    "name": name,
                    "pre_rank": pre_rank_map.get(code),
                    "trade_amount": _amt(it),
                    "reason": reason,
                }
            )
        else:
            survivors.append(it)
    removed_brief = [
        f"{rm['name']}({rm['code']}, 원{rm['pre_rank']}위) {rm['reason']}"
        for rm in removed
    ]

    # ── Step3 잔존 거래대금 desc 재정렬 → 새 T1/T2 재계산 ──────────────────
    surv_sorted = sorted(survivors, key=_amt, reverse=True)
    new1 = surv_sorted[0] if len(surv_sorted) >= 1 else None
    new2 = surv_sorted[1] if len(surv_sorted) >= 2 else None
    T1 = _amt(new1) if new1 else 0
    T2 = _amt(new2) if new2 else 0
    n1c = new1["ticker"] if new1 else None
    n1n = new1.get("name", "") if new1 else None
    n1kch = new1.get("change_pct") if new1 else None
    n2c = new2["ticker"] if new2 else None
    n2n = new2.get("name", "") if new2 else None

    gap_ratio = (T1 / T2) if T2 else (float("inf") if T1 > 0 else 0.0)
    gap_dominant = T1 >= T2 * GAP_MULT  # T2=0 이면 T1>=0 항상 참 (독보 간주)
    n1_is_bull, n1_candle = (
        is_bullish_candle(bars, idx, date_str, n1c) if new1 else (False, None)
    )

    # 원1위가 선제거되어 새1위가 달라졌는지 (승격 여부)
    promotion = bool(n1c and o1c and n1c != o1c)
    promo_note = (
        f" [원1위 {o1n}({o1c}) 선제거→ 새1위 {n1n}({n1c}) 승격]" if promotion else ""
    )
    rm_note = f" [선제거: {', '.join(removed_brief)}]" if removed_brief else ""
    candle_note = f"[{n1_candle}]" if n1_candle else "[봉색결측]"
    ratio_s = f"{gap_ratio:.3f}" if gap_ratio != float("inf") else "inf(T2=0)"

    # ── Step4 잔존 판정 ───────────────────────────────────────────────────
    branch = None
    picked = None
    branch_reason = ""

    if len(surv_sorted) == 0:
        branch = "보류"
        branch_reason = f"5종 선제거 後 잔존 0개 → 보류(미거래){rm_note}"
    elif len(surv_sorted) == 1:
        branch = "단독픽"
        bc = bullish_count_recent(bars, idx, n1c, date_str)
        picked = {
            "rank": 1,
            "code": n1c,
            "name": n1n,
            "trade_amount": T1,
            "change_pct": n1kch,
            "bullish_count": bc[0] if bc is not None else None,
        }
        branch_reason = (
            f"5종 선제거 後 잔존 1개 → PICK=단독 잔존 {n1n}({n1c}){promo_note}{rm_note}"
        )
    elif gap_dominant and n1_is_bull:
        branch = "1위픽"
        bc = bullish_count_recent(bars, idx, n1c, date_str)
        picked = {
            "rank": 1,
            "code": n1c,
            "name": n1n,
            "trade_amount": T1,
            "change_pct": n1kch,
            "bullish_count": bc[0] if bc is not None else None,
        }
        branch_reason = (
            f"독보적 갭(T1/T2={ratio_s}>=1.75) AND 새1위 {candle_note} 양봉 → "
            f"PICK=새1위 {n1n}({n1c}){promo_note}{rm_note}"
        )
    else:
        branch = "2위픽"
        if not gap_dominant:
            why = f"갭부족(T1/T2={ratio_s}<1.75)"
        else:
            why = f"독보적이나 새1위 {candle_note} 양봉 아님"
        n2kch = new2.get("change_pct")
        bc = bullish_count_recent(bars, idx, n2c, date_str)
        picked = {
            "rank": 2,
            "code": n2c,
            "name": n2n,
            "trade_amount": T2,
            "change_pct": n2kch,
            "bullish_count": bc[0] if bc is not None else None,
        }
        branch_reason = f"{why} → PICK=새2위 {n2n}({n2c}){promo_note}{rm_note}"

    return {
        "date": date_str,
        "criteria_version": "2026-06-05-unified-select",
        "profile_id": _PROFILE.profile_id,
        "bear_filter": BEAR_FILTER,
        "bear_filter_rule": _bear_filter_label(),
        "rule": (
            "UNIFIED_SELECT: top-5 중 상한가|하한가|강세|우선주|거래대금터진음봉 "
            f"({_bear_filter_label()}) "
            "5종 OR 한단계 선제거 → 잔존 desc 재정렬 → 잔존>=2 AND 독보(T1>=T2*1.75) "
            "AND 새1위 양봉 → 1위픽 / else 2위픽 / 잔존=1 단독픽 / 잔존=0 보류"
        ),
        "branch": branch,
        "branch_reason": branch_reason,
        "T1": T1,
        "T2": T2,
        "gap_ratio": round(gap_ratio, 4) if gap_ratio != float("inf") else None,
        "gap_dominant": bool(gap_dominant),
        "orig1_code": o1c,
        "orig1_name": o1n,
        "orig1_amount": orig_T1,
        "orig2_code": orig2["ticker"] if orig2 else None,
        "orig2_name": orig2.get("name", "") if orig2 else None,
        "orig2_amount": orig_T2,
        "new1_code": n1c,
        "new1_name": n1n,
        "new1_candle": n1_candle,
        "new1_is_bull": bool(n1_is_bull),
        "new2_code": n2c,
        "new2_name": n2n,
        "promotion": promotion,
        "removed": removed,
        "removed_brief": removed_brief,
        "picked": picked,
        "ranked": ranked,
        "source_count": src_count,
        "ticker_dropped_count": ticker_dropped,
        "min_trade_amount_gate": MIN_TRADE_AMOUNT,
        "gated_in_count": len(ls_gated),
        "gated_out_count": gated_out,
        "top5_count": len(top5),
        "bear_amount_min": BEAR_AMOUNT_MIN,
        "bear_recent_n": VOLMAX_RECENT_N,
        "bear_rank_max": VOLMAX_RANK_MAX,
        "input_freshness": input_freshness,
    }


def _compute_input_freshness(kiwoom_raw, now=None):
    """거래대금 입력 json 의 last_snapshot_at 기준 신선도 계산 (FLR-20260615-FLR-001).

    kiwoom_raw: kiwoom/{date}.json 을 json.load 한 dict 전체.
    now: 기준 시각 (미지정 시 현재 KST = 픽 선정 시각).
    return: input_freshness 메타 dict (항상 반환, picks JSON 에 박제).
      - last_snapshot_at: 입력 내부 최신 갱신 시각 (raw 문자열, 없으면 None)
      - staleness_minutes: now - last_snapshot_at (분, float; 측정 불가 시 None)
      - is_stale: staleness_minutes > 임계 (측정 불가 시 True — 보수적 경고)
      - threshold_min / measurable / reason.

    mtime 이 아닌 내부 timestamp 사용 이유: cron 재배포가 전 파일 mtime 을
    동일 시각으로 flip 하므로 mtime 은 데이터 신선도를 반영 못 함.
    """
    if now is None:
        now = datetime.now(KST)
    last_raw = None
    if isinstance(kiwoom_raw, dict):
        # last_snapshot_at(누적 스냅샷 최신) 우선, 폴백 first_snapshot_at.
        last_raw = kiwoom_raw.get("last_snapshot_at") or kiwoom_raw.get(
            "first_snapshot_at"
        )
    meta = {
        "last_snapshot_at": last_raw,
        "staleness_minutes": None,
        "is_stale": True,
        "threshold_min": FRESHNESS_THRESHOLD_MIN,
        "measurable": False,
        "checked_at": now.isoformat(),
        "reason": None,
    }
    if FRESHNESS_THRESHOLD_MIN <= 0:
        # 게이트 비활성 (env 로 0 이하 설정) — stale 판정 안 함.
        meta["is_stale"] = False
        meta["reason"] = "freshness 게이트 비활성 (threshold<=0)"
        return meta
    if not last_raw:
        meta["reason"] = (
            "입력에 last_snapshot_at/first_snapshot_at 부재 — 신선도 측정 불가"
        )
        return meta
    try:
        snap = datetime.fromisoformat(last_raw)
    except (ValueError, TypeError):
        meta["reason"] = f"last_snapshot_at 파싱 실패: {last_raw!r}"
        return meta
    if snap.tzinfo is None:
        snap = snap.replace(tzinfo=KST)
    delta_min = (now - snap).total_seconds() / 60.0
    meta["staleness_minutes"] = round(delta_min, 1)
    meta["measurable"] = True
    meta["is_stale"] = bool(delta_min > FRESHNESS_THRESHOLD_MIN)
    if meta["is_stale"]:
        meta["reason"] = (
            f"입력 last_snapshot_at={last_raw} 이 픽 시각 대비 "
            f"{delta_min:.0f}분 stale (임계 {FRESHNESS_THRESHOLD_MIN}분 초과)"
        )
    else:
        meta["reason"] = f"신선 ({delta_min:.0f}분 ≤ {FRESHNESS_THRESHOLD_MIN}분)"
    return meta


def _emit_stale_alarm(date_str, freshness):
    """stale 입력 경고 — stderr 로그 + osascript 비상 알람 (best-effort, 비차단).

    픽 자체는 차단하지 않는다(기존 로직 비훼손). 조용한 오선정만 차단.
    """
    msg = freshness.get("reason") or "입력 신선도 미상"
    print(f"[freshness 경고] {date_str}: {msg}", file=sys.stderr)
    sm = freshness.get("staleness_minutes")
    sm_s = f"{sm:.0f}분" if isinstance(sm, (int, float)) else "측정불가"
    notif = (
        f"PM320 픽 입력 STALE — {date_str} 거래대금 {sm_s} 지연. "
        f"kiwoom cron SSOT-guard SKIP 점검 (FLR-20260615-FLR-001)"
    )
    _notify("100m1s PM320 STALE", notif)


def _notify(title, body):
    """macOS osascript 비상 알람 (best-effort, 비차단). injection 방어 공유 경로.

    title/body 의 따옴표·백슬래시를 제거해 AppleScript 문자열 escape 안전 확보
    (_emit_stale_alarm / _emit_parse_alarm 공통 사용 — 단일 hardened path).
    """
    title = title.replace("\\", "").replace('"', "'")
    body = body.replace("\\", "").replace('"', "'")
    try:
        subprocess.run(  # noqa: S603 (고정 osascript 명령, 신뢰 입력)
            [
                "/usr/bin/osascript",  # 절대경로 (macOS 표준 고정) — S607 회피
                "-e",
                f'display notification "{body}" with title "{title}"',
            ],
            check=False,
            timeout=5,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        # 알람 실패는 픽을 막지 않는다 (로그는 이미 남김).
        pass


def _emit_parse_alarm(date_str, detail):
    """kiwoom 입력 파싱 실패 경고 — stderr + osascript 알람 (Finding A, stale 동격).

    select() 가 KiwoomInputError raise 시 main() 에서 호출. 픽은 exit 2 로 종료
    (파일 부재와 동일 "데이터 없음" 신호). cascade #3 (conflict marker malformed JSON).
    """
    print(
        f"데이터 부족 — kiwoom JSON 파싱 실패(손상/conflict marker 의심): {detail}",
        file=sys.stderr,
    )
    notif = (
        f"PM320 픽 입력 손상 — {date_str} kiwoom JSON 파싱 실패. "
        f"conflict marker/malformed 의심, cron worktree 점검 (FLR-20260615-FLR-001)"
    )
    _notify("100m1s PM320 입력손상", notif)


def _fmt_amount(amount):
    """거래대금 → 억 단위 문자열."""
    if not amount:
        return "?"
    return f"{amount / 1e8:,.0f}억"


def main(argv=None):
    parser = argparse.ArgumentParser(description="PM320 GAP75 선정")
    parser.add_argument("--date", help="YYYY-MM-DD (미지정 시 오늘 KST)")
    parser.add_argument(
        "--dry-run", action="store_true", help="JSON 미저장, 콘솔 출력만"
    )
    args = parser.parse_args(argv)

    date_str = args.date or datetime.now(KST).strftime("%Y-%m-%d")
    kiwoom_path = os.path.join(KIWOOM_DIR, f"{date_str}.json")
    if not os.path.exists(kiwoom_path):
        print(f"데이터 부족 — kiwoom 파일 부재: {kiwoom_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        bars, idx = load_dailybars(conn)
        ta = load_trade_amounts(conn)
    finally:
        conn.close()

    try:
        result = select(date_str, bars, idx, ta, kiwoom_path)
    except KiwoomInputError as exc:
        # Finding A: malformed/conflict-marker 입력 → 파일 부재와 동일 exit 2 통일
        # (uncaught JSONDecodeError exit 1 traceback 회피) + stale 동격 비상 알람.
        _emit_parse_alarm(date_str, str(exc))
        return 2

    # ── 입력 신선도 게이트 (FLR-20260615-FLR-001) — stale 시 경고+알람, 픽은 진행 ──
    freshness = result.get("input_freshness") or {}
    if freshness.get("is_stale"):
        _emit_stale_alarm(date_str, freshness)

    # ── 콘솔 출력 ────────────────────────────────────────────────────────
    gr = result["gap_ratio"]
    gr_s = f"{gr:.3f}" if gr is not None else "inf"
    print(f"[PM320 통합 선정] {date_str}")
    if freshness.get("is_stale"):
        print(f"  ⚠️ 입력 STALE: {freshness.get('reason')}")
    print(
        f"  소스 종목 {result['source_count']}개 → 거래대금≥{_fmt_amount(MIN_TRADE_AMOUNT)} 게이트 "
        f"통과 {result['gated_in_count']}개 (탈락 {result['gated_out_count']}개) → top-5 "
        f"(새T1={_fmt_amount(result['T1'])}, "
        f"새T2={_fmt_amount(result['T2'])}, 새T1/T2={gr_s})"
    )
    print(f"  분기 {result['branch']}: {result['branch_reason']}")
    if result.get("removed"):
        print("  [5종 OR 선제거 (상한가/하한가/강세/우선주/거래대금터진음봉)]")
        for rm in result["removed"]:
            print(
                f"    - {rm['name']}({rm['code']}) 원{rm['pre_rank']}위 "
                f"{_fmt_amount(rm['trade_amount'])} — {rm['reason']}"
            )
    print("  [top-5 원순위 raw]")
    for row in result["ranked"]:
        bc = row["bullish_count"]
        bc_s = f"강세{bc}" if bc is not None else "강세?"
        chg = row["change_pct"]
        chg_s = f"{chg:+.1f}%" if chg is not None else "?"
        print(
            f"    {row['rank']}위 {row['name']}({row['code']}) "
            f"{_fmt_amount(row['trade_amount'])} {chg_s} {bc_s}"
        )
    if result["picked"]:
        p = result["picked"]
        chg = p.get("change_pct")
        chg_s = f"{chg:+.1f}%" if chg is not None else "?"
        print(
            f"  → 최종 선정 (분기 {result['branch']}): "
            f"{p['name']}({p['code']}) {_fmt_amount(p['trade_amount'])} {chg_s}"
        )
    else:
        print(f"  → 보류 (미거래): {result['branch_reason']}")

    # ── JSON 저장 ────────────────────────────────────────────────────────
    if args.dry_run:
        print("  (dry-run: JSON 미저장)")
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{date_str}.json")
    payload = dict(result)
    payload["generated_at"] = datetime.now(KST).isoformat()
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"  결과 저장: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
