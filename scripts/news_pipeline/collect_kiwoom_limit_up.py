"""키움 ka10017 상한가 종목 수집 — REQ-080 신규 모듈.

⚠️ DEPRECATED (D축, 대표 확정 2026-06-16) — 상한가 SoT 폐기.
================================================================
상한가 판정이 ka10017 별도조회 → v1 조건검색 결과목록 등락률(>= 29.79%,
build_daily.LIMIT_UP_THRESHOLD) 로 통일됐다. v1 조건검색(거래대금 500억 ∪ 당일
상한가)이 상한가 종목을 결과목록에 이미 포함하고 각 항목에 등락률을 제공하므로
ka10017 별도조회·stock_status_badges SoT 머지가 불요하다 (build_daily.py 상한가
판정부 + build_theme_stats.build_limit_up_trend_json 참조).

본 모듈은 직접 실행 차단된다 (__main__ guard — 키움 API 콜 발생 전 봉쇄). import
대상은 어디에도 없다(검증 2026-06-16). cutover(cron stage 제거)는 별도 운영 전환
절차 — 본 코드는 보존(롤백·과거 backfill_limit_up 계열 참조용)하되 신규 호출 0.
환경변수 M1S_ALLOW_LEGACY_LIMIT_UP=1 명시 시에만 legacy 동작(전환기 안전장치).
================================================================

REQ: 상한가 종목을 키움 자체 API(ka10017)로 직접 수집하여 stock_status_badges에
영속화. 기존 build_daily.py status_badges는 공시 기반(투자주의/경고/위험/단기과열)만
처리 — 상한가는 SoT 부재 상태였음.

TR ID: ka10017 (상하한가요청)
엔드포인트: POST /api/dostk/stkinfo
응답 키: updown_pric (list)
응답 필드 (REQ-080 검증, 2026-04-29):
  stk_cd, stk_nm, cur_prc(부호+), pred_pre, flu_rt, trde_qty, pred_trde_qty,
  sel_req, sel_bid, buy_bid, buy_req, cnt(연속횟수), stk_infr, pred_pre_sig

요청 파라미터:
  mrkt_tp:      000=전체, 001=KOSPI, 101=KOSDAQ
  updown_tp:    1=상한, 2=상승, 3=보합, 4=하락, 5=하한
  sort_tp:      1=종목코드, 2=연속횟수, 3=등락률, 4=거래량
  stk_cnd:      0=전체, 1=관리종목제외, 3=우선주제외, 4=관리종목제외+우선주제외
                (Q-20260519-CYCLE12-002, 2026-05-19): "1"=관리종목제외 채택 — 우선주
                LU(006345 대원전선우·012205 계양전기우 등) catch. 대표 LU 본질
                "상한가 자체" 정합 + WebSearch §11.15 spec corroborating PASS.
  trde_qty_tp:  00100=10만주이상 등 (00000=전체)
  crd_cnd:      0=전체
  trde_gold_tp: 0=전체
  stex_tp:      1=KRX

거래대금(trde_prica) source 정책 (Q-20260519-CYCLE11-001, 2026-05-19):
  본래 규칙 = "상한가목록 = ohlc + 거래대금 함께 조회". ka10017 응답에는 trde_prica
  필드가 없으므로 LU catch 후 종목별 ka10081 추가 호출 → 오늘 일봉 row의
  trde_prica (백만원 단위) × 1_000_000 → 원 단위 환산하여 payload.trde_prica_calc
  에 직접 적재. ka10081 응답 dt=오늘 row 부재 시 fallback (trde_qty × cur_prc 단순곱)
  으로 회귀 (호환성 보존). payload에 trde_prica_source 명시하여 audit trail 유지.

  근거: ka10017 trde_qty × cur_prc는 평균체결단가 ≠ cur_prc인 경우 누적
  거래대금과 불일치. ka10081 trde_prica는 KRX 정식 누적 거래대금 (원본).
  본래 규칙 정합 + Fix D 동형 (dailybars SoT 우선) — rules/data-continuity.md
  § "상한가 source 정합화" 참조.

환경 분기 (KIWOOM_ENV):
  - 'live' (기본, 권고): KIWOOM_LIVE_APPKEY/SECRETKEY/BASE_URL 사용.
    실전키는 만료일 2027-04-01로 모의(2026-07-05) 대비 여유 큼.
  - 'mock': KIWOOM_APPKEY/SECRETKEY/BASE_URL 사용.

사용:
  python3 -m scripts.news_pipeline.collect_kiwoom_limit_up
  KIWOOM_ENV=mock python3 -m scripts.news_pipeline.collect_kiwoom_limit_up

cron 슬롯 (com.100m1s.kiwoom-limit-up.plist):
  평일 09:05 / 12:00 / 15:35 KST.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# cycle25 env-unification (2026-05-28): 메인 .env 단일 source 일원화.
# 우선순위: shell-level export (kiwoom_cron.sh) > MAIN_ENV setdefault > POC_ENV fallback (deprecated).
# S5 자립화 (DOC-20260707-REQ-001): env(M1S_COMPANY) 우선 + pm320 레포 로컬 fallback(parents[2]).
_M1S_COMPANY = Path(
    os.environ.get("M1S_COMPANY", str(Path(__file__).resolve().parents[2]))
)
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

# 패키지 import 경로 보정 (단독 실행 시)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.news_pipeline.db import connect, init_schema  # noqa: E402
from scripts.news_pipeline.ka10081_helper import find_today_trade_amount  # noqa: E402
from scripts.news_pipeline.kiwoom_client import get_token  # noqa: E402,F401

# 환경 분기 — 기본 'live' (실전키 우선; 모의 만료 D-67 임박)
KIWOOM_ENV = os.environ.get("KIWOOM_ENV", "live").lower()
if KIWOOM_ENV == "mock":
    KIWOOM_BASE = os.environ.get("KIWOOM_BASE_URL", "https://mockapi.kiwoom.com")
    KIWOOM_APPKEY = os.environ.get("KIWOOM_APPKEY")
    KIWOOM_SECRETKEY = os.environ.get("KIWOOM_SECRETKEY")
else:
    KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL", "https://api.kiwoom.com")
    KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get(
        "KIWOOM_APPKEY"
    )
    KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
        "KIWOOM_SECRETKEY"
    )

BADGE_TYPE_LIMIT_UP = "상한가"
SOURCE_KIWOOM = "kiwoom_ka10017"

# Q-20260515-058 sanity check (cycle5, FLR-20260428-TEC-001 / FLR-20260514-* 정합):
#   ka10017 flu_rt 자체는 키움 권리락 보정 후 값으로 신뢰 가능 (예: 290690 5/15 flu_rt=29.84%).
#   다만 dailybars row 부재 (거래정지·신규상장)로 후속 LU union/build_daily가 LAG(close) raw
#   차감을 사용할 경우 raw chg가 +68.79% 등 가짜 산출 위험 → SoT 적재 단계에서 차단.
#
#   판정 룰:
#     (a) flu_rt < FLU_RT_MIN → ka10017 응답 자체 비정상, skip (return False)
#     (b) flu_rt >= FLU_RT_MIN + dailybars 기준 adj_chg 산출 가능 + adj_chg <= CHG_MAX → 정상 (return True)
#     (c) flu_rt >= FLU_RT_MIN + dailybars row 부재 (prev_close 부재) → flu_rt 단독 신뢰 OK (return True)
#     (d) flu_rt >= FLU_RT_MIN + dailybars 기준 adj_chg > CHG_MAX + flu_rt 가 정상 상한가
#         범위 밖 → 진짜 stale row 의심, skip + 알람
#     (e) flu_rt >= FLU_RT_MIN + dailybars 기준 adj_chg > CHG_MAX 이지만 flu_rt 가 정상
#         상한가 범위(FLU_RT_MIN ~ CHG_MAX) 안 → 거래정지-해제 갭으로 인한 stale prev_close
#         판명. 키움 flu_rt 는 권리락·당일 등락 보정 후 SoT 값이므로 신뢰하여 구제 (return True).
#         예: 027040 (서울전자통신) 5/20~5/27 거래정지 → 5/28 재개 + 상한가(+30%) 안착.
#             dailybars 5/19 close=480 → 5/28 close=3120 (정지 기간 row 부재) →
#             LAG(close)=480 → adj_chg=+550% (가짜). flu_rt=30.0 (정확) → 구제.
#         (d)와 (e) 분리: 단순 cap 제거가 아니라 flu_rt 교차검증 통과 케이스만 좁게 구제.
#         FLR-20260428-TEC-001 (카드↔추이 한쪽 누락) 정합 — build_theme_stats.py 도 동일 구제.
FLU_RT_MIN = 28.0  # 상한가 +30% buffer (시장 직전 fill 또는 spec drift 회피)
CHG_MAX = 31.5  # 한국 일일 상하한가 ±30% + 1.5% buffer


def _get_token() -> str:
    # SSOT: kiwoom_client.get_token 위임 (env={KIWOOM_ENV} 문구 보존, 본문 byte-identical).
    return get_token(
        KIWOOM_BASE,
        KIWOOM_APPKEY,
        KIWOOM_SECRETKEY,
        env_note=f"env={KIWOOM_ENV}",
    )


def _parse_int(val) -> int | None:
    """키움 응답 숫자 파싱 (부호·콤마·패딩 제거). +3925 → 3925."""
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


def _parse_float(val) -> float | None:
    if val is None:
        return None
    s = str(val).replace(",", "").replace("+", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_dailybars_trade_amount(token: str, code: str) -> int | None:
    """LU 종목별 ka10081 호출 → 오늘 row의 trde_prica (원 단위) 반환.

    본래 규칙(상한가목록 = ohlc + 거래대금)을 ka10081로 정합화.
    ka10017 응답에는 trde_prica 필드 없음 → 본 함수가 LU catch 후 추가 호출.

    Args:
        token: 키움 OAuth 토큰.
        code: 6자리 종목코드.

    Returns:
        trade_amount (원 단위, ka10081 trde_prica × 1_000_000). 실패 시 None.
        오늘(KST) 일봉 row가 응답에 없으면 None (장 직전 등).

    호출 빈도 분석:
        LU cron 평일 3회 (09:05/12:00/15:35), 응답 ~5~30종목.
        0.3s sleep × 30 = 최대 9s 추가 latency. ka10081 rate limit 안전 마진.
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10081",
    }
    today_raw = datetime.now().strftime("%Y%m%d")
    body = {"stk_cd": f"A{code}", "base_dt": today_raw, "upd_stkpc_tp": "1"}
    for attempt in range(4):
        try:
            r = requests.post(
                f"{KIWOOM_BASE}/api/dostk/chart",
                json=body,
                headers=headers,
                timeout=20,
            )
        except Exception as e:
            print(f"  [ka10081 LU {code}] exception: {e}")
            return None
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"  [ka10081 LU {code}] 429, backoff {back}s")
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(f"  [ka10081 LU {code}] http {r.status_code}: {r.text[:200]}")
            return None
        try:
            data = r.json()
        except Exception:
            return None
        if data.get("return_code") == 5:
            back = 2 ** (attempt + 1)
            print(f"  [ka10081 LU {code}] rc=5, backoff {back}s")
            time.sleep(back)
            continue
        if data.get("return_code") != 0:
            return None
        rows = data.get("stk_dt_pole_chart_qry") or []
        if not isinstance(rows, list):
            return None
        # 오늘 row 검색 → ka10081_helper.find_today_trade_amount 위임 (SSOT 통합).
        # Q-20260519-CYCLE19-001 (2026-05-19): cycle12 Fix-3 + 본 LU helper 파싱이
        # verbatim 복제 risk → 단일 출처 통합. 결측 시 None 반환 (caller fallback 유도).
        return find_today_trade_amount(rows, today_raw)
    return None


def fetch_limit_up(token: str, mrkt_tp: str = "000") -> list[dict]:
    """ka10017 호출. updown_tp=1 (상한가만). stk_cnd=1 (관리종목제외, 우선주는 포함).

    Q-20260519-CYCLE12-002 (2026-05-19): stk_cnd "4"→"1" 변경.
    우선주(006345 대원전선우 / 012205 계양전기우 등) LU recurring 5건 catch 실패
    본질 — "4"(관리+우선주 제외)에 의한 우선주 명시적 제외. 대표 LU 본질
    "상한가 자체" 정합 + WebSearch §11.15 spec corroborating PASS.
    sanity check (flu_rt ≥ FLU_RT_MIN) 봉쇄 마진으로 안전성 보존.
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10017",
    }
    body = {
        "mrkt_tp": mrkt_tp,
        "updown_tp": "1",
        "sort_tp": "1",
        "stk_cnd": "1",
        "trde_qty_tp": "00000",  # 전체 (필터링은 application 단)
        "crd_cnd": "0",
        "trde_gold_tp": "0",
        "stex_tp": "1",
    }
    for attempt in range(4):
        try:
            r = requests.post(
                f"{KIWOOM_BASE}/api/dostk/stkinfo",
                json=body,
                headers=headers,
                timeout=20,
            )
        except Exception as e:
            print(f"[ka10017 {mrkt_tp}] exception: {e}")
            return []
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"[ka10017 {mrkt_tp}] 429, backoff {back}s")
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(f"[ka10017 {mrkt_tp}] http {r.status_code}: {r.text[:200]}")
            return []
        try:
            data = r.json()
        except Exception:
            return []
        rc = data.get("return_code")
        if rc == 5:
            back = 2 ** (attempt + 1)
            print(f"[ka10017 {mrkt_tp}] rc=5, backoff {back}s")
            time.sleep(back)
            continue
        if rc != 0:
            print(f"[ka10017 {mrkt_tp}] rc={rc} msg={data.get('return_msg')}")
            return []
        rows = data.get("updown_pric") or []
        return rows if isinstance(rows, list) else []
    return []


def _row_to_payload(raw: dict, ka10081_trade_amount: int | None = None) -> dict:
    """ka10017 row → payload dict.

    trde_prica_calc source 우선순위 (Q-20260519-CYCLE11-001, 2026-05-19):
      1. ka10081 trde_prica (KRX 정식 누적 거래대금) — 본래 규칙 정합 1순위
      2. trde_qty × cur_prc 단순곱 (fallback, ka10081 호출 실패 또는 오늘 row 부재 시)

    payload schema 보존: trde_prica_calc 필드명 그대로 유지 (downstream
    build_daily.py L2746/2751/2775 SQL 정합). trde_prica_source 신규 필드로
    source 추적 (kiwoom_ka10081 / calc_fallback).

    Args:
        raw: ka10017 응답 row.
        ka10081_trade_amount: LU catch 후 추가 ka10081 호출로 얻은 원 단위 거래대금.
                              None이면 fallback 계산값 사용.
    """
    cur_prc = _parse_int(raw.get("cur_prc"))
    trde_qty = _parse_int(raw.get("trde_qty"))
    calc_fallback = (
        cur_prc * trde_qty if (cur_prc is not None and trde_qty is not None) else None
    )
    if ka10081_trade_amount is not None:
        trde_prica_calc = ka10081_trade_amount
        trde_prica_source = "kiwoom_ka10081"
    else:
        trde_prica_calc = calc_fallback
        trde_prica_source = "calc_fallback"
    return {
        "stk_nm": raw.get("stk_nm") or "",
        "cur_prc": cur_prc,
        "flu_rt": _parse_float(raw.get("flu_rt")),
        "pred_pre": _parse_int(raw.get("pred_pre")),
        "trde_qty": trde_qty,
        "trde_prica_calc": trde_prica_calc,
        "trde_prica_source": trde_prica_source,
        "trde_prica_fallback_calc": calc_fallback,  # audit (source=kiwoom_ka10081 일 때도 비교용 보존)
        "buy_req": _parse_int(raw.get("buy_req")),
        "sel_req": _parse_int(raw.get("sel_req")),
        "cnt": _parse_int(raw.get("cnt")),  # 연속 상한가 횟수
    }


def _sanity_check_row(
    conn,
    date_str: str,
    code: str,
    flu_rt: float | None,
    cur_prc: int | None = None,
) -> tuple[bool, str]:
    """ka10017 row sanity check — stock_status_badges INSERT 전 봉쇄.

    build_theme_stats.py L766~803 adj_ratio cascade 동형 SQL로 dailybars 기준
    adj_chg_high_pct 산출 → flu_rt 와 cross-check.

    Q-CYCLE22-LU-POST-CLOSE-GUARD (2026-05-20, P0):
        장 마감 후 (15:30+) sanity check 시점, dailybars.close 가용 +
        close < cur_prc 1% 이상 mismatch ⇒ 장중 상한가 catch 후 마감 전 풀린
        패턴 (142280 5/20: dailybars.close=6230 vs ka10017 cur_prc=6310,
        diff=1.27%). 본 종목 skip → upsert_badges 의 response_codes set에
        포함되지 않음 → 풀림 처리 (active_until=now UPDATE) 자동 trigger.
        이로써 가짜 상한가 라이브 노출 봉쇄. cur_prc 미제공 시 (호환 호출)
        본 단계 skip + 기존 adj_chg 가드만 수행 (회귀 안전).

    Returns:
        (ok, reason). ok=True면 부착 진행, False면 skip + reason 알람.
    """
    if flu_rt is None:
        return False, "flu_rt=None"
    if flu_rt < FLU_RT_MIN:
        return False, f"flu_rt={flu_rt} < {FLU_RT_MIN} (응답 비정상)"

    # dailybars 기준 adj_chg 산출 (build_theme_stats.py L774~803 동형)
    # dailybars cross-check 는 "장중 풀림"(142280 동형) 보강일 뿐 부재 시 비적용이
    # 이미 명시된 설계 의도(아래 row is None 분기). 테이블 부재는 row 부재의 상위
    # 케이스이므로 동일 처리. (FLR-20260616-TEC-001 root cause — cron DB dailybars 부재,
    # commit a52cf55d 재이관: S2 pm320 repo migration 시 유실 방지)
    # 지역 import: module-level import 는 autoflake 계열 formatter 가 except 절 참조를
    # 놓쳐 미사용으로 오판·제거하는 사례 실측 (DOC-20260524-FLR-001 동형). 함수 지역에
    # 두어 formatter 가 사용을 인식하게 한다.
    import sqlite3

    try:
        row = conn.execute(
            """WITH chain AS (
                 SELECT close,
                        LAG(close) OVER (ORDER BY date) AS prev_close,
                        LAG(date)  OVER (ORDER BY date) AS prev_date,
                        date
                   FROM dailybars
                  WHERE code = ?
                    AND date <= ?
                    AND close IS NOT NULL AND close > 0
                  ORDER BY date DESC
                  LIMIT 2
               )
               SELECT close, prev_close, prev_date, date
                 FROM chain
                WHERE date = (SELECT MAX(date) FROM chain)""",
            (code, date_str),
        ).fetchone()
    except sqlite3.OperationalError as e:
        # dailybars 테이블 부재 (서빙 DB) → row 부재와 동일 graceful 처리.
        return True, f"dailybars 테이블 부재 (flu_rt 단독 신뢰): {e}"

    if row is None or row["close"] is None:
        # dailybars row 자체 부재 (장 시작 직후 cron 적재 전 등) → flu_rt 단독 신뢰
        return True, "dailybars row 부재 (flu_rt 단독 신뢰)"

    # ─── Q-CYCLE22-LU-POST-CLOSE-GUARD (P0, 본질 fix) ──────────────
    # 장 마감 후 (15:30+) + dailybars.close 가용 + close < cur_prc 1%+ mismatch
    # ⇒ 장중 상한가 catch 후 마감 전 풀린 패턴 → skip → retire trigger
    # 142280 5/20 catch path: dailybars.close=6230 vs cur_prc=6310 (1.27%).
    # 마감 전 (장중) cron 사이클에는 dailybars 미반영 가능 → 본 guard 미적용.
    if cur_prc is not None and cur_prc > 0 and row["date"] == date_str and row["close"]:
        _now = datetime.now()
        _is_post_close = _now.hour > 15 or (_now.hour == 15 and _now.minute >= 30)
        if _is_post_close:
            _db_close = int(row["close"])
            _diff_pct = abs(cur_prc - _db_close) * 100.0 / cur_prc
            if _diff_pct >= 1.0 and _db_close < cur_prc:
                return (
                    False,
                    f"post-close mismatch: dailybars.close={_db_close} < "
                    f"cur_prc={cur_prc} (diff={_diff_pct:.2f}%) "
                    f"— 장중 상한가 풀림 패턴, 142280 5/20 동형",
                )
    # ─── /Q-CYCLE22-LU-POST-CLOSE-GUARD ───

    prev_close = row["prev_close"]
    prev_date = row["prev_date"]
    if prev_close is None or prev_close <= 0:
        return True, "prev_close 부재 (신규상장·flu_rt 단독 신뢰)"

    # 권리락 ratio cascade (build_theme_stats.py L782~793 동형)
    adj_row = conn.execute(
        """SELECT ratio FROM dailybars_adjustments
            WHERE code = ?
              AND date > ?
              AND date <= ?
            ORDER BY date DESC LIMIT 1""",
        (code, prev_date, row["date"]),
    ).fetchone()
    adj_ratio = float(adj_row["ratio"]) if adj_row else 1.0

    adjusted_prev = prev_close * adj_ratio
    if adjusted_prev <= 0:
        return True, "adjusted_prev<=0 (flu_rt 단독 신뢰)"
    adj_chg = (row["close"] - adjusted_prev) * 100.0 / adjusted_prev

    if adj_chg > CHG_MAX:
        # 5/14 row 부재로 LAG가 5/13 close를 prev로 사용 → raw +68.79% 가짜 산출
        # ka10017 flu_rt 는 정상이지만 dailybars 인입이 stale → 기본은 부착 skip.
        #
        # Q-CYCLE23-LU-SUSPENSION-LIFT-GUARD (P0, 본질 fix, 2026-05-28):
        #   거래정지-해제 후 상한가 안착 케이스 구제. dailybars adj_chg 가 CHG_MAX 초과
        #   (stale prev_close)인데 키움 flu_rt 가 정상 상한가 범위(FLU_RT_MIN ~ CHG_MAX)
        #   안이면 → 정지 기간 dailybars row 부재로 LAG(close)가 정지 전 종가를 prev로
        #   잡은 가짜 adj_chg 로 판명. flu_rt 는 권리락·당일 등락 보정 후 SoT 값이므로
        #   신뢰하여 상한가로 구제 (return True). 단순 cap 제거가 아닌 flu_rt 교차검증
        #   통과 케이스만 좁게 구제 — 진짜 stale(flu_rt 도 비정상)은 여전히 아래로 skip.
        #   027040 5/28: adj_chg=550% (가짜) + flu_rt=30.0 (정상) → 구제.
        if FLU_RT_MIN <= flu_rt <= CHG_MAX:
            return (
                True,
                f"adj_chg={adj_chg:.2f}% > {CHG_MAX}% but flu_rt={flu_rt} "
                f"정상 상한가 범위 → 거래정지-해제 stale prev 구제 "
                f"(prev_date={prev_date}, flu_rt fallback)",
            )
        return (
            False,
            f"adj_chg={adj_chg:.2f}% > {CHG_MAX}% "
            f"(prev_date={prev_date} stale 의심, flu_rt={flu_rt} 비정상 범위)",
        )
    return True, f"adj_chg={adj_chg:.2f}% OK"


def upsert_badges(
    conn,
    date_str: str,
    rows: list[dict],
    allow_retire: bool = True,
    token: str | None = None,
) -> tuple[int, int, int, int, int]:
    """stock_status_badges에 상한가 배지 interval 모델 UPSERT (REQ-080 v2).

    interval 정책 (2026-04-29 전환, 대표 결정):
      1. 응답 종목 중 현재 활성 없음 → 새 row INSERT (active_until=NULL)
      2. 응답 종목 중 현재 활성 있음 → payload_json만 갱신 (created_at 유지)
      3. 응답에 없는 현재 활성 (kiwoom_ka10017) → active_until=now UPDATE (풀림)
      4. pipeline_chg는 영구 활성 (본 함수가 건드리지 않음 — kiwoom_ka10017만 처리)

    가드 (FLR-20260428-TEC-001 재발 방지):
      - 빈 응답이면 retire 금지 (장 시작 직후 / 일시 API 오류 방어)
      - allow_retire=False → 풀림 처리 skip

    sanity check (Q-20260515-058 cycle5, 2026-05-15):
      - INSERT/UPDATE 직전 _sanity_check_row()로 dailybars 기준 cross-check
      - flu_rt < FLU_RT_MIN 또는 dailybars adj_chg > CHG_MAX 시 skip + 알람
      - 290690 (5/14 거래정지·5/15 재개) 류 stale row 인입 봉쇄

    trade_amount source 정합 (Q-20260519-CYCLE11-001, 2026-05-19):
      - sanity gate 통과 종목별 ka10081 추가 호출 → 오늘 row의 trde_prica (백만원)
        × 1_000_000 → payload.trde_prica_calc 직접 적재 (본래 규칙 정합 1순위)
      - ka10081 호출 실패 또는 오늘 row 부재 시 fallback (trde_qty × cur_prc)
      - payload.trde_prica_source 필드로 source 추적 (kiwoom_ka10081 / calc_fallback)

    UNIQUE: (date, stock_code, badge_type, source, active_from).
      active_from에 마이크로초 포함 → race condition 회피.

    Args:
        conn: sqlite3 connection.
        date_str: 적재 기준일 (YYYY-MM-DD).
        rows: ka10017 응답 row 리스트.
        allow_retire: 응답에 없는 종목 풀림 처리 허용 여부. 기본 True.
        token: 키움 OAuth 토큰 (LU 종목별 ka10081 추가 호출에 사용).
               None이면 ka10081 호출 skip + 모든 종목 fallback (회귀 안전).

    Returns:
        (inserted, updated, retired, skipped_sanity, ka10081_fetched)
        ka10081_fetched: ka10081 호출 성공으로 trade_amount 정합화한 종목 수.
    """
    inserted = 0
    updated = 0
    retired = 0
    skipped_sanity = 0
    ka10081_fetched = 0
    # 마이크로초 포함 → UNIQUE race 회피 (비판 2 반영)
    now_ts = datetime.now().isoformat(timespec="microseconds")

    # 1. 응답 코드 set — sanity gate 통과 시에만 응답 코드로 인정
    #    (skip된 종목은 retire 처리 대상에서도 제외 — 기존 활성 row 변동 없음)
    response_codes: set[str] = set()
    valid_rows: list[dict] = []
    skip_reasons: list[tuple[str, str]] = []
    for raw in rows:
        code = (raw.get("stk_cd") or "").strip()
        if not (code and len(code) == 6):
            continue
        flu_rt = _parse_float(raw.get("flu_rt"))
        # Q-CYCLE22-LU-POST-CLOSE-GUARD: cur_prc 전달하여 마감 후 dailybars
        # mismatch 사전 봉쇄 (142280 5/20 동형). 미제공 시 회귀 안전 (가드 skip).
        _cur_prc_sanity = _parse_int(raw.get("cur_prc"))
        ok, reason = _sanity_check_row(
            conn, date_str, code, flu_rt, cur_prc=_cur_prc_sanity
        )
        if not ok:
            skipped_sanity += 1
            skip_reasons.append((code, reason))
            print(
                f"[ka10017 sanity] SKIP code={code} name={raw.get('stk_nm') or ''} "
                f"flu_rt={flu_rt} reason={reason}"
            )
            continue
        response_codes.add(code)
        valid_rows.append(raw)

    # 2. 풀림 처리 — 응답에 없는 현재 활성 kiwoom_ka10017 → active_until=now_ts
    #    가드: 빈 응답이면 retire 금지
    if allow_retire and valid_rows:
        placeholders = ",".join("?" * len(response_codes))
        sql = f"""UPDATE stock_status_badges
                     SET active_until = ?
                   WHERE date = ?
                     AND badge_type = ?
                     AND source = ?
                     AND active_until IS NULL
                     AND stock_code NOT IN ({placeholders})"""
        params = (
            now_ts,
            date_str,
            BADGE_TYPE_LIMIT_UP,
            SOURCE_KIWOOM,
            *response_codes,
        )
        cur = conn.execute(sql, params)
        retired = cur.rowcount or 0

    # 3. 응답 종목 INSERT 또는 payload UPDATE
    #    Q-20260519-CYCLE11-001: LU sanity-gate 통과 종목별 ka10081 추가 호출 →
    #    오늘 row의 trde_prica (백만원 × 1_000_000) 직접 적재 (본래 규칙 정합 1순위).
    for idx_lu, raw in enumerate(valid_rows):
        code = raw["stk_cd"].strip()
        ka10081_amount: int | None = None
        if token is not None:
            ka10081_amount = fetch_dailybars_trade_amount(token, code)
            if ka10081_amount is not None:
                ka10081_fetched += 1
            # ka10081 호출 간 0.3s sleep (5req/s 안전 마진 — ka10081 동형)
            if idx_lu < len(valid_rows) - 1:
                time.sleep(0.3)
        payload = _row_to_payload(raw, ka10081_trade_amount=ka10081_amount)
        payload_json = json.dumps(payload, ensure_ascii=False)

        existing = conn.execute(
            """SELECT id FROM stock_status_badges
                WHERE date = ?
                  AND stock_code = ?
                  AND badge_type = ?
                  AND source = ?
                  AND active_until IS NULL""",
            (date_str, code, BADGE_TYPE_LIMIT_UP, SOURCE_KIWOOM),
        ).fetchone()

        if existing:
            # 비판 3 반영 — created_at 유지, payload_json만 갱신
            conn.execute(
                """UPDATE stock_status_badges
                       SET payload_json = ?
                     WHERE id = ?""",
                (payload_json, existing["id"]),
            )
            updated += 1
        else:
            # 신규 진입 또는 재진입 (이전 row는 active_until 있음)
            conn.execute(
                """INSERT INTO stock_status_badges
                       (date, stock_code, badge_type, source, payload_json,
                        active_from, active_until, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    date_str,
                    code,
                    BADGE_TYPE_LIMIT_UP,
                    SOURCE_KIWOOM,
                    payload_json,
                    now_ts,
                    now_ts,
                ),
            )
            inserted += 1

    conn.commit()
    return inserted, updated, retired, skipped_sanity, ka10081_fetched


def collect(date_str: str | None = None) -> dict:
    """ka10017 1회 호출 → stock_status_badges 적재.

    Args:
        date_str: 적재 기준일 (YYYY-MM-DD). None=오늘.
    """
    init_schema()  # stock_status_badges 보장
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    token = _get_token()
    rows = fetch_limit_up(token)
    summary: dict = {
        "env": KIWOOM_ENV,
        "base": KIWOOM_BASE,
        "date": date_str,
        "raw_count": len(rows),
        "inserted": 0,
        "updated": 0,
        "retired": 0,
        "skipped_sanity": 0,
        "ka10081_fetched": 0,
        "samples": [],
    }
    with connect() as conn:
        ins, upd, ret, skp, ka_fetched = upsert_badges(
            conn, date_str, rows, token=token
        )
        summary["inserted"] = ins
        summary["updated"] = upd
        summary["retired"] = ret
        summary["skipped_sanity"] = skp
        summary["ka10081_fetched"] = ka_fetched
    summary["samples"] = [
        {
            "code": (r.get("stk_cd") or "").strip(),
            "name": r.get("stk_nm") or "",
            "flu_rt": _parse_float(r.get("flu_rt")),
            "cur_prc": _parse_int(r.get("cur_prc")),
        }
        for r in rows[:5]
    ]
    return summary


def main():
    # ⚠️ DEPRECATED guard (D축, 2026-06-16) — 상한가 SoT 폐기.
    # 상한가 판정은 v1 조건검색 등락률(>= 29.79%)로 통일됨 (build_daily). 본 모듈의
    # ka10017 별도조회는 불요 → 키움 API 콜 발생 전 차단. 전환기 롤백·재현용으로
    # M1S_ALLOW_LEGACY_LIMIT_UP=1 명시 시에만 legacy 동작.
    if os.environ.get("M1S_ALLOW_LEGACY_LIMIT_UP") != "1":
        print(
            "[collect_kiwoom_limit_up] DEPRECATED (D축 2026-06-16) — 상한가 SoT는 "
            "v1 조건검색 등락률(>= 29.79%)로 통일됨. 본 모듈은 비활성. "
            "강제 실행은 M1S_ALLOW_LEGACY_LIMIT_UP=1.",
            file=sys.stderr,
        )
        return
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--date",
        type=str,
        default=None,
        help="기준일 YYYY-MM-DD (기본: 오늘)",
    )
    args = ap.parse_args()
    result = collect(date_str=args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
