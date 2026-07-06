"""
거래대금 리스트 전량 + 폴백 체인 → stock-YYYY-MM-DD.json 생성.
프론트(/news)가 로드하는 최종 산출물.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .config import HOMEPAGE
from .db import connect

OUT_DIR = HOMEPAGE / "data" / "interpreted"
CAL_INDEX = HOMEPAGE / "data" / "calendar" / "index.json"

# DSN-004 v9.1 §I.2 — KOREA_HOLIDAYS 1차 소스 (togusa 산출 commit 08dc440).
# main 레포 root 기준 상대경로. Path 비교 위해 import 시점 lazy 평가.
from pathlib import Path as _Path  # noqa: E402

_REPO_ROOT = _Path(__file__).resolve().parents[2]
KOREA_HOLIDAYS_PATH = (
    _REPO_ROOT / "rules" / "_whitelist" / "korea-trading-holidays.json"
)

# togusa estimated hit 콘솔 경고 dedup (process 1회만).
_ESTIMATED_WARN_LOGGED: bool = False

_WEEKDAYS_KR = "월화수목금토일"

# 상한가 판정 임계 (D축, 대표 3회 확정 2026-06-16).
# 상한가 = v1 조건검색 결과목록 등락률(change_pct) >= 29.79%.
# 한국 상하한가 ±30% 제도상 호가 단위 보정 후 실측 상한가 등락률은 29.79~30.00%.
# build_theme_stats.build_limit_up_trend_json LIMIT_UP_THRESHOLD 와 동일 값 (소스 정합 의무).
LIMIT_UP_THRESHOLD = 29.79


class _HeroshikSkipUnion(Exception):
    """HEROSHIK STRICT OVERRIDE 활성 시 union 코드를 skip하기 위한 sentinel.

    대표 결정 P0 (2026-05-08 16:17 KST): 5/6 단일 일자 영웅식 strict 적용 시
    condition_500eok* / pipeline_chg / kiwoom_ka10017 union + 상한가 LU union 모두 skip.
    KOSPI100 mega-cap 재포함 방지 (mission Step 5 정합).
    """

    pass


def _fmt_ko_date(s: str | None) -> str:
    """YYYY-MM-DD → '2026-04-15(수)'. 파싱 실패 시 원문 반환."""
    if not s:
        return ""
    try:
        from datetime import date as _date

        y, m, d = map(int, s.split("-")[:3])
        return f"{y:04d}-{m:02d}-{d:02d}({_WEEKDAYS_KR[_date(y, m, d).weekday()]})"
    except Exception:
        return s or ""


def _build_credit_reason(
    disclosures: list[dict],
    today: str,
    code: str | None = None,
    stock_status: dict | None = None,
) -> str:
    """신용불가 사유를 날짜+요일 포함 문장으로 생성.

    우선순위:
      0) kt20017 회사한도초과 (당일 기준) → "회사한도초과 (당일 기준)"
      1) 매매거래정지 / 거래소조치
      2) 투자위험 > 투자경고 > 투자주의 (지정 기간 / 당일)
      3) 단기과열 지정
      4) 관리종목 / 상장폐지
      5) 폴백: 공시 없음 → "신용가능 목록 미포함 (사유 미공개)"

    stock_status: {code: (limit_exceeded:int, raw_status:str)} — 메모리 캐시.
    """
    # 0순위: kt20017 회사한도초과 (당일 기준)
    if code and stock_status:
        row = stock_status.get(code)
        if row and row[0]:  # limit_exceeded=1
            return "회사한도초과"
    reason_cats = {
        "관리종목",
        "상장폐지",
        "투자주의",
        "투자경고",
        "투자위험",
        "거래소조치",
        "단기과열",
    }
    reason_kws = (
        "투자주의",
        "투자경고",
        "투자위험",
        "매매거래 정지",
        "매매거래정지",
        "관리종목",
        "상장폐지",
        "불성실공시",
        "단기과열",
    )

    candidates: list[dict] = []
    for d in disclosures:
        rtitle = d.get("raw_title") or d.get("title") or ""
        cat = d.get("category") or ""
        if cat in reason_cats or any(kw in rtitle for kw in reason_kws):
            candidates.append(d)
    if not candidates:
        # 공시 없는 신용불가 → 실제 원인 추적 (사유 자체만 — "- 부가설명" 제거)
        if code and stock_status:
            ss = stock_status.get(code)
            if ss and not ss[0]:  # limit_exceeded=0 but still credit_risk
                return "신용융자 불가"
        if code:
            from .db import connect as _connect

            with _connect() as _c:
                # 1) 최근 14일 이내 거래소 조치 공시
                since = (
                    datetime.strptime(today, "%Y-%m-%d") - timedelta(days=14)
                ).strftime("%Y-%m-%d")
                recent_disc = _c.execute(
                    """SELECT title, disclosure_cat
                       FROM disclosures
                       WHERE stock_code=? AND date BETWEEN ? AND ?
                       AND disclosure_cat IN ('투자주의','투자경고','투자위험','거래소조치','관리종목','상장폐지','단기과열')
                       ORDER BY date DESC LIMIT 1""",
                    (code, since, today),
                ).fetchone()
                if recent_disc:
                    cat = recent_disc["disclosure_cat"] or ""
                    return cat if cat else _strip_suffix(recent_disc["title"] or "")

                # 2) 종목 유형 (사유 자체만)
                master = _c.execute(
                    "SELECT name, market FROM stocks WHERE code=?", (code,)
                ).fetchone()
                if master:
                    name = master["name"] or ""
                    if any(
                        tag in name
                        for tag in (
                            "ETF",
                            "ETN",
                            "KODEX",
                            "TIGER",
                            "KBSTAR",
                            "ARIRANG",
                            "SOL",
                        )
                    ):
                        return "ETF/ETN"
                    if "스팩" in name or "SPAC" in name.upper():
                        return "스팩(SPAC)"
                    if name.endswith("우") or name.endswith("우B"):
                        return "우선주"
        return "신용거래 제한 종목"

    # 심각도 순위 (거래정지 > 투자위험 > 투자경고 > 투자주의 > 관리종목 > 상장폐지 > 단기과열)
    sev_keys = [
        ("매매거래정지", "매매거래정지"),
        ("매매거래 정지", "매매거래정지"),
        ("투자위험", "투자위험"),
        ("투자경고", "투자경고"),
        ("투자주의", "투자주의"),
        ("관리종목", "관리종목"),
        ("상장폐지", "상장폐지"),
        ("단기과열", "단기과열"),
        ("불성실공시", "불성실공시"),
    ]

    def severity(d: dict) -> int:
        rtitle = d.get("raw_title") or d.get("title") or ""
        cat = d.get("category") or ""
        for i, (kw, _) in enumerate(sev_keys):
            if kw in rtitle or cat == sev_keys[i][1]:
                return i
        return len(sev_keys)

    top = sorted(candidates, key=severity)[0]
    rtitle = top.get("raw_title") or top.get("title") or ""
    cat = top.get("category") or ""
    start = top.get("period_start")
    end = top.get("period_end")
    sf = _fmt_ko_date(start)
    ef = _fmt_ko_date(end)

    def _range_or_single(start_s: str, end_s: str) -> str:
        if sf and ef and start_s != end_s:
            return f"{sf} ~ {ef}"
        if sf and ef:
            return f"{sf} 당일 적용"
        if sf:
            return f"{sf}부터 (재개 미정)" if "정지" in rtitle else f"{sf}부터"
        if ef:
            return f"~ {ef}"
        return ""

    # 사유 자체만 — "- 부가설명" 제거. 기간·날짜는 status_badges에 별도 노출
    # 1) 거래정지 (예고 vs 본 거래정지 분기 — Q-CYCLE21-005 Phase 1 정교화)
    # 본 거래정지 = 즉시·미래 발효 매매거래정지 공시.
    # 예고 = 매매거래정지 예고 (조건부 시장감시규정 §5의3, 본 거래정지 자체 아님).
    # FLR-AGT-002 동형 차단: 예고를 본 거래정지로 표시 시 신용불가 사유 거짓 충실성.
    # 라이브 데이터 (2026-05-04 케이엠제약 등): disclosure_cat='거래정지' 99% = 예고.
    # cat 정합: status_badges line 3275는 cat='거래정지' 매핑 → 본 함수도 동일 cat 사용
    # (legacy 'cat == "거래소조치"' substring branch는 status_badges 매핑 본문 부재 → 정합 정정).
    if (
        "매매거래정지" in rtitle
        or "매매거래 정지" in rtitle
        or cat in ("거래정지", "거래소조치")
    ):
        # 예고 분기: rtitle "예고" 본문 포함 시 (KRX 공식 표기 = '매매거래정지 예고').
        # status_badges line 3293-3302 (REQ-051 FLR-AGT-002 차단) 동형 정합.
        if "예고" in rtitle:
            return "매매거래정지 예고"
        return "매매거래정지"
    # 2) 투자위험/경고/주의
    for tag in ("투자위험", "투자경고", "투자주의"):
        if tag in rtitle or cat == tag:
            return tag
    # 3) 단기과열 (예고/지정 구분 보존)
    if "단기과열" in rtitle or cat == "단기과열":
        return "단기과열 예고" if "예고" in rtitle else "단기과열 지정"
    # 4) 관리/상장폐지
    if cat in ("관리종목", "상장폐지") or "관리종목" in rtitle or "상장폐지" in rtitle:
        return (
            cat
            if cat in ("관리종목", "상장폐지")
            else ("관리종목" if "관리종목" in rtitle else "상장폐지")
        )
    # 5) 폴백 — 부가설명 제거 (`-` 또는 `:` 또는 `(` 이후 잘라냄)
    if "불성실공시" in rtitle:
        return "불성실공시"
    return _strip_suffix(rtitle) if rtitle else "사유 미공개"


def _strip_suffix(s: str) -> str:
    """사유 텍스트에서 부가설명 제거 — `- ...`, `: ...`, `(... )` 패턴 잘라냄."""

    if not s:
        return ""
    # `- 부가설명`, ` — 부가설명` 패턴 잘라내기
    s = re.split(r"\s*[-—–]\s+", s, maxsplit=1)[0]
    # `: 부가설명` 잘라내기
    s = re.split(r"\s*:\s+", s, maxsplit=1)[0]
    # 첫 괄호 이후 잘라내기 (괄호 안이 부가설명인 경우 — 종목명 포함된 raw_title 등)
    s = re.split(r"\s*\(", s, maxsplit=1)[0]
    return s.strip()


def _add_trading_days(start_date: str, n_days: int) -> str:
    """start_date(YYYY-MM-DD)에서 n_days 거래일 후 날짜 반환. 시작일 포함 안 함."""
    from .config import is_market_holiday

    dt = datetime.strptime(start_date, "%Y-%m-%d")
    added = 0
    while added < n_days:
        dt += timedelta(days=1)
        if not is_market_holiday(dt.strftime("%Y-%m-%d")):
            added += 1
    return dt.strftime("%Y-%m-%d")


def _load_korea_holidays() -> tuple[dict, dict]:
    """togusa 화이트리스트 (rules/_whitelist/korea-trading-holidays.json) 로드.

    Returns:
        (verified_map, estimated_map): 각 {YYYY-MM-DD: 사유 텍스트}.
        파일 부재/파싱 실패 시 빈 dict 2개.

    Source 우선순위 (소스: korea-trading-holidays.json $verification_note):
      - trading_holidays_2026: verified (이시카와 §1 / homepage holidays.json 정합)
      - trading_holidays_2027_h1: estimated (KRX 미공시 — 한국 공휴일 룰 추정)
    """
    try:
        if not KOREA_HOLIDAYS_PATH.exists():
            return {}, {}
        data = json.loads(KOREA_HOLIDAYS_PATH.read_text())
        cats = data.get("categories", {}) or {}
        verified: dict = {}
        estimated: dict = {}
        for _key, _cat in cats.items():
            if not isinstance(_cat, dict):
                continue
            grade = _cat.get("verification_status")
            items = _cat.get("items") or {}
            if not isinstance(items, dict):
                continue
            if grade == "verified":
                verified.update(items)
            elif grade == "estimated":
                estimated.update(items)
        return verified, estimated
    except Exception:
        return {}, {}


def _next_trading_day_strict(view_date: str) -> tuple[str | None, str]:
    """DSN-004 v9.1 §I.2 — predicted 시제 칩 분기용 다음 거래일 산출.

    Source 체인 (1차→3차):
      1. togusa rules/_whitelist/korea-trading-holidays.json (verified + estimated)
      2. 100m1s-homepage/data/holidays.json (market_closed map)
      3. _add_trading_days (config.KR_HOLIDAYS_2026 하드코딩 — 5/1 등 일부 누락)

    estimated 등급(2027 h1) hit 시 process 1회 콘솔 경고 (FLR-20260423-FLR-002 정합).

    Returns:
        (next_date_str | None, source_grade)
        - source_grade ∈ {'verified', 'estimated', 'fallback_homepage', 'fallback_legacy', 'unknown'}
    """
    global _ESTIMATED_WARN_LOGGED

    # 1차: togusa 화이트리스트
    verified_map, estimated_map = _load_korea_holidays()
    if verified_map or estimated_map:
        try:
            dt = datetime.strptime(view_date, "%Y-%m-%d")
            grade = "verified"
            for _ in range(14):
                dt += timedelta(days=1)
                cand = dt.strftime("%Y-%m-%d")
                # 토(5)/일(6) — togusa weekend_rule (배열 무한 확장 회피)
                if dt.weekday() >= 5:
                    continue
                if cand in estimated_map:
                    grade = "estimated"
                    if not _ESTIMATED_WARN_LOGGED:
                        print(
                            f"[build_daily] WARN: 2027 추정 휴장일({cand}) hit — "
                            "togusa korea-trading-holidays.json estimated 등급. "
                            "KRX 공식 공시 후 verified 갱신 필요 (FLR-20260423-FLR-002).",
                            flush=True,
                        )
                        _ESTIMATED_WARN_LOGGED = True
                    continue
                if cand in verified_map:
                    continue
                return cand, grade
        except Exception:
            pass

    # 2차: homepage holidays.json
    try:
        holidays_path = HOMEPAGE / "data" / "holidays.json"
        if holidays_path.exists():
            data = json.loads(holidays_path.read_text())
            market_closed = data.get("market_closed", {}) or {}
            if market_closed:
                dt = datetime.strptime(view_date, "%Y-%m-%d")
                for _ in range(14):
                    dt += timedelta(days=1)
                    cand = dt.strftime("%Y-%m-%d")
                    if cand not in market_closed:
                        return cand, "fallback_homepage"
    except Exception:
        pass

    # 3차: legacy
    try:
        return _add_trading_days(view_date, 1), "fallback_legacy"
    except Exception:
        return None, "unknown"


def _calc_range_240d_at(
    conn, code: str, today: str, current_price: int | None
) -> dict | None:
    """v4 옵션 B — 시점별 240영업일 high/low 재계산.

    dailybars 시계열에서 today 이전 (today 포함) 240행을 가져와 통계.
    current_price가 None이면 today 종가 사용.
    데이터 부족 (50행 미만) 또는 dailybars 미존재 시 None 반환 → caller가 fallback.
    """
    try:
        rows = conn.execute(
            """SELECT date, open, high, low, close FROM dailybars
               WHERE code=? AND date<=?
               ORDER BY date DESC LIMIT 240""",
            (code, today),
        ).fetchall()
    except Exception:
        return None
    if not rows or len(rows) < 50:
        return None

    cur = current_price
    if cur is None or cur <= 0:
        # today 또는 가장 최근 종가
        for r in rows:
            if r["close"] and r["close"] > 0:
                cur = r["close"]
                break
    if not cur or cur <= 0:
        return None

    high_val = -1
    high_date = ""
    low_val = 10**12
    low_date = ""
    for r in rows:
        h = r["high"] or 0
        lo = r["low"] or 0
        d = r["date"]
        if h > 0 and h > high_val:
            high_val = h
            high_date = d
        if lo > 0 and lo < low_val:
            low_val = lo
            low_date = d
    if high_val <= 0 or low_val >= 10**12:
        return None
    # FLR-20260425 (대표 지적, 고영 4/24 케이스): dailybars의 today 행이
    # 부분 데이터(intraday 시점 high)이거나 미적재일 때 신고가/신저가가
    # range에서 누락된다. current_price(=오늘 종가)도 high/low 후보에 포함하여
    # 적재 타이밍 의존성을 제거. cur==high인 동률은 today 갱신을 우선 표시.
    if cur > high_val:
        high_val = cur
        high_date = today
    if cur < low_val:
        low_val = cur
        low_date = today

    # REQ-068 (대표 지적, 4/28 대한전선): dailybars의 today 행 미적재 + close가
    # 240일 max(high)와 동률일 때, "현재가 = 신고가" 표기되지만 미니캔들은 daily_picks
    # 장중 high 사용 → 위 꼬리 표시 → 시각 모순. 본질: range_240d가 close만 보고
    # 장중 더 높았던 가격을 누락. daily_picks의 today high/low도 후보에 포함하여
    # 미니캔들과 정합 보장.
    try:
        picks_row = conn.execute(
            """SELECT high_price, low_price FROM daily_picks
               WHERE stock_code=? AND date=?""",
            (code, today),
        ).fetchone()
    except Exception:
        picks_row = None
    if picks_row:
        try:
            picks_high = int(picks_row["high_price"] or 0)
            picks_low = int(picks_row["low_price"] or 0)
        except (TypeError, ValueError):
            picks_high = 0
            picks_low = 0
        if picks_high > high_val:
            high_val = picks_high
            high_date = today
        if picks_low > 0 and picks_low < low_val:
            low_val = picks_low
            low_date = today

    # REQ-008 Invariant 1 (data): range_240d.high >= current AND range_240d.low <= current.
    # 4/25 고영 사고(high=37600 < current=42300) 직접 차단. 위 보정 로직(cur>high면 cur 채택)
    # 이후에도 위반이면 데이터 모순 — RuntimeError로 빌드 실패시켜 라이브 노출 차단.
    # 부동소수점·정수 혼용 안전판으로 1원 tolerance 허용 (정수 종가 기준 영향 0).
    if high_val + 1 < cur:
        raise RuntimeError(
            f"INVARIANT_VIOLATION range_240d.high < current: "
            f"code={code} today={today} high={high_val} high_date={high_date} current={cur}"
        )
    if low_val - 1 > cur:
        raise RuntimeError(
            f"INVARIANT_VIOLATION range_240d.low > current: "
            f"code={code} today={today} low={low_val} low_date={low_date} current={cur}"
        )

    pct_from_high = round((cur - high_val) / high_val * 100, 2)
    pct_from_low = round((cur - low_val) / low_val * 100, 2)
    return {
        "high": high_val,
        "high_date": high_date,
        "high_pct": pct_from_high,
        "low": low_val,
        "low_date": low_date,
        "low_pct": pct_from_low,
        "current": cur,
    }


def _calc_daily_20(conn, code: str, today: str) -> list[dict] | None:
    """REQ-pm320-ux-cycle #3 — 시점별 20영업일 일봉 OHLC.

    dailybars에서 today 이전 최대 20행을 ASC 정렬로 반환. 카드 head 캔들 SVG 렌더용.
    행이 1건 이상이면 그대로 반환 (Q-20260512-FRESH-LISTING-DATA: 신규 상장 1건도 자연 노출).
    행 0건이면 None. OHLC 일부 0/음수 시 None (데이터 무결성).
    키 약어(o/h/l/c)는 프론트 SVG 빌드시 페이로드 크기 절감 목적.

    Q-20260512-DAILY-20-TODAY-FALLBACK (2026-05-12 catch):
    dailybars의 마지막 row.date < today 이고 daily_picks에 today 행이 있으면
    daily_picks 한 행을 today 일봉으로 append (장중 잠정 일봉). 본 fallback이 없으면
    신규 상장 종목 등에서 카드 일자(today)와 마지막 일봉(전일) 불일치로 renderer
    stale 분기가 작동하여 "전일 기준 레이블"이 노출되는 시각 모순 발생.

    Q-20260515-DAILY-20-ADJ-RATIO (2026-05-15 catch):
    dailybars OHLC raw. 권리락·분할 종목에서 미니캔들(raw)과 일봉캔들 마지막 행
    (top-level close_price, build_daily.py L3003~3022에서 adjustments 적용된 SoT)
    mismatch. 각 row의 OHLC에 `(row.date < adj.date <= today)`인 ratio 누적곱
    cumulative_ratio 곱셈하여 권리락 이후 가격 수준으로 정규화. ratio 부재 시 1.0 (영향 0).
    예: 290690 5/15 ratio=1.688 → 5/14 이전 raw OHLC 모두 ×1.688, 5/15 row 자체는 ×1.0.
    cascade SQL 패턴 (heroshik_strict_5_6_v3.py L157~163) 동형, 다만 다중 row이므로
    Python 측 누적곱 적용.
    """
    try:
        rows = conn.execute(
            """SELECT date, open, high, low, close FROM dailybars
               WHERE code=? AND date<=?
               ORDER BY date DESC LIMIT 20""",
            (code, today),
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None

    # Q-20260515-DAILY-20-ADJ-RATIO: ratio 조회 1회 (today 기준 20행 범위 내).
    # row.date 이후 발생한 ratio들의 누적곱이 해당 row의 보정 계수.
    try:
        adj_rows = conn.execute(
            """SELECT date, ratio FROM dailybars_adjustments
               WHERE code=? AND date <= ? AND ratio > 0
               ORDER BY date ASC""",
            (code, today),
        ).fetchall()
    except Exception:
        adj_rows = []
    adj_pairs = [(a["date"], float(a["ratio"])) for a in adj_rows if a["ratio"]]

    out = []
    for r in reversed(rows):
        o = r["open"] or 0
        h = r["high"] or 0
        lo = r["low"] or 0
        c = r["close"] or 0
        if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
            return None
        # row.date 이후 ratio 누적곱
        cum = 1.0
        for adj_date, ratio in adj_pairs:
            if adj_date > r["date"]:
                cum *= ratio
        if cum != 1.0:
            o = int(round(o * cum))
            h = int(round(h * cum))
            lo = int(round(lo * cum))
            c = int(round(c * cum))
        out.append({"date": r["date"], "o": o, "h": h, "l": lo, "c": c})

    # daily_picks today fallback — 마지막 dailybars row.date < today 시에만 가동
    if out and out[-1]["date"] < today:
        try:
            pick = conn.execute(
                """SELECT open_price, high_price, low_price, price
                   FROM daily_picks
                   WHERE stock_code=? AND date=? AND source='kiwoom'
                   LIMIT 1""",
                (code, today),
            ).fetchone()
        except Exception:
            pick = None
        if pick:
            try:
                p_o = int(pick["open_price"] or 0)
                p_h = int(pick["high_price"] or 0)
                p_l = int(pick["low_price"] or 0)
                p_c = int(pick["price"] or 0)
            except (TypeError, ValueError):
                p_o = p_h = p_l = p_c = 0
            if p_o > 0 and p_h > 0 and p_l > 0 and p_c > 0:
                out.append({"date": today, "o": p_o, "h": p_h, "l": p_l, "c": p_c})
                # 21건 초과 시 head 1건 제거하여 20건 유지
                if len(out) > 20:
                    out = out[-20:]
    return out


# predicted 배지 기간 규정 테이블 (참고: interpret_disclosures.py:62)
# start_offset: today 기준 지정일까지 거래일 수 (+1=익일)
# duration: start 기준 기간 거래일 수 (해제일 = start + duration)
# 단기과열: D+1 예고 → D+2 지정 → D+4 해제 (해제는 지정 후 3거래일)
_PREDICTED_PERIOD_RULES = {
    "투자주의 근접": {"start_offset": 1, "duration": 1},  # 익일 지정, 1일 해제
    "투자경고 근접": {"start_offset": 1, "duration": 2},  # 익일 지정, 2거래일 단일가
    "투자위험 근접": {
        "start_offset": 1,
        "duration": 4,
    },  # 익일 지정, 1일 정지+3일 단일가
    "단기과열 근접": {"start_offset": 2, "duration": 3},  # D+2 지정, 3거래일 단일가
}


def _period_for_predicted(label: str, today: str) -> tuple[str | None, str | None]:
    """predicted 배지 라벨 → (period_start, period_end) 거래일 계산.

    라벨이 규정 테이블에 없으면 (None, None) 반환.
    _add_trading_days 실패(데이터 에러)도 (None, None)으로 안전 폴백.
    """
    rule = _PREDICTED_PERIOD_RULES.get(label)
    if not rule:
        return None, None
    try:
        start = _add_trading_days(today, rule["start_offset"])
        end = _add_trading_days(start, rule["duration"])
        return start, end
    except Exception:
        return None, None


# 임무 E (2026-04-23) — predicted 배지 단계별 안전 스위치.
# togusa JSON v1.0 threshold 오류·지수 데이터 부재로 일부 단계 정확도 미보장.
# 단계별 개별 ON/OFF로 실생산 오보 방지. v1.1 + 임무 C(지수) 완료 후 재평가.
PREDICT_ENABLED = {
    "투자주의": True,  # 5일 60% 기반 — KRX 가격 요건 보수적, 오보 가능성 낮음
    "투자경고": False,  # 15일 100% threshold 정합성 확인 전까지 OFF (togusa JSON v1.1 대기)
    # REQ-003 완료 (2026-04-24): stock_alert_history + _compute_index_ratio_period +
    # 3경로 패턴 (risk_surge_3d_45pct / 5d_60pct / 15d_100pct_after_warn).
    # 업종지수 미조달 상태이므로 종합지수(KOSPI/KOSDAQ) 기준으로 평가하며
    # 각 배지에 regulation_source_confidence="pending" 표기 → UI 배너가 출처 고지.
    "투자위험": True,
    "단기과열": False,  # 복합지표(회전율·변동성) 데이터 미조달
}

# 지수 등락률 ≈ 0 판정 epsilon (ratio 분모 폭주 방지).
# 값 선택 근거: KOSPI/KOSDAQ 일별 등락률이 ±0.01% 미만인 날은 사실상 '변동 없음'에
# 가까워 종목상승률 대비 비율이 수백~수천 배로 튐. 보수적 0.01 고정.
# REQ-003 완료 시 추가 — 기존 _compute_index_ratio 클로저가 모듈 레벨 상수를 참조하도록 승격.
INDEX_CHG_EPS = 0.01


# 투자경고 active 판정 — stock_alert_history 기반 (임무 D 산출물).
# REQ-003 (2026-04-24): 투자위험 predicted 3경로의 prerequisite 전용.
# 규정: 투자위험 entry_paths.prerequisite = "warn_designated_within_10d"
#   → 해당 종목의 투자경고가 지정(designated) 상태이고 해제(released)되지 않았으며
#     최근 designated 이벤트가 today 기준 10영업일 이내여야 함.
# 본 구현은 "designated|notice 가 released보다 최신이고, 최신 designated가 10영업일 이내"
# 여부를 판정. 보수적으로 released 우선 확인 → 빈 결과거나 designated가 10일 이상 지난
# 경우 False 반환.
def _is_under_warning(conn, stock_code: str, today: str, within_days: int = 10) -> bool:
    """stock_alert_history에서 투자경고 active 여부 판정.

    Args:
        conn: sqlite3 connection
        stock_code: 종목코드 (6자리)
        today: YYYY-MM-DD
        within_days: 지정 후 몇 영업일 이내까지 active 간주 (기본 10 = KRX 투자위험 승급 창)

    Returns:
        True: 투자경고 최신 이벤트가 designated/notice 이고 within_days 이내
        False: 최신 이벤트가 released 이거나, 레코드 자체 없음, 또는 within_days 초과
    """
    try:
        row = conn.execute(
            """SELECT date, event_type FROM stock_alert_history
               WHERE stock_code=? AND stage='투자경고' AND date<=?
               ORDER BY date DESC LIMIT 1""",
            (stock_code, today),
        ).fetchone()
    except Exception:
        # 테이블 미존재 등 → graceful degrade, 투자위험 predicted 평가 중단
        return False
    if not row:
        return False
    if row["event_type"] not in ("designated", "notice"):
        return False
    # within_days 영업일 이내 확인 — dailybars 거래일 카운트로 판정 (휴장 고려)
    try:
        cnt = conn.execute(
            """SELECT COUNT(DISTINCT date) AS n FROM dailybars
               WHERE date > ? AND date <= ?""",
            (row["date"], today),
        ).fetchone()
        if cnt is None or cnt["n"] is None:
            return False
        # cnt["n"] = today 까지의 거래일 수(지정일 미포함). within_days 이내면 active.
        return int(cnt["n"]) <= within_days
    except Exception:
        return False


# 투자위험 3경로 전용 ratio 계산 — N영업일 기간.
# 규정: "주가상승률이 같은 기간 종합주가지수 상승률의 5배(또는 3배) 이상"
# (FLR-20260424-FLR-001 시정 결과와 동일 수식: stock_chg / idx_chg).
#   - 본 헬퍼는 "기간 누적 상승률"을 사용 (일일 등락률이 아님).
#   - 업종지수 미조달 → KOSPI/KOSDAQ 종합지수 기준 (regulation_source_confidence="pending").
def _compute_index_ratio_period(
    conn, stock_code: str, today: str, days: int, stock_close_today: int | None
) -> tuple[float | None, bool]:
    """N영업일 기간 종목상승률 ÷ 종합지수상승률.

    Args:
        conn: sqlite3 connection
        stock_code: 종목코드
        today: YYYY-MM-DD
        days: 기간 (3/5/15)
        stock_close_today: 오늘 종가 (dailybars에 미반영일 수 있어 인자로 전달)

    Returns:
        (ratio, eligible) —
          ratio: 수치. 계산 불가 시 None.
          eligible: 비교 가능 여부 (상승 국면 + 데이터 충족). False면 호출측이 met=False로 간주.
    """
    if stock_close_today is None or stock_close_today <= 0:
        return None, False

    # 종목 N영업일 전 종가
    try:
        srow = conn.execute(
            """SELECT close FROM dailybars
               WHERE code=? AND date < ? AND close > 0
               ORDER BY date DESC LIMIT 1 OFFSET ?""",
            (stock_code, today, days - 1),
        ).fetchone()
    except Exception:
        return None, False
    if not srow or not srow["close"] or srow["close"] <= 0:
        return None, False
    stock_close_n_ago = float(srow["close"])
    stock_chg = (float(stock_close_today) - stock_close_n_ago) / stock_close_n_ago * 100

    # 종목 시장 분기 — stocks.market 조회 (KOSPI/KOSDAQ만 지원, 그 외 None)
    try:
        mrow = conn.execute(
            "SELECT market FROM stocks WHERE code=?", (stock_code,)
        ).fetchone()
    except Exception:
        mrow = None
    market = (mrow["market"] if mrow else None) or ""
    if market not in ("KOSPI", "KOSDAQ"):
        return None, False

    # 지수 오늘 종가 및 N영업일 전 종가
    try:
        irow_today = conn.execute(
            """SELECT close FROM index_dailybars
               WHERE index_code=? AND date<=? AND close>0
               ORDER BY date DESC LIMIT 1""",
            (market, today),
        ).fetchone()
        irow_past = conn.execute(
            """SELECT close FROM index_dailybars
               WHERE index_code=? AND date<=? AND close>0
               ORDER BY date DESC LIMIT 1 OFFSET ?""",
            (market, today, days),
        ).fetchone()
    except Exception:
        return None, False
    if not irow_today or not irow_past:
        return None, False
    if not irow_today["close"] or not irow_past["close"] or irow_past["close"] <= 0:
        return None, False
    idx_today = float(irow_today["close"])
    idx_past = float(irow_past["close"])
    idx_chg = (idx_today - idx_past) / idx_past * 100

    if abs(idx_chg) < INDEX_CHG_EPS:
        return None, False
    ratio = round(stock_chg / idx_chg, 2)
    eligible = stock_chg > 0 and idx_chg > 0
    return ratio, eligible


def _predict_status_from_dailybars(
    conn, code: str, today: str, current_price: int
) -> list[dict]:
    """v4 — KRX 시장경보 주요 패턴을 dailybars 시계열로 자체 검사.

    공시(확정) 라벨이 없을 때만 호출됨. 결과는 status_badges에 추가되며 source='predicted'.
    label에 "근접" suffix → frontend가 시각 구분 가능 (KRX 공식 "예고"와 구분).

    검사 패턴 (가격 only — 거래량 조건은 별도 데이터 필요해서 제외):
      - 투자주의 (근접): 5거래일 전 종가 대비 60%↑
      - 투자경고 (근접): 15거래일 전 종가 대비 100%↑
      - 단기과열 (근접): 직전 5거래일 평균 종가 대비 30%↑ + 직전 25거래일 종가 대비 75%↑

    참고: 실제 KRX 규정은 거래대금/회전율/지수반영 등 추가 조건 多.
    여기선 보수적 가격-only 검사로 거짓양성 최소화. label에 "근접" 표기 의무.
    각 배지에 period_start/period_end(+ start/end alias) 주입 — 렌더러 호환.
    """
    rows = conn.execute(
        """SELECT date, close FROM dailybars
           WHERE code=? AND date<? AND close>0
           ORDER BY date DESC LIMIT 30""",
        (code, today),
    ).fetchall()
    if not rows or len(rows) < 5:
        return []

    closes = [r["close"] for r in rows]
    dates = [r["date"] for r in rows]

    badges: list[dict] = []

    # 패턴 1: 5일 전 종가 대비 60%↑ → 투자주의 근접
    if PREDICT_ENABLED.get("투자주의", False) and len(closes) >= 5:
        base5 = closes[4]
        if base5 > 0:
            pct5 = (current_price - base5) / base5 * 100
            if pct5 >= 60:
                badges.append(
                    {
                        "label": "투자주의 근접",
                        "severity": "caution",
                        "source": "predicted",
                        "thresholds": [
                            {
                                "desc": f"5일 전({dates[4]}) 대비 +60%↑",
                                "base_price": base5,
                                "threshold": int(base5 * 1.6),
                                "current": current_price,
                                "triggered": True,
                            }
                        ],
                        "regulation": "KRX 시장경보 가격 조건 충족 (거래량 미검증, 자체 추정)",
                    }
                )

    # 패턴 2: 15일 전 종가 대비 100%↑ → 투자경고 근접
    if PREDICT_ENABLED.get("투자경고", False) and len(closes) >= 15:
        base15 = closes[14]
        if base15 > 0:
            pct15 = (current_price - base15) / base15 * 100
            if pct15 >= 100:
                badges.append(
                    {
                        "label": "투자경고 근접",
                        "severity": "warning",
                        "source": "predicted",
                        "thresholds": [
                            {
                                "desc": f"15일 전({dates[14]}) 대비 +100%↑",
                                "base_price": base15,
                                "threshold": int(base15 * 2.0),
                                "current": current_price,
                                "triggered": True,
                            }
                        ],
                        "regulation": "KRX 시장경보 가격 조건 충족 (거래량 미검증, 자체 추정)",
                    }
                )

    # 패턴 2.5 (REQ-003, 2026-04-24): 투자위험 근접 — 투자경고 지정 종목이
    # 3경로(3일 45% / 5일 60% / 15일 100%) 급등 + 지수 ratio(5x/5x/3x) + 15일 최고가.
    # 규정 출처: rules/krx-stage-conditions.json v1.4 stages[투자위험].entry_paths.
    # 제약:
    #   - 업종지수 미조달 (REQ-005 대기) → 종합지수 기준. 배지에 regulation_source_confidence="pending".
    #   - 투자경고 active 선결 — _is_under_warning(within_days=10) 필터링.
    #   - 데이터 충분성(dailybars N+1일 이상 + index_dailybars) 부족 시 해당 경로 생략.
    if PREDICT_ENABLED.get("투자위험", False):
        if _is_under_warning(conn, code, today, within_days=10):
            _risk_paths = [
                (
                    "risk_surge_3d_45pct_after_warn",
                    3,
                    0.45,
                    5.0,
                    "danger",
                    "3일 전 종가 대비 45%↑ + 지수 5배 + 15일 최고가",
                ),
                (
                    "risk_surge_5d_60pct_after_warn",
                    5,
                    0.60,
                    5.0,
                    "danger",
                    "5일 전 종가 대비 60%↑ + 지수 5배 + 15일 최고가",
                ),
                (
                    "risk_surge_15d_100pct_after_warn",
                    15,
                    1.00,
                    3.0,
                    "danger",
                    "15일 전 종가 대비 100%↑ + 지수 3배 + 15일 최고가",
                ),
            ]
            for path_id, days_n, price_th, ratio_th, sev, desc_txt in _risk_paths:
                # 데이터 충족: closes 인덱스 days_n 까지 접근 가능해야 함 (closes[days_n-1])
                if len(closes) < days_n:
                    continue
                base_n = closes[days_n - 1]
                if not base_n or base_n <= 0:
                    continue
                price_chg = current_price / float(base_n) - 1
                if price_chg < price_th:
                    continue
                # 15일 최고가 조건 — dailybars 최근 15일 + current_price 최고여야 함
                # closes[:15]가 15개 미만이면 (상장 직후 등) 해당 경로 skip (보수적).
                if len(closes) < 15:
                    continue
                if current_price < max(closes[:15]):
                    continue
                # 지수 ratio — 종합지수 기준 (업종지수 미조달 상태)
                _ratio, _elig = _compute_index_ratio_period(
                    conn, code, today, days_n, current_price
                )
                if not _elig or _ratio is None or _ratio < ratio_th:
                    continue
                badges.append(
                    {
                        "label": "투자위험 근접",
                        "severity": sev,
                        "source": "predicted",
                        "state": "predicted_stage3_notice",
                        "paths": [path_id],
                        "path": path_id,
                        "price_chg": round(price_chg, 4),
                        "index_ratio": _ratio,
                        "confidence": "medium",
                        "regulation_source_confidence": "pending",
                        "thresholds": [
                            {
                                "desc": f"{days_n}일 전({dates[days_n - 1]}) 대비 +{int(price_th * 100)}%↑",
                                "base_price": int(base_n),
                                "threshold": int(base_n * (1 + price_th)),
                                "current": current_price,
                                "triggered": True,
                            },
                            {
                                "desc": f"종합지수({days_n}영업일) 상승률 대비 {ratio_th:g}배 이상",
                                "base_price": None,
                                "threshold": ratio_th,
                                "current": _ratio,
                                "triggered": True,
                            },
                            {
                                "desc": f"최근 15일 최고가 ({dates[0]}~{dates[14]})",
                                "base_price": int(max(closes[:15])),
                                "threshold": int(max(closes[:15])),
                                "current": current_price,
                                "triggered": True,
                            },
                        ],
                        "regulation": (
                            "KRX 시장경보 투자위험 entry_paths (투자경고 전제 + "
                            + desc_txt
                            + "). 업종지수 미조달 상태 — 종합지수 기준 잠정 평가"
                        ),
                    }
                )
                # 3경로 중 하나라도 매칭되면 중단 (배지 중복 방지).
                # 필요 시 모든 경로를 paths[] 배열에 누적하는 설계로 확장 가능.
                break

    # 패턴 3: 단기과열 근접 — 직전 5일 평균 대비 30%↑ AND 25일 전 대비 75%↑
    if PREDICT_ENABLED.get("단기과열", False) and len(closes) >= 25:
        avg5 = sum(closes[:5]) / 5
        base25 = closes[24]
        if avg5 > 0 and base25 > 0:
            pct_avg5 = (current_price - avg5) / avg5 * 100
            pct_25 = (current_price - base25) / base25 * 100
            if pct_avg5 >= 30 and pct_25 >= 75:
                badges.append(
                    {
                        "label": "단기과열 근접",
                        "severity": "hot",
                        "source": "predicted",
                        "thresholds": [
                            {
                                "desc": "직전 5일 평균 대비 +30%↑",
                                "base_price": int(avg5),
                                "threshold": int(avg5 * 1.3),
                                "current": current_price,
                                "triggered": True,
                            },
                            {
                                "desc": f"25일 전({dates[24]}) 대비 +75%↑",
                                "base_price": base25,
                                "threshold": int(base25 * 1.75),
                                "current": current_price,
                                "triggered": True,
                            },
                        ],
                        "regulation": "KRX 단기과열 가격 조건 충족 (거래량 미검증, 자체 추정)",
                    }
                )

    # 대표 지시(2026-04-22): predicted 배지에 적용 기간(지정일~해제일) 주입.
    # 렌더러는 b.start/b.end를 참조하므로 start/end alias 동시 세팅.
    # period_start/period_end는 공시 DB 스키마와 의미 일치 (interpret_disclosures.py:62).
    for b in badges:
        p_start, p_end = _period_for_predicted(b.get("label", ""), today)
        if p_start and p_end:
            b["period_start"] = p_start
            b["period_end"] = p_end
            b["start"] = p_start
            b["end"] = p_end

    return badges


def _single_price_for_badge(badge: dict) -> bool:
    """REQ-008 v2 — 단일가매매 병기 규칙 (FLR-20260423-002 반영).

    True: **단기과열 '지정'만**. 규정 근거: KRX 단기과열완화제도 §5 (D+3~D+5 30분 단위).
    False: 시장경보 3단계(투자주의/투자경고/투자위험) 전부 — 단일가매매 자동 효과 없음
           (rules/krx-stage-conditions.json auto_effects_not_applicable).
    False: '예고' 단계, predicted(자체 추정), 기타 카테고리(거래정지·관리·상폐 등).
    False: start 결손(None/빈문자) — 단일가매매 적용 시점(D+3~D+5)을 산출할 수 없으므로
           프런트엔드 getSinglePriceStatus 의 epoch fallback("적용 중" 오판정) 회피.
           REQ-010 PHASE B-4 (4/21 077970 STX엔진 사례).
    """
    if badge.get("source") == "predicted":
        return False
    label = badge.get("label") or ""
    if "예고" in label or "근접" in label:
        return False
    if "단기과열" not in label:
        return False
    if not badge.get("start"):
        return False
    return True


# REQ-020 v9.5 §I — 효과 배지 SSOT 산출 (build_daily.py = 단일 진실 소스).
# 명세: DOC-20260427-DSN-005 §I.3 / §V.
# 책임 분리: build = badge별 effect_badges[] 산출 / utils.js = 카드 단위 머지·dedup·정렬.
# P0 함정 차단: view_date 거래일 가드 (휴장일/공휴일 진입 시 빈 배열).

# REQ-020a 핫픽스 — strict 3 AND SSOT (utils.js KRX_MAIN_TRACK_LABELS_FOR_STRICT 동등).
# togusa rules/krx-stage-flow.json#flow.stages 정합. predicted "X 근접"은 KRX 공식 "X 예고" 또는 1단계 자체로 매핑.
_KRX_MAIN_TRACK_LABELS_FOR_STRICT = [
    "투자주의",
    "투자경고 예고",
    "투자경고",
    "투자위험 예고",
    "투자위험",
    "매매거래정지",
]


def _match_main_track_step(label: str) -> int:
    """KRX_MAIN_TRACK 인덱스 산출. utils.js matchMainTrackStep 동등 SSOT."""
    if not label:
        return -1
    if label in _KRX_MAIN_TRACK_LABELS_FOR_STRICT:
        return _KRX_MAIN_TRACK_LABELS_FOR_STRICT.index(label)
    if label.endswith("근접"):
        stripped = label.replace("근접", "").strip()
        candidate1 = f"{stripped} 예고"
        if candidate1 in _KRX_MAIN_TRACK_LABELS_FOR_STRICT:
            return _KRX_MAIN_TRACK_LABELS_FOR_STRICT.index(candidate1)
        if stripped in _KRX_MAIN_TRACK_LABELS_FOR_STRICT:
            return _KRX_MAIN_TRACK_LABELS_FOR_STRICT.index(stripped)
    return -1


def _get_current_stage_index(badges: list) -> int:
    """current = disclosure source 중 KRX_MAIN_TRACK 최대 인덱스 (predicted 제외).
    utils.js getCurrentStageIndex 동등 SSOT.
    """
    if not isinstance(badges, list) or not badges:
        return -1
    max_idx = -1
    for b in badges:
        if not b:
            continue
        b_label = b.get("label") or ""
        is_pred = (
            b.get("source") == "predicted" or "근접" in b_label or "예상" in b_label
        )
        if is_pred:
            continue
        idx = _match_main_track_step(b_label)
        if idx > max_idx:
            max_idx = idx
    return max_idx


def _is_trading_day(view_date: str) -> bool:
    """KRX 거래일 여부. is_market_closed의 역."""
    if not view_date:
        return False
    try:
        return not is_market_closed(view_date)
    except Exception:
        return False


def _trading_day_diff(badge_start: str, view_date: str) -> int | None:
    """badge_start 기준 view_date의 D-offset (거래일 단위). 음수=발효 전, 0=지정 당일.

    SSOT: utils.js computeTradingDayDiff과 동일 의미 (영업일 캘린더 기반).
    badge_start와 view_date 사이 거래일 수를 카운트.
    """
    if not badge_start or not view_date:
        return None
    try:
        from .config import is_market_holiday

        start_dt = datetime.strptime(badge_start, "%Y-%m-%d")
        view_dt = datetime.strptime(view_date, "%Y-%m-%d")
        if start_dt == view_dt:
            return 0
        sign = 1 if view_dt > start_dt else -1
        cur = start_dt
        days = 0
        safety = 60
        target = view_dt
        while safety > 0:
            safety -= 1
            cur += timedelta(days=sign)
            ymd = cur.strftime("%Y-%m-%d")
            if not is_market_holiday(ymd):
                days += 1
            if cur == target:
                return days * sign
        return None
    except Exception:
        return None


def _compute_short_term_day_offset(badge: dict, view_date: str) -> str:
    """단기과열 dayOffset 산출 (utils.js getShortTermDayOffset SSOT 통일).

    'd+0' (지정 당일/발효 전) / 'd+1' / 'd+2' (거래정지) / 'd+3-5' (단일가매매) / 'd+6+' / 'unknown'.
    """
    start = badge.get("start") if badge else None
    if not start or not view_date:
        return "unknown"
    days = _trading_day_diff(start, view_date)
    if days is None:
        return "unknown"
    if days < 0:
        return "d+0"  # 발효 전 — 효과 부재 동일 처리
    if days == 0:
        return "d+0"
    if days == 1:
        return "d+1"
    if days == 2:
        return "d+2"
    if 3 <= days <= 5:
        return "d+3-5"
    return "d+6+"


def _compute_effect_badges(
    badge: dict, view_date: str, all_badges: list | None = None
) -> list[dict]:
    """KRX 단계 → 효과 배지(effect+when+severity) 매트릭스 산출.

    명세: DOC-20260427-DSN-005 §I.1 16건 매트릭스 + §I.3 알고리즘 + A1~A4 결정.
    P0 함정 #1 차단:
      - view_date 거래일 가드 (False → []).
      - SSOT 영업일 헬퍼 (_add_trading_days, _trading_day_diff) — utils.js와 정합.

    REQ-020b 핫픽스 — predicted source는 헤더 effect_badges 0건 단순 차단:
      - predicted_shadow = 가격·지수 임계 도달만 평가 (자체 추정).
      - KRX 실제 발효 = 비공개 조건(계좌관여율) + 거래소 운영자 판단 의존.
      - 즉 strict 3 AND 충족해도 실제 단계 진입 보장 X → 헤더 단정 메시지 부적합.
      - 4/24 주성엔지 케이스: strict 충족 → "거래정지(내일 가능)" 부착했으나 4/27 실제 미발생 (FLR-AGT-002 직격).
      - predicted 정보는 그래프 박스 predicted-imminent 노드 + entry-window + 상세 박스(dsn-v8-block--predicted)에 잔존.
    REQ-020a 잔존 — 영업일·인접 평가 헬퍼는 후속 영역(개별 단계 분기 within-disclosure)에서 잔존.

    P0 함정 #1 차단:
      - view_date 거래일 가드 (False → []).
      - SSOT 영업일 헬퍼 (_add_trading_days, _trading_day_diff) — utils.js와 정합.

    각 effect_badge = {effect, when, severity, source_label, source_kind}
      effect ∈ {'credit-block', 'trade-halt', 'single-price'}
      when ∈ {'today', 'tomorrow'} (v9.8 DSN-010 §I — tomorrow_maybe/in_*_days 폐기)
        (today_and_tomorrow 머지는 utils.js collectEffectBadges에서 카드 단위 처리)
      severity ∈ {'warning', 'danger', 'info'} (CSS 클래스 hint)
    """
    if not badge:
        return []
    # P0 가드 — 거래일이 아닌 view_date(주말/공휴일)는 빈 배열
    if not _is_trading_day(view_date):
        return []

    label = badge.get("label") or ""
    source = badge.get("source") or ""
    start = badge.get("start")
    end = badge.get("end")
    is_predicted = source == "predicted" or "근접" in label or "예상" in label
    source_kind = "predicted" if is_predicted else "disclosure"

    # REQ-020b — predicted source 헤더 effect_badges 즉시 차단.
    # 사유: predicted_shadow는 가격·지수만 평가 (자체 추정) — KRX 실제 발효는 비공개 조건 의존.
    # strict 3 AND 충족해도 단정 메시지 부적합. predicted 정보는 그래프·상세 박스에서 잔존.
    if is_predicted:
        return []

    # disclosure 시점 산출 (KRX 공식 사실만 헤더 노출)
    try:
        view_next = _add_trading_days(view_date, 1)
    except Exception:
        view_next = None
    if start and start == view_next:
        when_default = "tomorrow"
    elif start and start <= view_date and (not end or end >= view_date):
        when_default = "today"
    elif label in ("매매거래정지", "거래정지") and start and start > view_date:
        # REQ-047 — 거래정지 D+2+ 미래 (4/27 시점에 4/29 발효 등): tomorrow로 fallback.
        # 사용자 인지 우선 (정밀 D+N은 상세 영역에서 별도 표기).
        when_default = "tomorrow"
    elif label in ("거래정지 예고", "매매거래정지 예고"):
        # REQ-051 — 거래정지 예고 (조건부)는 시점 산출 통과 (effect 분기 line 1120에서 pass).
        when_default = "tomorrow"  # 표시용, 실제 effect 부착 안 됨
    else:
        return []  # 시점 산출 불가

    effects: list[dict] = []

    def _push(effect: str, when: str, severity: str) -> None:
        effects.append(
            {
                "effect": effect,
                "when": when,
                "severity": severity,
                "source_label": label,
                "source_kind": source_kind,
            }
        )

    # 단계별 효과 매핑 (KRX SSOT — krx-stage-conditions.json 정합)
    if "단기과열" in label and "예고" not in label and "근접" not in label:
        # 단기과열 본 지정 → dayOffset 분기
        day_offset = _compute_short_term_day_offset(badge, view_date)
        if day_offset == "d+0":
            pass  # 효과 부재 (A3 fallback)
        elif day_offset == "d+1":
            _push("trade-halt", "tomorrow", "danger")  # D+2 거래정지 D-1
        elif day_offset == "d+2":
            _push("trade-halt", "today", "danger")
            _push("single-price", "tomorrow", "info")  # D+3 단일가 D-1
        elif day_offset == "d+3-5":
            _push("single-price", "today", "info")
        # d+6+ / unknown → 효과 부재
    elif "단기과열" in label and "예고" in label and "근접" not in label:
        # REQ-023 v9.8 §III — 단기과열 예고 헤더 0건 (DSN-010).
        # 휴지 결정 C 정합: D+0 효과 부재 → 헤더 effect_badges 미산출.
        # 사유 박스만 1줄 산출 (§IV.3 단순 텍스트 — "M/D 지정 가능").
        # 폐기 (DSN-009 v9.7 → DSN-010 v9.8):
        #   - in_2_days / in_3_5_days enum (대표 본질 비판 #1·#2 — 거짓 정밀성)
        #   - 일정 명시 reason_text (대표 본질 비판 #6 — 1차원 정보 X)
        if start and start > view_date:
            try:

                def _md(s: str) -> str:
                    # YYYY-MM-DD → M/D
                    parts = s.split("-")
                    return f"{int(parts[1])}/{int(parts[2])}" if len(parts) == 3 else s

                # v9.8 §IV.3 — 단순 1줄. 거래정지·단일가 일정 미언급.
                badge["short_overheat_reason_text"] = (
                    f"단기과열 예고 — {_md(start)} 지정 가능"
                )
            except Exception:
                pass
        # effect_badges 0건 (헤더 미노출).
        # predicted_shadow 부활 폐기 (togusa SSOT 4/27 21:12 — 인프라 산출 0건 = 거짓 기능):
        #   - 단기과열 strict: 회전율·변동성 데이터 미조달 (krx-stage-conditions.json enabled=false)
        #   - 투자경고/투자위험 strict: 지수 데이터 미가용
        #   - REQ-020b 결정 유지 — predicted source = [] (헤더 차단)
        # 후속: P005 Trade Cockpit + data-dev 회전율·변동성 조달 후 별도 REQ.
    elif "단기과열" in label:
        # 단기과열 근접(predicted)는 상위 is_predicted 가드(라인 1029)에서 차단됨.
        # 그 외 단기과열 변형 케이스 fallback (효과 부재).
        pass
    elif label == "투자경고 예고":
        _push("credit-block", "tomorrow", "warning")
    # NOTE: REQ-020b — "투자경고 근접" / "투자위험 근접" 분기는 dead code.
    # is_predicted 차단으로 도달 불가. 후속 사이클 정리 검토.
    elif "투자경고" in label and "예고" not in label and "근접" not in label:
        _push("credit-block", when_default, "warning")
    elif label == "투자위험 예고":
        # 투자경고 효과 잔존 + 거래정지 D-1
        _push("credit-block", "today", "warning")
        _push("trade-halt", "tomorrow", "danger")
    elif "투자위험" in label and "예고" not in label and "근접" not in label:
        # 투자경고 효과 + 지정 직전 1거래일 매매거래정지 (오늘 발효 중)
        _push("credit-block", when_default, "warning")
        _push("trade-halt", when_default, "danger")
    elif label == "거래정지 예고" or label == "매매거래정지 예고":
        # REQ-051 — 거래정지 예고 (조건부 시장감시규정 §5의3) effect 0건 (FLR-AGT-002 정합).
        # 단기과열 예고 패턴 정합 (REQ-027 §D 롤백). 사유 박스에 본문 잔존, 헤더 단정 미부착.
        # 대표 비판 (2026-04-28 03:14 KST): "파두도 내일 거래정지가 되는게 아니라 예고였네"
        # 헤더 effect 미부착, 상세 영역만 잔존.
        pass
    elif label == "매매거래정지" or label == "거래정지":
        # REQ-047 — KRX 거래정지 확정 공시 (예고 외 — 즉시·미래 발효).
        # when_default는 line 1037~ 시점 산출에서 today/tomorrow로 결정됨.
        _push("trade-halt", when_default, "danger")
    # 그 외 (투자주의 등) → 효과 부재 (A3)

    return effects


def _calc_thresholds(
    conn, code: str, current_price: int, today: str, condition: str
) -> list[dict]:
    """조건 텍스트에서 비율을 추출하고 과거 가격에서 임계가격 계산."""

    thresholds = []

    # 과거 가격 조회 (daily_picks에서)
    prices = conn.execute(
        """SELECT date, price FROM daily_picks
           WHERE stock_code=? AND date <= ? AND price > 0
           ORDER BY date DESC LIMIT 20""",
        (code, today),
    ).fetchall()

    if not prices:
        return thresholds

    price_by_date = {r["date"]: r["price"] for r in prices}
    dates_desc = sorted(price_by_date.keys(), reverse=True)

    # 패턴 (KRX 시장경보 표준 조건)
    patterns = [
        # "5일 전 종가 대비 60%↑", "15일 100%↑" 등
        (
            r"(\d+)일\s*(?:전\s*종가)?\s*(?:대비|종가보다|기준|전\s*기준)?\s*(\d+)%\s*[↑이상]",
            "day_ago_pct",
        ),
        # "T-5 대비 60%↑"
        (r"T-(\d+)\s*대비\s*(\d+)%\s*[↑이상]", "t_minus_pct"),
        # "전일 대비 5%↑" (단순)
        (r"(?:전일|직전가)\s*대비\s*(\d+)%\s*[↑이상]", "prev_day_pct"),
        # "최근 15일 (최고가|종가 중 최고가)" — 현재가가 15일 최고 이상이면 trigger
        (
            r"(?:최근|판단일\s*종가가?\s*최근)?\s*(\d+)일\s*(?:종가\s*중\s*)?최고가",
            "n_day_high",
        ),
        # "1년 초과상승률 200%↑" 또는 "1년간 초과 주가상승률 200%↑"
        (r"1년(?:간|\s*초과)?\s*(?:상승률|주가상승률)\s*(\d+)%\s*[↑이상]", "year_pct"),
        # "단기과열 종료 연장: 종료일 종가가 지정일 전일 종가 대비 20% 이상" — base는 지정일 전일
        (
            r"(?:종료일\s*종가가?\s*)?지정(?:일|전)?\s*전일\s*종가\s*대비\s*(\d+)%\s*[↑이상]",
            "designation_prev_pct",
        ),
        # "2026-04-21 종가가 04-17 종가 대비 40% 이상 상승" — 절대 날짜 (월-일 형식)
        (
            r"(\d{2})-(\d{2})\s*종가\s*대비\s*(\d+)%\s*[↑이상]",
            "absolute_date_pct",
        ),
        # "지정 후 2일간 40% 이상 상승" — 지정일 종가 대비 N% (지정일=공시일로 추정)
        (
            r"지정\s*후\s*(\d+)일간\s*(\d+)%\s*[↑이상]",
            "after_designation_pct",
        ),
    ]

    for pat, ptype in patterns:
        for m in re.finditer(pat, condition):
            if ptype == "day_ago_pct":
                days_ago = int(m.group(1))
                pct = int(m.group(2))
                if days_ago < len(dates_desc):
                    base_date = dates_desc[min(days_ago, len(dates_desc) - 1)]
                    base_price = price_by_date[base_date]
                    threshold_price = int(base_price * (1 + pct / 100))
                    thresholds.append(
                        {
                            "desc": f"{days_ago}일 전({base_date}) 대비 {pct}%↑",
                            "base_price": base_price,
                            "threshold": threshold_price,
                            "current": current_price,
                            "triggered": current_price >= threshold_price,
                        }
                    )
            elif ptype == "t_minus_pct":
                days_ago = int(m.group(1))
                pct = int(m.group(2))
                if days_ago < len(dates_desc):
                    base_date = dates_desc[min(days_ago, len(dates_desc) - 1)]
                    base_price = price_by_date[base_date]
                    threshold_price = int(base_price * (1 + pct / 100))
                    thresholds.append(
                        {
                            "desc": f"T-{days_ago}({base_date}) 대비 {pct}%↑",
                            "base_price": base_price,
                            "threshold": threshold_price,
                            "current": current_price,
                            "triggered": current_price >= threshold_price,
                        }
                    )
            elif ptype == "prev_day_pct":
                pct = int(m.group(1))
                if len(dates_desc) >= 2:
                    prev_date = dates_desc[1]
                    base_price = price_by_date[prev_date]
                    threshold_price = int(base_price * (1 + pct / 100))
                    thresholds.append(
                        {
                            "desc": f"전일({prev_date}) 대비 {pct}%↑",
                            "base_price": base_price,
                            "threshold": threshold_price,
                            "current": current_price,
                            "triggered": current_price >= threshold_price,
                        }
                    )
            elif ptype == "n_day_high":
                # 최근 N일 최고가 — KRX 규정: 판단일(t) 직전 N영업일 (t-1 ~ t-N) 기준
                # FLR 학습: t 포함 시 자기 자신이 base가 되는 사고
                n = int(m.group(1))
                rows = conn.execute(
                    """SELECT date, COALESCE(high_price, price) AS hi
                       FROM daily_picks WHERE stock_code=? AND date < ?
                       AND COALESCE(high_price, price) > 0
                       ORDER BY date DESC LIMIT ?""",
                    (code, today, n),
                ).fetchall()
                if rows:
                    hi_row = max(rows, key=lambda r: r["hi"])
                    base_price = int(hi_row["hi"])
                    thresholds.append(
                        {
                            "desc": f"최근 {n}일 최고가({hi_row['date']})",
                            "base_price": base_price,
                            "threshold": base_price,
                            "current": current_price,
                            "triggered": current_price >= base_price,
                        }
                    )
            elif ptype == "year_pct":
                # 1년 초과상승률 — 1년 전 가격 ×(1+pct/100)
                pct = int(m.group(1))
                from datetime import datetime as _dt
                from datetime import timedelta as _td

                year_ago = (_dt.strptime(today, "%Y-%m-%d") - _td(days=365)).strftime(
                    "%Y-%m-%d"
                )
                row = conn.execute(
                    """SELECT date, price FROM daily_picks
                       WHERE stock_code=? AND date<=? AND price>0
                       ORDER BY ABS(julianday(date)-julianday(?)) ASC LIMIT 1""",
                    (code, today, year_ago),
                ).fetchone()
                if row:
                    base_price = row["price"]
                    threshold_price = int(base_price * (1 + pct / 100))
                    thresholds.append(
                        {
                            "desc": f"1년 전({row['date']}) 대비 {pct}%↑",
                            "base_price": base_price,
                            "threshold": threshold_price,
                            "current": current_price,
                            "triggered": current_price >= threshold_price,
                        }
                    )
            elif ptype == "designation_prev_pct":
                # 단기과열 연장 — 지정일 전일 종가 대비 20%↑.
                # 지정일은 추적이 어려우므로 14일 내 단기과열 공시 date - 1을 사용
                pct = int(m.group(1))
                desig = conn.execute(
                    """SELECT date FROM disclosures WHERE stock_code=?
                       AND disclosure_cat='단기과열' AND date<=?
                       ORDER BY date DESC LIMIT 1""",
                    (code, today),
                ).fetchone()
                if desig:
                    desig_prev = (
                        datetime.strptime(desig["date"], "%Y-%m-%d") - timedelta(days=1)
                    ).strftime("%Y-%m-%d")
                    row = conn.execute(
                        """SELECT date, price FROM daily_picks
                           WHERE stock_code=? AND date<=? AND price>0
                           ORDER BY date DESC LIMIT 1""",
                        (code, desig_prev),
                    ).fetchone()
                    if row:
                        base_price = row["price"]
                        threshold_price = int(base_price * (1 + pct / 100))
                        thresholds.append(
                            {
                                "desc": f"지정일 전일({row['date']}) 대비 {pct}%↑",
                                "base_price": base_price,
                                "threshold": threshold_price,
                                "current": current_price,
                                "triggered": current_price >= threshold_price,
                            }
                        )

            elif ptype == "absolute_date_pct":
                # "04-17 종가 대비 40%↑" — 같은 연도 가정
                mm = m.group(1)
                dd = m.group(2)
                year = today[:4]
                base_date = f"{year}-{mm}-{dd}"
                pct = int(m.group(3))
                row = conn.execute(
                    """SELECT date, price FROM daily_picks
                       WHERE stock_code=? AND date=? AND price>0 LIMIT 1""",
                    (code, base_date),
                ).fetchone()
                if not row:
                    # 가까운 거래일로 fallback
                    row = conn.execute(
                        """SELECT date, price FROM daily_picks
                           WHERE stock_code=? AND date<=? AND price>0
                           ORDER BY date DESC LIMIT 1""",
                        (code, base_date),
                    ).fetchone()
                if row:
                    base_price = row["price"]
                    threshold_price = int(base_price * (1 + pct / 100))
                    thresholds.append(
                        {
                            "desc": f"{row['date']} 종가 대비 {pct}%↑",
                            "base_price": base_price,
                            "threshold": threshold_price,
                            "current": current_price,
                            "triggered": current_price >= threshold_price,
                        }
                    )
            elif ptype == "after_designation_pct":
                # "지정 후 2일간 40%↑" — 가장 최근 시장경보 공시 date를 지정일로
                days = int(m.group(1))
                pct = int(m.group(2))
                desig = conn.execute(
                    """SELECT date FROM disclosures WHERE stock_code=?
                       AND disclosure_cat IN ('투자주의','투자경고','투자위험','단기과열')
                       AND date<=?
                       ORDER BY date DESC LIMIT 1""",
                    (code, today),
                ).fetchone()
                if desig:
                    row = conn.execute(
                        """SELECT date, price FROM daily_picks
                           WHERE stock_code=? AND date=? AND price>0 LIMIT 1""",
                        (code, desig["date"]),
                    ).fetchone()
                    if row:
                        base_price = row["price"]
                        threshold_price = int(base_price * (1 + pct / 100))
                        thresholds.append(
                            {
                                "desc": f"지정일({desig['date']}) +{days}일 종가 대비 {pct}%↑",
                                "base_price": base_price,
                                "threshold": threshold_price,
                                "current": current_price,
                                "triggered": current_price >= threshold_price,
                            }
                        )

    # 중복 제거 (같은 desc 여러 패턴이 잡을 수 있음)
    seen = set()
    unique = []
    for t in thresholds:
        key = (t["desc"], t["threshold"])
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


# REQ-019 §I (DSN-004 v9.4): 투자경고 예고 진입 임계가격 산출
#
# 토구사 SSOT (rules/krx-stage-conditions.json#stages[1].entry_paths):
#   path 0 warn_surge_3d_100pct   close_today/close[T-3]  × 2.0  + 지수 5배
#   path 1 warn_surge_5d_60pct    close_today/close[T-5]  × 1.6  + 지수 5배
#   path 2 warn_surge_15d_100pct  close_today/close[T-15] × 2.0  + 지수 3배
#   path 3 warn_repeated_caution  close_today/close[T-15] × 1.75 + 투자주의 5회 누적 (선조건)
#   path 4 warn_surge_1y_200pct   계좌관여율 KRX 비공개 → 산출 불가 (텍스트 첨언만)
#
# 대표 4결정:
#   1. 조건공식 모든 사항 고려 + 임계 외 조건 함께 명시
#   2. 복수 임계가 = 최저점만 표시
#   3. 기간 = 10거래일 모두 표시
#   4. 산출 시점 = 직전 거래일 종가 (자율 a — 휴장일 자연 fallback)
_ENTRY_PATH_LABEL = {
    "warn_surge_3d_100pct": "3일 전 종가 × 2.0",
    "warn_surge_5d_60pct": "5일 전 종가 × 1.6",
    "warn_surge_15d_100pct": "15일 전 종가 × 2.0",
    "warn_repeated_caution": "15일 전 종가 × 1.75 (투자주의 반복)",
}
_ENTRY_PATH_AND_CONDITION = {
    "warn_surge_3d_100pct": "KOSPI 동기간 상승률 5배 동시 충족 시",
    "warn_surge_5d_60pct": "KOSPI 동기간 상승률 5배 동시 충족 시",
    "warn_surge_15d_100pct": "KOSPI 동기간 상승률 3배 동시 충족 시",
    "warn_repeated_caution": "투자주의 5회 반복 충족 시",
}


def _get_close_n_trading_days_ago(conn, code: str, today: str, n: int) -> float | None:
    """dailybars에서 today 직전 n번째 거래일 종가. 미존재 시 None."""
    try:
        row = conn.execute(
            """SELECT close FROM dailybars
               WHERE code=? AND date < ? AND close > 0
               ORDER BY date DESC LIMIT 1 OFFSET ?""",
            (code, today, max(n - 1, 0)),
        ).fetchone()
    except Exception:
        return None
    if not row or not row["close"] or row["close"] <= 0:
        return None
    return float(row["close"])


def _get_caution_count_15d(conn, code: str, today: str) -> int:
    """최근 15거래일 내 투자주의 지정 횟수.

    data-dev 임무 D 미완료 시 disclosures 테이블 fallback (불완전).
    임무 D 완료 후 stock_alert_history 기반으로 교체 권장.
    """
    try:
        # 15 거래일 윈도우 = 약 21 달력일 (주말 포함)
        window_start = (
            datetime.strptime(today, "%Y-%m-%d") - timedelta(days=21)
        ).strftime("%Y-%m-%d")
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM disclosures
               WHERE stock_code=? AND disclosure_cat='투자주의'
               AND date BETWEEN ? AND ?""",
            (code, window_start, today),
        ).fetchone()
        return int(row["n"]) if row and row["n"] else 0
    except Exception:
        return 0


def _compute_entry_threshold(
    conn, code: str, badge_start: str, today: str
) -> dict | None:
    """투자경고 예고 카드용 진입 임계가 산출 (REQ-019 §I).

    Args:
        conn: sqlite3 connection
        code: 종목 코드
        badge_start: 예고 발효 예정일 (YYYY-MM-DD) — 임계가 윈도우 시작일
        today: 오늘 (YYYY-MM-DD) — baseline = today 직전 거래일

    Returns:
        {
          'price': int,                       # path 0~3 min(price)
          'via': str,                         # 채택된 path id
          'and_condition': str,               # 비가격 AND 조건 텍스트
          'window_end': str,                  # badge_start + 10 거래일
          'assumption': str,                  # "KOSPI 횡보 가정"
        }
        또는 None (path 0~3 모두 산출 불가 — dailybars 부족 등)
    """
    candidates: list[dict] = []

    # path 0 — 3일 전 종가 × 2.0
    c3 = _get_close_n_trading_days_ago(conn, code, today, 3)
    if c3 is not None:
        candidates.append(
            {
                "price": int(c3 * 2.0),
                "via": "warn_surge_3d_100pct",
                "and_condition": _ENTRY_PATH_AND_CONDITION["warn_surge_3d_100pct"],
            }
        )

    # path 1 — 5일 전 종가 × 1.6
    c5 = _get_close_n_trading_days_ago(conn, code, today, 5)
    if c5 is not None:
        candidates.append(
            {
                "price": int(c5 * 1.6),
                "via": "warn_surge_5d_60pct",
                "and_condition": _ENTRY_PATH_AND_CONDITION["warn_surge_5d_60pct"],
            }
        )

    # path 2 — 15일 전 종가 × 2.0
    c15 = _get_close_n_trading_days_ago(conn, code, today, 15)
    if c15 is not None:
        candidates.append(
            {
                "price": int(c15 * 2.0),
                "via": "warn_surge_15d_100pct",
                "and_condition": _ENTRY_PATH_AND_CONDITION["warn_surge_15d_100pct"],
            }
        )

    # path 3 — 15일 전 종가 × 1.75 (투자주의 5회 누적 충족 시만 후보군 진입)
    if c15 is not None:
        caution_n = _get_caution_count_15d(conn, code, today)
        if caution_n >= 5:
            candidates.append(
                {
                    "price": int(c15 * 1.75),
                    "via": "warn_repeated_caution",
                    "and_condition": _ENTRY_PATH_AND_CONDITION["warn_repeated_caution"],
                }
            )

    # path 4 — 계좌관여율 (KRX 비공개) → 후보군 진입 0건. 텍스트 첨언만.

    if not candidates:
        return None

    winner = min(candidates, key=lambda c: c["price"])

    # 종료일 — start + 10 거래일 (KRX 휴장일 회피)
    try:
        window_end = _add_trading_days(badge_start, 10)
    except Exception:
        window_end = badge_start

    winner["window_end"] = window_end
    winner["assumption"] = "KOSPI 횡보 가정"
    return winner


def _attach_entry_threshold(conn, badge: dict, code: str, today: str) -> None:
    """투자경고 예고 disclosure 카드에 entry_threshold_* 5필드 부착.

    REQ-019 §V — disclosure 카드는 source='disclosure' + 5 신규 필드.
    path 0~3 모두 산출 불가 시 price/via/and_condition=None, window_end는 항상 산출.
    """
    if badge.get("label") != "투자경고 예고":
        return
    if not badge.get("start"):
        return

    threshold = _compute_entry_threshold(conn, code, badge["start"], today)
    if threshold:
        badge["entry_threshold_price"] = threshold["price"]
        badge["entry_threshold_via"] = threshold["via"]
        badge["entry_threshold_and_condition"] = threshold["and_condition"]
        badge["entry_window_end"] = threshold["window_end"]
        badge["entry_threshold_assumption"] = threshold["assumption"]
    else:
        badge["entry_threshold_price"] = None
        badge["entry_threshold_via"] = None
        badge["entry_threshold_and_condition"] = None
        try:
            badge["entry_window_end"] = _add_trading_days(badge["start"], 10)
        except Exception:
            badge["entry_window_end"] = badge["start"]
        badge["entry_threshold_assumption"] = "KOSPI 횡보 가정"


def _normalize_themes(themes: list) -> list:
    """테마 리스트를 정규화 + 중복 제거."""
    from .theme_normalizer import normalize_list

    return normalize_list(themes)


# ── 카페 테마 병합 (2단계 b, read-time only) ───────────────────────────────
# cafe.db → cafe_theme_mapper 산출 staging(data/cafe-staging/theme-overrides.json)을
# 읽어, 티커별 카페 유래 테마를 종목 themes 리스트에 union한다.
# 서빙 stocks.db 에는 절대 write 하지 않는다(read-time merge only). staging 부재·
# 티커 미스 시 graceful no-op(기존 동작 불변). FLR-AGT-002 거짓충실성 회피 —
# staging 없으면 조용히 빈 인덱스 반환(mock 주입 금지).
CAFE_STAGING_PATH = _REPO_ROOT / "data" / "cafe-staging" / "theme-overrides.json"
_CAFE_THEME_INDEX_CACHE = None  # None=미로드, dict=로드완료(빈 dict 포함)


def _load_cafe_theme_index() -> dict:
    """staging → {ticker: [테마명, ...]} 인덱스. 프로세스 1회 로드 후 캐시.

    canonical 매칭된 테마는 canonical name, 신규후보는 raw 이름(cafe_theme_raw)을 사용.
    ticker 없는(종목 미해석) 항목은 스킵. 파일 부재/파싱실패 시 빈 dict(no-op).
    """
    global _CAFE_THEME_INDEX_CACHE
    if _CAFE_THEME_INDEX_CACHE is not None:
        return _CAFE_THEME_INDEX_CACHE

    idx: dict = {}
    try:
        raw = CAFE_STAGING_PATH.read_text()
        payload = json.loads(raw)
        for it in payload.get("items", []):
            ticker = it.get("ticker")
            if not ticker:
                continue
            theme = it.get("canonical_theme") or it.get("cafe_theme_raw")
            if not theme:
                continue
            bucket = idx.setdefault(ticker, [])
            if theme not in bucket:
                bucket.append(theme)
    except FileNotFoundError:
        idx = {}
    except Exception:
        # 파싱 오류 시에도 기존 동작 보존(카페 병합만 skip).
        idx = {}

    _CAFE_THEME_INDEX_CACHE = idx
    return idx


def _merge_cafe_themes(code: str, themes: list) -> tuple[list, list]:
    """종목 themes(문자열 리스트)에 카페 유래 테마를 union.

    반환: (병합된 themes 문자열 리스트, 실제 추가된 카페 테마 리스트).
    - themes 는 순수 문자열 리스트 유지(프론트 회귀 0 — dict 혼합 안 함).
    - 카페 유래 구분은 반환 2번째 값(추가분)으로 별도 마킹(cafe_themes 필드).
    - 기존 테마와 대소문자·정규화 무관 문자열 dedup.
    """
    cafe_idx = _load_cafe_theme_index()
    cafe_themes = cafe_idx.get(code)
    if not cafe_themes:
        return themes, []

    existing_norm = {_norm_key(t) for t in themes}
    merged = list(themes)
    added = []
    for ct in cafe_themes:
        if _norm_key(ct) in existing_norm:
            continue
        merged.append(ct)
        existing_norm.add(_norm_key(ct))
        added.append(ct)
    return merged, added


def _norm_key(s) -> str:
    """dedup 비교용 정규화 키(공백·구분자 제거·소문자)."""
    if not isinstance(s, str):
        s = str(s)
    return re.sub(r"[\s·ㆍ・\-/&.,]+", "", s.strip().lower())


def _get_theme_path(conn, theme_name: str) -> list:
    """테마의 루트→말단 경로 반환. 예: ['AI', 'AI 인프라', '전력설비', '원전']"""
    row = conn.execute(
        "SELECT id, parent_id FROM themes WHERE name=?", (theme_name,)
    ).fetchone()
    if not row:
        return [theme_name]
    chain = [theme_name]
    current = row
    while current and current["parent_id"]:
        parent = conn.execute(
            "SELECT id, name, parent_id FROM themes WHERE id=?", (current["parent_id"],)
        ).fetchone()
        if not parent:
            break
        chain.append(parent["name"])
        current = parent
    chain.reverse()  # 루트→말단 순서
    return chain


def _get_cumulative_themes(conn, code: str, master) -> list:
    """stock_themes 누적 테마 조회. 최근 테마 우선.

    REQ-076 Phase 4-mini (FLR-TEC-002 §4) — retired_v3 행은 chip 출력 제외.
    V3 재해석 시 stale 테마는 source='retired_v3'로 마킹되며, 라이브 chip에 노출되면
    retire 의미가 무력화된다.
    """
    st_rows = conn.execute(
        """SELECT t.name FROM stock_themes st
           JOIN themes t ON st.theme_id = t.id
           WHERE st.stock_code = ? AND t.is_active = 1
             AND COALESCE(st.source, '') != 'retired_v3'
           ORDER BY st.date_last DESC, st.date_added DESC""",
        (code,),
    ).fetchall()
    return [r["name"] for r in st_rows]


def _get_theme_paths(conn, themes: list) -> list:
    """테마 리스트 → 트리 경로. 부분 경로는 더 구체적 경로에 흡수."""
    raw = []
    for t in themes:
        path = _get_theme_path(conn, t)
        raw.append({"name": t, "path": path})
    # 부분 경로 제거: A의 경로가 B의 경로의 접두사이면 A 제거
    result = []
    for i, a in enumerate(raw):
        is_subset = False
        for j, b in enumerate(raw):
            if i == j:
                continue
            # a의 경로가 b의 경로 앞부분과 일치하면 a는 부분 경로
            if (
                len(a["path"]) < len(b["path"])
                and a["path"] == b["path"][: len(a["path"])]
            ):
                is_subset = True
                break
        if not is_subset:
            result.append(a)
    return result


def load_kiwoom_volume_list(date_str: str = None):
    """키움 종목 리스트 로드 (SC 정책 = Option C, 대표 결정 2026-05-08).

    SC stocks = 영웅문 식 ('500억이상' 조건검색, ka10017 식) ∪ 상한가 union (LU).
    Q-20260513-047 (대표 정책 2026-05-13): ka10032 일상 사용 금지. chg_pct 산출은
    dailybars SoT 1순위 (L2978~ 참조). 외부 ranking 의존 폐기.

    영웅문 식 진입: ka10017 식 close 기준 (B+C+D AND 조건). homepage scripts/kiwoom-scraper
    가 daily_top 적재 → 본 함수가 read.

    LU union (상한가 종목 union)은 호출측 build()에서 stock_status_badges 기반으로
    추가 (line ~2454 `_added_lu`). 본 함수는 daily_top primary list만 반환.

    HEROSHIK STRICT OVERRIDE (2026-05-08, 대표 결정 P0): 특정 일자에 대해
    daily_picks.source='heroshik_strict_<DATE_KEY>' 가 존재하면 해당 source의
    종목으로 primary list 대체 (영웅식 정확 spec: !A NOT KOSPI100 + 5,000억 cap +
    16일 신고가 strict). 5/6 단일 일자 적용. 다른 날 미적용 (override 부재 시
    기존 영웅문식 그대로).

    반환: (stocks_list, source_tag)  source_tag는 'kiwoom' 또는 'heroshik_strict'
    """
    from .config import pipeline_date

    target_date = date_str or pipeline_date()
    kiwoom_dir = HOMEPAGE / "data" / "kiwoom"

    # HEROSHIK STRICT OVERRIDE — daily_picks 의 heroshik_strict_<date_key> source 우선
    # 2026-05-06 단일 일자 (대표 결정 16:17 KST)
    try:
        target_date.replace("-", "_")[2:]  # '2026-05-06' → '26_05_06'
        # 우리는 'heroshik_strict_5_6'를 5/6에 적용. 일반화: target_date에서 'M_D' 추출
        # date_key 형식 확정: 'heroshik_strict_5_6' (앞자리 0 strip 합의 — 5/6 명세 그대로)
        m_d = "_".join(str(int(p)) for p in target_date.split("-")[1:])  # '5_6'
        override_source = f"heroshik_strict_{m_d}"
        with connect() as _conn_h:
            _h_rows = _conn_h.execute(
                """SELECT stock_code, rank, trade_amount, change_pct, price, high_price
                   FROM daily_picks WHERE date=? AND source=?
                   ORDER BY trade_amount DESC""",
                (target_date, override_source),
            ).fetchall()
        if _h_rows:
            print(
                f"[load_kiwoom] HEROSHIK STRICT OVERRIDE active: "
                f"date={target_date} source={override_source} count={len(_h_rows)}"
            )
            stocks = []
            for r in _h_rows:
                stocks.append(
                    {
                        "ticker": r["stock_code"],
                        "name": "",
                        "last_price": r["price"],
                        "max_trade_amount": r["trade_amount"],
                        "trade_amount": r["trade_amount"],
                        "max_change_pct": None,
                        "change_pct": r["change_pct"],
                        "appearances": 1,
                        "first_seen": "",
                        "last_seen": "",
                        "open": None,
                        "high": r["high_price"],
                        "low": r["price"],
                        "min_change_pct": None,
                        "_source_union": f"daily_picks:{override_source}",
                    }
                )
            return stocks, "heroshik_strict"
    except Exception as _e_h:
        print(f"[load_kiwoom] heroshik strict override skip (오류): {_e_h}")

    # 1) 조건검색 결과
    # Q-20260513-050 (대표 결정 2026-05-13 23:55 KST): primary source
    # = latest_stocks (마지막 snapshot 25종). 키움 HTS '500억이상' 정합.
    # 기존 daily_top (일일 누적 거래대금 max 50종) → 라이브 종목수 절반 감소,
    # 마지막 snapshot 시점 미선정 종목 (예: 5/13 010170 대한광통신 — 00:22
    # 1회 등장 후 21:33 시점 제외) 자연 배제. fallback: daily_top → stocks
    # (legacy snapshot 호환).
    date_file = kiwoom_dir / f"{target_date}.json"
    if date_file.exists():
        data = json.loads(date_file.read_text())
        stocks = (
            data.get("latest_stocks")
            or data.get("daily_top")
            or data.get("stocks")
            or []
        )
        if stocks:
            return stocks, "kiwoom"

    # 2) 폴백: latest.json — 날짜가 일치할 때만
    latest = kiwoom_dir / "latest.json"
    if latest.exists():
        data = json.loads(latest.read_text())
        latest_date = data.get("date", "")
        if latest_date == target_date:
            stocks = data.get("stocks", [])
            if stocks:
                return stocks, "kiwoom"
        else:
            print(
                f"[load_kiwoom] latest.json date={latest_date} != target={target_date}"
                " — 날짜 불일치, 폴백 거부"
            )

    # kiwoom_ranking 폴백 제거됨 — 조건검색 없으면 빈 리스트 (대표 지시)
    return [], "kiwoom"


def _load_prev_day_stocks(today_str: str):
    """전일 stock-*.json에서 종목별 뉴스 로드 (겹치는 종목 재활용)."""
    for i in range(1, 8):
        prev = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=i)).strftime(
            "%Y-%m-%d"
        )
        prev_path = OUT_DIR / f"stock-{prev}.json"
        if prev_path.exists():
            try:
                data = json.loads(prev_path.read_text())
                by_code = {}
                for s in data.get("stocks", []):
                    if s.get("news") and not all(
                        n.get("newzy_verdict", "").startswith("반대") for n in s["news"]
                    ):
                        by_code[s["code"]] = s
                return by_code, prev
            except Exception:
                continue
    return {}, None


def _dedup_news(news_list: list[dict]) -> list[dict]:
    """URL 또는 제목 기준 뉴스 중복 제거. 먼저 등장한 것 유지."""
    seen: set[str] = set()
    result = []
    for n in news_list:
        key = n.get("url") or n.get("title") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(n)
    return result


def fetch_fallback(code: str, prev_stocks: dict, prev_date: str):
    """폴백 체인: 오늘 뉴스 → 전일 겹치는 종목 뉴스 → 최근 DB 뉴스 → 마스터 DB."""
    from .config import pipeline_date

    today = pipeline_date()
    with connect() as conn:
        cols = """title, url, source, published_at, causal_chain, macro_event,
                 newzy_freshness, newzy_persistence, newzy_magnitude,
                 newzy_virality, newzy_tradability, newzy_score, newzy_verdict,
                 article_type"""
        # FLR-20260529-TEC-001: article_type 호재 우선 정렬 추가. LG전자 급등일에
        # 시황/사건사고(흉기난동)가 시간순으로 상위에 와서 호재(젠슨 황 회동·피지컬 AI·
        # 로봇 신고가)가 카드 5건에서 밀리던 문제. 호재(0) > 공시(1) > 시세정형/NULL(2) >
        # 사건사고/악재(3) 순으로 정렬키 추가 → 급등 사유 호재가 카드 상위 노출.
        today_news = conn.execute(
            f"""SELECT {cols} FROM news WHERE stock_code=? AND date(published_at)=?
               AND COALESCE(is_robot, 0) = 0
               ORDER BY (CASE WHEN causal_chain IS NULL OR TRIM(causal_chain)=''
                              OR causal_chain LIKE '%식별%불가%'
                              OR causal_chain LIKE '%식별할 수 없%'
                              OR causal_chain LIKE '%식별되지 않%'
                              OR causal_chain LIKE '%형성되지 않%' THEN 1 ELSE 0 END),
                        (CASE article_type
                              WHEN '호재' THEN 0 WHEN '공시' THEN 1
                              WHEN '악재' THEN 3 WHEN '사건사고' THEN 3
                              ELSE 2 END),
                        newzy_score DESC, published_at DESC LIMIT 5""",
            (code, today),
        ).fetchall()
        if today_news:
            return {
                "news": _dedup_news([dict(r) for r in today_news]),
                "fallback": False,
            }

        # 전일 데이터에 같은 종목이 있으면 재활용 (대표 지시 4/9)
        if code in prev_stocks:
            prev = prev_stocks[code]
            return {
                "news": _dedup_news(prev.get("news", [])),
                "fallback": True,
                "fallback_date": prev_date,
            }

        recent = conn.execute(
            f"""SELECT {cols} FROM news WHERE stock_code=?
               AND COALESCE(is_robot, 0) = 0
               ORDER BY published_at DESC LIMIT 5""",
            (code,),
        ).fetchall()
        if recent:
            return {"news": _dedup_news([dict(r) for r in recent]), "fallback": True}

        # 마스터 DB 활용 — industry/sector/themes 최대한 노출 (대표 지시 4/9)
        stock = conn.execute(
            "SELECT industry, sector FROM stocks WHERE code=?", (code,)
        ).fetchone()
        # stock_themes에서 테마 조회 (REQ-076 Phase 4-mini — retired_v3 제외)
        theme_rows = conn.execute(
            """SELECT t.name FROM stock_themes st
               JOIN themes t ON st.theme_id = t.id
               WHERE st.stock_code = ? AND t.is_active = 1
                 AND COALESCE(st.source, '') != 'retired_v3'""",
            (code,),
        ).fetchall()
        return {
            "news": [],
            "fallback": True,
            "themes": [r["name"] for r in theme_rows],
            "industry": stock["industry"] if stock else None,
            "sector": stock["sector"] if stock else None,
        }


def _sync_sw_cache_name() -> None:
    """REQ-024 §1.5 — sw.js의 CACHE_NAME을 news.html의 utils.js 버전 토큰과 동기화.

    회귀 방지: REQ-014~023 동안 sw.js CACHE_NAME='news-v5'로 정체된 채
    stale-while-revalidate가 첫 진입 사용자에게 폐기된 enum/UI를 그대로 노출.
    매 빌드마다 자동 동기화하여 누락 차단.

    추출: news.html의 `<script src="/js/utils.js?v=YYYYMMDDvNN" ...>` 토큰의 vNN 부분
    치환: sw.js의 `const CACHE_NAME = 'news-vNN';`

    DOC-20260504-PLAN-001 §3.4 — 다운그레이드 차단 보강 (FLR-20260504-FLR-003 recurring 영구):
    - sw.js v 가 news.html v 보다 높으면 (lead/DevOps 가 sw.js 를 먼저 bump 한 경우)
      news.html 을 sw.js v 로 upgrade. sw.js 는 절대 다운그레이드하지 않음.
    - 라이브 sw.js 는 SSOT (build 산출물 < 라이브 산출물 인 경우 build 가 라이브를 덮어씌우면 안 됨).
    """

    news_html = HOMEPAGE / "news.html"
    sw_js = HOMEPAGE / "sw.js"
    if not news_html.exists() or not sw_js.exists():
        return

    html = news_html.read_text()
    m_news = re.search(r"/js/utils\.js\?v=\d+v(\d+)", html)
    if not m_news:
        print("[sync_sw] WARNING: utils.js 버전 토큰 추출 실패 — 동기화 스킵")
        return
    news_v = int(m_news.group(1))

    sw = sw_js.read_text()
    m_sw = re.search(r"const CACHE_NAME = 'news-v(\d+)';", sw)
    if not m_sw:
        print("[sync_sw] WARNING: sw.js의 CACHE_NAME 라인 매칭 실패 — 동기화 스킵")
        return
    sw_v = int(m_sw.group(1))

    # 다운그레이드 차단 — sw.js v > news.html v 면 news.html 을 sw.js 로 upgrade.
    if sw_v > news_v:
        target_v = sw_v
        target_token_v = sw_v
        # news.html 의 ?v=YYYYMMDDvNN 의 vNN 만 치환 (앞의 YYYYMMDD 보존).
        new_html, nh = re.subn(
            r"(/js/utils\.js\?v=\d+v)\d+",
            rf"\g<1>{target_token_v}",
            html,
        )
        if nh == 0:
            print(
                f"[sync_sw] WARNING: news.html upgrade 시 토큰 치환 실패 — sw.js v{sw_v} 유지, 동기화 스킵"
            )
            return
        news_html.write_text(new_html)
        print(
            f"[FLR-003 sw.js-guard] news.html v{news_v} < sw.js v{sw_v} → "
            f"news.html upgraded to v{target_token_v} (sw.js 다운그레이드 차단)"
        )
        return

    # 정상 경로: news.html v >= sw.js v → sw.js 를 news.html v 로 동기화 (upgrade or no-op).
    target_v = news_v
    target_cache = f"news-v{target_v}"
    new_sw, n = re.subn(
        r"const CACHE_NAME = 'news-v\d+';",
        f"const CACHE_NAME = '{target_cache}';",
        sw,
        count=1,
    )
    if n == 0:
        print("[sync_sw] WARNING: sw.js의 CACHE_NAME 라인 매칭 실패 (재) — 동기화 스킵")
        return
    if new_sw == sw:
        return
    sw_js.write_text(new_sw)
    print(f"[sync_sw] CACHE_NAME → '{target_cache}'")


def rebuild_calendar_index() -> dict:
    """calendar/index.json 의 days 맵을 클릭 콘텐츠 SSOT 기준 재구성 (멱등·union 보존).

    종전 `_update_calendar_index(today, …)` 는 매 실행 `days[today]` 한 칸만
    기록 → 과거 일자를 재구성하지 않아, 어떤 사유로든 한 번 누락된 거래일
    (예: cron/homepage worktree divergence, env 분기, 신규 cron worktree 초기화)
    은 영구 공백으로 남았다 (FLR-20260428-TEC-001 한쪽 수정·다른 끝 누락 동형 /
    FLR-AGT-002 false-fidelity — 데이터는 정상 존재하나 index 누락으로 화면 비활성).

    **SSOT = `data/interpreted/stock-{date}.json` 의 `stocks[]` 개수** (= 달력
    날짜 클릭 시 로드되는 per-date 콘텐츠, `data-loader.js` fetch 경로). `stock_count`
    = 해당 날짜 카드 표시 유니버스 크기 (예 4/8=29). **daily_picks distinct(4 같은
    축소 윈도) 가 아니다** — cron daily_picks 는 최근 N일만 보존하므로 그것을 SSOT
    로 잡으면 과거 거래일이 드롭된다 (본 fix 의 1차 회귀 ROOT, 2026-06-22 19:20 catch).

    **union 보존 (드롭 절대 금지)**: 기존 index 의 days 를 base 로 깔고, interpreted
    로 발견한 날짜만 (interpreted 우선) 덮어쓴다. interpreted 가 못 채운 날짜(예 구
    일자 interpreted 파일 정리됨)는 **기존 index 값 그대로 유지** → 기존 stock_count>0
    날짜는 결과에 반드시 포함. interpreted JSON 파싱 실패는 graceful skip (mock 생성
    금지, 기존 값 보존 — FLR-AGT-002).

    news_count 는 news 테이블 published_at date 기준 보강값 (표시용). 클릭 게이트는
    frontend `calHasData()` 가 stock_count 단독 판정 (DSN-arch-frontend §3.5.1).

    days 스키마는 종전과 동일 (`stock_count` / `news_count` / `activity`).
    Returns: 기록한 days 맵 (검증·로그용).
    """
    import glob

    CAL_INDEX.parent.mkdir(parents=True, exist_ok=True)
    idx = {}
    if CAL_INDEX.exists():
        try:
            idx = json.loads(CAL_INDEX.read_text())
        except Exception:
            idx = {}

    # base = 기존 days (보존 merge 의 출발점 — 절대 드롭 금지).
    base_days = idx.get("days", {})
    if not isinstance(base_days, dict):
        base_days = {}

    # SSOT: interpreted stock-{date}.json 날짜별 stocks 개수 (클릭 콘텐츠 유니버스).
    interp_counts: dict[str, int] = {}
    pat = re.compile(r"stock-(\d{4}-\d{2}-\d{2})\.json$")
    for fpath in glob.glob(str(OUT_DIR / "stock-*.json")):
        m = pat.search(fpath)
        if not m:
            continue
        d = m.group(1)
        try:
            data = json.loads(_Path(fpath).read_text())
            stocks = data.get("stocks")
            if isinstance(stocks, list):
                interp_counts[d] = len(stocks)
        except Exception:
            # 파싱 실패 → 기존 base 값 보존 (mock 생성 금지, FLR-AGT-002).
            continue

    # news 날짜별 건수 (published_at date 기준). 표시용 보강값.
    news_counts: dict[str, int] = {}
    with connect() as conn:
        for row in conn.execute(
            "SELECT date(published_at) AS d, COUNT(*) AS n "
            "FROM news WHERE published_at IS NOT NULL "
            "GROUP BY date(published_at)"
        ).fetchall():
            d = row["d"]
            if d:
                news_counts[str(d)] = int(row["n"] or 0)

    # union 보존 merge: 기존 days 전부 유지 + interpreted 발견 날짜는 갱신/추가.
    days: dict[str, dict] = {}
    all_dates = set(base_days) | set(interp_counts)
    for d in sorted(all_dates):
        prev = base_days.get(d) if isinstance(base_days.get(d), dict) else {}
        # stock_count: interpreted SSOT 우선, 없으면 기존 값 보존 (드롭 0).
        sc = interp_counts.get(d)
        if sc is None:
            sc = int(prev.get("stock_count") or 0)
        # news_count: news 테이블 우선(신선), 없으면 기존 값 보존.
        nc = news_counts.get(d)
        if nc is None:
            nc = int(prev.get("news_count") or 0)
        days[d] = {
            "stock_count": sc,
            "news_count": nc,
            "activity": sc,
        }

    idx["days"] = days
    idx["last_updated"] = datetime.now().isoformat()
    CAL_INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2))
    return days


def _update_calendar_index(today: str, stock_count: int, news_count: int):
    """calendar/index.json 갱신 — 캘린더 활성도에 반영.

    종전 today 단일 칸 갱신 → DB 기준 전 거래일 full rebuild 로 전환 (멱등,
    누락 자동 복구). 인자 ``today``/``stock_count``/``news_count`` 는 호출부
    호환을 위해 유지하되, days 는 DB 를 단일 출처로 재구성한다 (인자 무시).
    호출 시점(L4329)에 ``_save_daily_picks``(L3207)가 이미 today 픽을 적재한
    뒤이므로 today 도 DB rebuild 에 자동 포함된다.
    """
    rebuild_calendar_index()


def _save_daily_picks(today: str, kiwoom_stocks: list, source: str = "kiwoom"):
    """오늘의 종목을 daily_picks 테이블에 저장 + stocks.pick_dates_json 갱신."""
    # 휴장일 방어: _save_daily_picks를 직접 호출하는 경우에도 거래일 검증
    if is_market_closed(today):
        print(f"[_save_daily_picks] {today} 은(는) 휴장일 — 적재 거부")
        return
    now = datetime.now().isoformat()
    with connect() as conn:
        # daily_picks 테이블 존재 확인 (없으면 생성)
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, stock_code TEXT NOT NULL,
            rank INTEGER, trade_amount INTEGER, change_pct REAL,
            price INTEGER, open_price REAL, high_price REAL, low_price REAL,
            source TEXT DEFAULT 'kiwoom', created_at TEXT NOT NULL,
            UNIQUE(date, stock_code, source))""")
        # 기존 테이블에 OHLC 컬럼이 없으면 추가 (ALTER TABLE)
        dp_cols = [
            r[1] for r in conn.execute("PRAGMA table_info(daily_picks)").fetchall()
        ]
        for col in ("open_price", "high_price", "low_price"):
            if col not in dp_cols:
                conn.execute(f"ALTER TABLE daily_picks ADD COLUMN {col} REAL")
        # pick_dates_json 컬럼 확인
        cols = [r[1] for r in conn.execute("PRAGMA table_info(stocks)").fetchall()]
        if "pick_dates_json" not in cols:
            conn.execute(
                "ALTER TABLE stocks ADD COLUMN pick_dates_json TEXT DEFAULT '[]'"
            )

        # ETF/펀드/채권 필터: 종목코드가 숫자 6자리가 아닌 것 제외
        import re

        _CODE_RE = re.compile(r"^\d{6}$")

        # [JSON-GUARD] dailybars 테이블 존재 여부 사전 확인.
        # cron 서빙 DB(데이터 레이어 분리 미구현 단계)는 dailybars 테이블이 없어
        # OperationalError 크래시 → daily_picks 적재 전체 사망. 1회 체크로 graceful.
        _dailybars_available = bool(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='dailybars'"
            ).fetchone()
        )
        if not _dailybars_available:
            print(
                "[_save_daily_picks] dailybars 테이블 부재 — "
                "dailybars OHLC/cp/ta fallback 경로 SKIP (D축 graceful)"
            )

        for i, k in enumerate(kiwoom_stocks):
            code = k.get("code") or k.get("ticker")
            if not code:
                continue
            # 비주식 종목 필터링 (ETF/펀드/채권 — 종목코드가 순수 숫자 6자리가 아님)
            if not _CODE_RE.match(code):
                continue
            # FLR-20260507-TEC (cross-source 2nd) — change_pct/trade_amount SSOT 통일
            #   결함: max_change_pct (장중 high 기준 +29.x%) + max_trade_amount fallback이
            #     LU(limit-up-trend) close 기준과 mismatch → 49건 cross-source 결함.
            #   정책 (LU 정합 b800ef9 + a4adf16):
            #     change_pct = close 기준만 (max_change_pct fallback 제거).
            #       dailybars.close + LAG(close) 직접 계산으로 fallback.
            #     trade_amount = max_trade_amount fallback 후 dailybars.trade_amount fallback 추가.
            ta = k.get("trade_amount") or k.get("max_trade_amount")
            cp = k.get("change_pct")
            # close 기준 직접 계산 (k에 close가 있으면)
            # Q-THEME-TREND-CHANGE-PCT-ZERO-FIX (2026-05-12): cp == 0 도 fallback 발동.
            # 장 시작 전 source(latest.json fetched_at 미갱신, 또는 다른 union source)에서
            # cp=0 으로 들어오는 경우 dailybars 전일 close 기반 산출. FLR-20260507-TEC
            # 정책 정합 (close 기준만, max_change_pct/장중 fallback 금지).
            close_today = k.get("price") or k.get("last_price") or k.get("close")
            if (cp is None or cp == 0) and close_today:
                # Q-20260527-OG-MISMATCH (2026-05-27): cp fallback 2 layer fix.
                #
                # ROOT (5/27 23종 mismatch >1.0%p, 23/55=42% catch):
                #   1) 기존 안전판 `close != close_today` 는 dailybars 휴장일/가짜 복사본
                #      행 회피 의도였으나 collect_dailybars 는 휴장일 picks 자체를 0건으로
                #      유지(`is_market_holiday(today)` 분기, §7.2.3 정합) → 휴장일 row 적재
                #      자체가 부재. 안전판 = phantom 결함.
                #   2) kiwoom k.get("price") snapshot stale (장중 폴링이 직전일 종가 그대로
                #      반환) 시 close_today = stale prev close 값 → 안전판이 immediate prev
                #      row 를 SKIP → 그 이전 거래일 close 를 잘못된 prev 로 채택 → cp 가
                #      +29.82% 등 허위 박제 cascade. 5/27 sample 052710 / 062970 / 195870 /
                #      000990 / 295310 / 330860 / 064290 / 036540 등 23종.
                #   대표 5/27 22:48 KST verbatim "전체로직을 점검하도록" 정합.
                #
                # Fix 2 layer (Layer1 + Layer2 동시):
                #   Layer1 (close_today SoT 통일): dailybars 의 today close 가 존재하면
                #     stale-snapshot kiwoom k.price 보다 우선 사용. Fix B (Q-DAILYPICKS-OHLC-
                #     DAILYBARS-OVERRIDE 2026-05-13) OHLC override 와 동일한 SSOT 통일
                #     원칙을 cp fallback 분기에도 일관 적용. 단 Fix C 의 stale 4값 동일
                #     검사 후 dailybars override 만 채택 (점상 LU/LD 면제 동형).
                #   Layer2 (안전판 제거): immediate prev_close 직접 사용. Layer1 통과 후
                #     close_today = 실제 today close 이므로 immediate prev 비교가 정합
                #     (안전판 = phantom). Q-20260515-BUILD-DAILY-ADJ-RATIO 권리락 ratio
                #     보정 유지 (raw_prev × ratio = adj_prev).
                if _dailybars_available:
                    _cp_db_today = conn.execute(
                        "SELECT open, high, low, close FROM dailybars WHERE code=? AND date=?",
                        (code, today),
                    ).fetchone()
                else:
                    _cp_db_today = None
                if _cp_db_today and _cp_db_today["close"]:
                    _o2 = _cp_db_today["open"] or 0
                    _h2 = _cp_db_today["high"] or 0
                    _lo2 = _cp_db_today["low"] or 0
                    _c2 = _cp_db_today["close"] or 0
                    _db_stale = False
                    if (
                        _o2 > 0
                        and _h2 > 0
                        and _lo2 > 0
                        and _c2 > 0
                        and _o2 == _h2 == _lo2 == _c2
                    ):
                        # Fix C 동형: OHLC 4값 동일은 LU/LD 점상이 아니면 cur_prc stale.
                        _k_chg = k.get("change_pct")
                        _is_lp = _k_chg is not None and abs(float(_k_chg)) >= 29.0
                        if not _is_lp:
                            _db_stale = True
                    if not _db_stale:
                        close_today = float(_cp_db_today["close"])
                # Layer2: immediate prev_close (안전판 제거)
                if _dailybars_available:
                    prev_db = conn.execute(
                        """SELECT close AS raw_close, date AS prev_date FROM dailybars
                           WHERE code=? AND date<? AND close>0
                           ORDER BY date DESC LIMIT 1""",
                        (code, today),
                    ).fetchone()
                else:
                    prev_db = None
                if prev_db and prev_db["raw_close"]:
                    _adj = conn.execute(
                        """SELECT ratio FROM dailybars_adjustments
                           WHERE code=? AND date > ? AND date <= ?
                           ORDER BY date DESC LIMIT 1""",
                        (code, prev_db["prev_date"], today),
                    ).fetchone()
                    _ratio = float(_adj["ratio"]) if _adj and _adj["ratio"] else 1.0
                    _adj_prev = float(prev_db["raw_close"]) * _ratio
                    cp = round(
                        (float(close_today) - _adj_prev) / _adj_prev * 100,
                        2,
                    )
            # dailybars.trade_amount fallback (LU 정합)
            if ta is None and _dailybars_available:
                db_ta = conn.execute(
                    "SELECT trade_amount FROM dailybars WHERE code=? AND date=?",
                    (code, today),
                ).fetchone()
                if db_ta and db_ta["trade_amount"]:
                    ta = db_ta["trade_amount"]
            # Fix B (2026-05-13, Q-DAILYPICKS-OHLC-DAILYBARS-OVERRIDE):
            #   장 종료 후 KRX 정식 일봉(dailybars) 적재가 완료되면 OHLC SSOT 통일.
            #   kiwoom k(open/high/low/price)가 stale (장중 마지막 폴링 스냅샷 또는
            #   intraday cached) 인 경우가 있어 daily_picks 가 dailybars 와 mismatch.
            #   dailybars 존재 시 override, 미존재(장중) 시 kiwoom k 유지 (장중 fallback).
            #   close=price, open/high/low=dailybars 정합.
            # Fix C (2026-05-13, Q-20260513-030 stale carryover catch):
            #   dailybars row의 OHLC 4값이 모두 동일하면 키움이 장중·봉 미확정 시점에
            #   cur_prc로만 응답한 stale row (collect_dailybars Fix-2 commit 2d25aef
            #   이전 적재 흔적). 정상 OHLC 봉이 아니므로 dailybars override skip 후
            #   kiwoom k 유지. 본 stale row는 collect_dailybars 후속 cron에서 키움
            #   정상 OHLC 반환 시 INSERT OR REPLACE 로 자동 갱신.
            #   대표 catch (010170 5/13): dailybars 29100/29100/29100/29100 (장중 stale)
            #     vs 키움 종가 24750 (+14.95%). dailybars SoT 우선이 stale 카드 노출.
            if _dailybars_available:
                db_ohlc = conn.execute(
                    "SELECT open, high, low, close FROM dailybars WHERE code=? AND date=?",
                    (code, today),
                ).fetchone()
            else:
                db_ohlc = None
            price_v = k.get("price") or k.get("last_price")
            open_v, high_v, low_v = k.get("open"), k.get("high"), k.get("low")
            # Fix C: OHLC 4값 동일 stale 검사 (collect_dailybars L331 동일 패턴)
            # Q-20260519-CYCLE12-JOLSS (2026-05-19): LU/LD 점상 stale 오판 면제.
            #   change_pct ±29.0% 이상 (KRX 한국 상하한가 ±30%, 호가 단위 보정 후
            #   29.0~30.0) 시 OHLC 4값 동일은 정합 점상 (장 마감 후 정상 일봉).
            #   Fix C (commit feaa6f4 2026-05-13)가 LU 점상까지 stale 오판 → SoT
            #   override skip → open_price=NULL 적재 결함 (졸스 018700 5/19 catch).
            #   대표 본질 룰 (2026-05-19 17:30): "특정 종목 hard-code 금지, 전체
            #   로직 반영" — 전 LU/LD 종목 일관 처리.
            db_ohlc_is_stale = False
            if db_ohlc and db_ohlc["close"]:
                _o = db_ohlc["open"] or 0
                _h = db_ohlc["high"] or 0
                _lo = db_ohlc["low"] or 0
                _c = db_ohlc["close"] or 0
                if _o > 0 and _h > 0 and _lo > 0 and _c > 0 and _o == _h == _lo == _c:
                    # LU/LD 점상 면제 (정합 데이터). 그 외 cur_prc fallback = stale.
                    _k_chg = k.get("change_pct")
                    _is_limit_point = _k_chg is not None and abs(float(_k_chg)) >= 29.0
                    if not _is_limit_point:
                        db_ohlc_is_stale = True
            if db_ohlc and db_ohlc["close"] and not db_ohlc_is_stale:
                # dailybars override (장 종료 후 KRX 정식 일봉 정합)
                price_v = db_ohlc["close"]
                open_v = db_ohlc["open"] if db_ohlc["open"] else open_v
                high_v = db_ohlc["high"] if db_ohlc["high"] else high_v
                low_v = db_ohlc["low"] if db_ohlc["low"] else low_v
                # Q-20260527-OG-MISMATCH (2026-05-27) Layer3: cp SSOT 동시 통일.
                #   기존 Fix B (Q-DAILYPICKS-OHLC-DAILYBARS-OVERRIDE) 는 OHLC 만 override,
                #   cp 는 kiwoom k.get("change_pct") snapshot 유지 → kiwoom 폴링이 직전일
                #   stale snapshot (price=직전일 종가) 인 경우 cp 도 동일 stale (직전일 cp
                #   값 또는 0) → daily_picks.change_pct 가 dailybars close 와 무관한 stale
                #   값으로 박제 cascade. 5/27 sample 062970 dp_cp=29.91 vs dailybars cp=-16.57.
                #   Layer3 = dailybars override 시 cp 도 dailybars close + immediate prev
                #   기반 재산출 (Q-20260515-BUILD-DAILY-ADJ-RATIO 권리락 ratio 보정 유지).
                #   cp=0/None 분기 (Layer1+Layer2) 와 본 분기는 mutually exclusive 입력
                #   조건 (cp != 0 + dailybars 정합 today close 존재) → 양 분기 중복 0건.
                _cp_prev_db = conn.execute(
                    """SELECT close AS raw_close, date AS prev_date FROM dailybars
                       WHERE code=? AND date<? AND close>0
                       ORDER BY date DESC LIMIT 1""",
                    (code, today),
                ).fetchone()
                if _cp_prev_db and _cp_prev_db["raw_close"]:
                    _cp_adj = conn.execute(
                        """SELECT ratio FROM dailybars_adjustments
                           WHERE code=? AND date > ? AND date <= ?
                           ORDER BY date DESC LIMIT 1""",
                        (code, _cp_prev_db["prev_date"], today),
                    ).fetchone()
                    _cp_ratio = (
                        float(_cp_adj["ratio"]) if _cp_adj and _cp_adj["ratio"] else 1.0
                    )
                    _cp_adj_prev = float(_cp_prev_db["raw_close"]) * _cp_ratio
                    cp = round(
                        (float(price_v) - _cp_adj_prev) / _cp_adj_prev * 100,
                        2,
                    )
            # Q-20260519-CYCLE12-JOLSS-V2 (2026-05-19): open/high/low NULL fallback.
            #   kiwoom k 캐시 + dailybars 양쪽 모두 NULL 인 경우 price_v (close) 로
            #   fallback. close가 있다는 것은 = 정합 점상 (open=high=low=close 동일치)
            #   또는 단순 stale 누락. miniCandle '|' 표시 봉쇄 (전 종목 룰).
            #   대표 본질 룰 (2026-05-19 17:30+17:43): "특정 종목 hard-code 금지,
            #   전체 로직 반영, 전 종목·전 날짜 개선." 졸스 018700 5/19 catch.
            if price_v:
                if not open_v:
                    open_v = price_v
                if not high_v:
                    high_v = price_v
                if not low_v:
                    low_v = price_v
            conn.execute(
                """INSERT INTO daily_picks(date, stock_code, rank, trade_amount, change_pct,
                     price, open_price, high_price, low_price, source, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(date, stock_code, source) DO UPDATE SET
                     rank=excluded.rank, trade_amount=excluded.trade_amount,
                     change_pct=excluded.change_pct, price=excluded.price,
                     open_price=excluded.open_price, high_price=excluded.high_price,
                     low_price=excluded.low_price""",
                (
                    today,
                    code,
                    k.get("rank", i + 1),
                    ta,
                    cp,
                    price_v,
                    open_v,
                    high_v,
                    low_v,
                    source,
                    now,
                ),
            )
            # 신규 종목 자동 마스터 등록 hook (P0 본질 fix 2026-05-11)
            # daily_picks 진입 종목이 stocks 마스터에 없으면 scraper 제공 name 으로
            # 최소 row 삽입. 누락 시 collect_dailybars UPDATE 가 0 rows 처리되어
            # price_high_240d 영구 NULL → range_240d UI "--" 결함 (FLR-20260511 root B).
            # 후속 seed_master_for_today.py 파이프라인 스테이지가 KIND lookup 으로
            # market/industry/sector 보강 + 240d backfill 실행.
            stock_name = k.get("name") or k.get("stock_name")
            if stock_name:
                conn.execute(
                    """INSERT INTO stocks(code, name, last_updated, pick_dates_json)
                       VALUES(?, ?, ?, '[]')
                       ON CONFLICT(code) DO NOTHING""",
                    (code, str(stock_name).strip(), now),
                )
            # pick_dates_json 갱신
            row = conn.execute(
                "SELECT pick_dates_json FROM stocks WHERE code=?", (code,)
            ).fetchone()
            if row:
                dates = json.loads(row["pick_dates_json"] or "[]")
                if today not in dates:
                    dates.append(today)
                    dates.sort()
                    conn.execute(
                        "UPDATE stocks SET pick_dates_json=? WHERE code=?",
                        (json.dumps(dates), code),
                    )
        conn.commit()


def _reconcile_daily_picks_with_dailybars(today: str, source: str = "kiwoom"):
    """daily_picks(today) 전건을 dailybars SoT 기준으로 재계산 후 UPDATE.

    Q-20260527-OG-MISMATCH-INPUT-LAYER (2026-05-28 옵션 A 채택, sub-agent
    a68fd61 권고 정합, 대표 5/28 00:10 KST 결정):

    배경:
      _save_daily_picks 의 Fix B/C/Layer1~3 (L2156~L2310) 가 동작하는 분기는
      "Layer1 통과 (dailybars today close 존재 + non-4eq-stale)" 이지만,
      kiwoom k snapshot 이 직전일 stale 값으로 들어오고 _save_daily_picks
      호출 직전에 dailybars(today) 가 미적재된 종목은 Fix B/C 분기를 통과
      하지 못해 stale cp/price 가 daily_picks 에 박제됨. 5/27 case 에서 18
      종목 (069540/403870/036540/000990/347700/222800/006260/178320/295310/
      005290/062970/078600/064290/031330/330860/195870/095610/308080) cp
      mismatch >1.0%p (max +46.48%p 062970 dp=29.91 vs db=-16.57) 발생.

    Fix (옵션 A — _save_daily_picks 직후 reconciliation pass 1회):
      build_daily 사이클 끝(또는 _save_daily_picks 호출 직후) dailybars
      SoT 기준 daily_picks(today) 전건 UPDATE.
      - source = 'kiwoom' 만 reconcile (heroshik_strict_* 등 다른 source 제외)
      - dailybars(today) 부재 시 skip (장중 stale 잔존, post-cron 봉쇄)
      - 4eq stale 시 skip (Fix C 동형 가드)
      - LU/LD 점상 면제 (k.change_pct ±29.0% 이상 + 4eq → 정합 점상 유지)
      - immediate prev_close (안전판 제거, Layer2 동형) + adjustments ratio
        보정 (Q-20260515-BUILD-DAILY-ADJ-RATIO 정합)

    부작용 0건 보증:
      - WHERE date=today 한정 → 다른 일자 영향 0건
      - source=? 한정 → heroshik_strict_* 등 별 source 영향 0건
      - dailybars 부재 시 skip → 장중/post-stale 잔존 봉쇄
      - cp/price/open/high/low/trade_amount 만 UPDATE (rank/source/created_at 미변경)
    """
    if is_market_closed(today):
        print(f"[_reconcile_daily_picks_with_dailybars] {today} 휴장일 — skip")
        return

    updated = 0
    skipped_no_db = 0
    skipped_stale = 0
    unchanged = 0

    with connect() as conn:
        rows = conn.execute(
            """SELECT id, stock_code, change_pct, price, open_price, high_price,
                      low_price, trade_amount
               FROM daily_picks
               WHERE date=? AND source=?""",
            (today, source),
        ).fetchall()

        for row in rows:
            code = row["stock_code"]
            db_today = conn.execute(
                """SELECT open, high, low, close, trade_amount
                   FROM dailybars WHERE code=? AND date=?""",
                (code, today),
            ).fetchone()
            if not db_today or not db_today["close"]:
                skipped_no_db += 1
                continue

            _o = db_today["open"] or 0
            _h = db_today["high"] or 0
            _lo = db_today["low"] or 0
            _c = db_today["close"] or 0

            # 4eq stale 가드 (Fix C 동형). LU/LD 점상은 면제.
            if _o > 0 and _h > 0 and _lo > 0 and _c > 0 and _o == _h == _lo == _c:
                cur_cp = row["change_pct"]
                _is_limit_point = cur_cp is not None and abs(float(cur_cp)) >= 29.0
                if not _is_limit_point:
                    skipped_stale += 1
                    continue

            # immediate prev_close + adjustments ratio 보정
            prev_db = conn.execute(
                """SELECT close AS raw_close, date AS prev_date FROM dailybars
                   WHERE code=? AND date<? AND close>0
                   ORDER BY date DESC LIMIT 1""",
                (code, today),
            ).fetchone()
            if not prev_db or not prev_db["raw_close"]:
                skipped_no_db += 1
                continue

            _adj = conn.execute(
                """SELECT ratio FROM dailybars_adjustments
                   WHERE code=? AND date > ? AND date <= ?
                   ORDER BY date DESC LIMIT 1""",
                (code, prev_db["prev_date"], today),
            ).fetchone()
            _ratio = float(_adj["ratio"]) if _adj and _adj["ratio"] else 1.0
            _adj_prev = float(prev_db["raw_close"]) * _ratio
            new_cp = round((float(_c) - _adj_prev) / _adj_prev * 100, 2)
            new_ta = db_today["trade_amount"] or row["trade_amount"]

            # 변경 필요 여부 판단 (cp / OHLC / trade_amount 중 1건이라도 diff)
            cur_cp = row["change_pct"]
            cur_price = row["price"]
            cur_open = row["open_price"]
            cur_high = row["high_price"]
            cur_low = row["low_price"]
            cur_ta = row["trade_amount"]

            needs_update = False
            if cur_cp is None or abs(float(cur_cp) - new_cp) >= 0.01:
                needs_update = True
            if cur_price != _c:
                needs_update = True
            if cur_open != _o:
                needs_update = True
            if cur_high != _h:
                needs_update = True
            if cur_low != _lo:
                needs_update = True
            if new_ta and cur_ta != new_ta:
                needs_update = True

            if not needs_update:
                unchanged += 1
                continue

            conn.execute(
                """UPDATE daily_picks SET
                     change_pct=?, price=?, open_price=?, high_price=?, low_price=?,
                     trade_amount=?
                   WHERE id=?""",
                (new_cp, _c, _o, _h, _lo, new_ta, row["id"]),
            )
            updated += 1

        conn.commit()

    print(
        f"[_reconcile_daily_picks_with_dailybars] {today} source={source} "
        f"updated={updated} unchanged={unchanged} "
        f"skipped_no_db={skipped_no_db} skipped_stale={skipped_stale} "
        f"total={len(rows)}"
    )


def _get_prev_pick_info(today: str, code: str, conn=None):
    """전일 daily_picks에서 같은 종목의 정보 조회 (연속 선정 확인).

    pick_count는 **같은 source 내 연속 등장 일수** (오늘 포함, 거래일 기준).
    "연속"이란: 오늘부터 과거로 거슬러 올라가며, 해당 source의 거래일에
    종목이 존재하는 연속 횟수. 중간에 하루라도 빠지면 즉시 중단.

    R3 호환: 외부 conn 주입 가능. None이면 자체 connect (백워드 호환).
    실제 hot path는 `_prefetch_prev_pick_info` 사용 권장.
    """
    if conn is None:
        with connect() as _c:
            return _get_prev_pick_info(today, code, conn=_c)

    src_row = conn.execute(
        """SELECT source FROM daily_picks
           WHERE stock_code=? AND date=?
           ORDER BY CASE WHEN source='kiwoom' THEN 0 ELSE 1 END
           LIMIT 1""",
        (code, today),
    ).fetchone()
    source = src_row["source"] if src_row else "kiwoom"

    prev = conn.execute(
        """SELECT date, rank, trade_amount, change_pct
           FROM daily_picks WHERE stock_code=? AND date < ? AND source=?
           ORDER BY date DESC LIMIT 1""",
        (code, today, source),
    ).fetchone()

    all_dates = conn.execute(
        """SELECT DISTINCT date FROM daily_picks
           WHERE source=? AND date <= ?
           ORDER BY date DESC""",
        (source, today),
    ).fetchall()
    trading_dates = [r["date"] for r in all_dates]

    if not trading_dates or trading_dates[0] != today:
        return dict(prev) if prev else None, 0

    stock_rows = conn.execute(
        """SELECT DISTINCT date FROM daily_picks
           WHERE stock_code=? AND source=?""",
        (code, source),
    ).fetchall()
    stock_dates = {r["date"] for r in stock_rows}

    consecutive = 0
    for d in trading_dates:
        if d in stock_dates:
            consecutive += 1
        else:
            break

    return dict(prev) if prev else None, consecutive


def _prefetch_prev_pick_info(conn, today: str, codes: list[str]) -> dict:
    """build() 종목 루프용 일괄 prefetch — N+1 제거.

    종목 N개에 대해 source/prev/trading_dates/stock_dates를 4개 SELECT로 일괄 로드.
    반환: {code: (prev_dict_or_None, consecutive_int)}.
    `_get_prev_pick_info(today, code)`와 동일 시맨틱 보장.
    """
    if not codes:
        return {}

    placeholders = ",".join("?" * len(codes))

    # 1) 종목별 source (kiwoom 우선) — ORDER BY로 종목별 첫 행 채택
    source_by_code: dict[str, str] = {}
    src_all = conn.execute(
        f"""SELECT stock_code, source
            FROM daily_picks
            WHERE date=? AND stock_code IN ({placeholders})
            ORDER BY stock_code,
                     CASE WHEN source='kiwoom' THEN 0 ELSE 1 END""",
        (today, *codes),
    ).fetchall()
    for r in src_all:
        if r["stock_code"] not in source_by_code:
            source_by_code[r["stock_code"]] = r["source"]
    for c in codes:
        source_by_code.setdefault(c, "kiwoom")

    # 2) 종목별 prev (date<today, 같은 source) — window MAX(date)
    prev_rows = conn.execute(
        f"""SELECT dp.stock_code, dp.date, dp.rank, dp.trade_amount, dp.change_pct, dp.source
            FROM daily_picks dp
            JOIN (
                SELECT stock_code, source, MAX(date) AS mx
                FROM daily_picks
                WHERE date < ? AND stock_code IN ({placeholders})
                GROUP BY stock_code, source
            ) m
              ON dp.stock_code=m.stock_code AND dp.source=m.source AND dp.date=m.mx""",
        (today, *codes),
    ).fetchall()
    prev_by_key: dict[tuple[str, str], dict] = {}
    for r in prev_rows:
        prev_by_key[(r["stock_code"], r["source"])] = {
            "date": r["date"],
            "rank": r["rank"],
            "trade_amount": r["trade_amount"],
            "change_pct": r["change_pct"],
        }

    # 3) source별 trading_dates 일괄 (오늘 이하, 내림차순)
    sources = set(source_by_code.values())
    trading_by_src: dict[str, list[str]] = {}
    if sources:
        src_ph = ",".join("?" * len(sources))
        td_rows = conn.execute(
            f"""SELECT source, date FROM daily_picks
                WHERE date <= ? AND source IN ({src_ph})
                GROUP BY source, date
                ORDER BY source, date DESC""",
            (today, *sources),
        ).fetchall()
        for r in td_rows:
            trading_by_src.setdefault(r["source"], []).append(r["date"])

    # 4) (종목,source) → 등장 date set 일괄
    stock_rows = conn.execute(
        f"""SELECT DISTINCT stock_code, source, date
            FROM daily_picks
            WHERE stock_code IN ({placeholders})""",
        tuple(codes),
    ).fetchall()
    dates_by_key: dict[tuple[str, str], set] = {}
    for r in stock_rows:
        dates_by_key.setdefault((r["stock_code"], r["source"]), set()).add(r["date"])

    out: dict[str, tuple] = {}
    for code in codes:
        source = source_by_code.get(code, "kiwoom")
        prev = prev_by_key.get((code, source))
        trading = trading_by_src.get(source, [])
        if not trading or trading[0] != today:
            out[code] = (prev, 0)
            continue
        stock_dates = dates_by_key.get((code, source), set())
        consecutive = 0
        for d in trading:
            if d in stock_dates:
                consecutive += 1
            else:
                break
        out[code] = (prev, consecutive)
    return out


# ===== REQ-039 — 강세 배지 (키움 정의) ============================================
# 정의 (대표 발화 2026-04-28 01:30 KST, 키움 HTS 표기):
#   1) prev_close * 1.15 <= high   (전일 종가 +15% 이상 고가 도달)
#   2) low * 1.15 <= high          (장중 저가 대비 +15% 이상 고가 도달)
#   3) open * 1.09 <= close        (시가 대비 +9% 이상 종가 마감)
# 셋 모두 AND.
#
# SSOT: daily_picks 테이블 (date, stock_code, source, price, open_price, high_price, low_price)
#  - 당일 OHLC = today row의 open_price/high_price/low_price/price
#  - prev_close = 전일(같은 source) row의 price
#  - prev_close 부재(NULL/0)면 False — FLR-AGT-002 거짓 충실성 차단
#
# streak (연속 강세 일수): 영업일 순회 기준
#  - 오늘 강세 0이면 streak=0
#  - 오늘 강세 1이면 streak=1, 어제도 강세였으면 +1, ... 빠지는 날 발견 시 break
def _calc_limit_up_streak(conn, stock_code: str, date: str) -> int:
    """REQ-082 — 과거 N일 연속 상한가 카운트 (change_pct >= LIMIT_UP_THRESHOLD).

    daily_picks.change_pct SSOT 기준 (D축 정합 — change_pct 1순위 = dailybars close).
    영업일이 아닌 캘린더일 < 비교는 SQLite ORDER BY DESC LIMIT로 자연 처리.
    안전 가드: 30일 초과 streak는 비현실적이므로 break (한국 상하한 ±30% 제도상 7일 연속도 극히 드뭄).
    """
    streak = 1
    cur_date = date
    while True:
        prev = conn.execute(
            """SELECT date, change_pct FROM daily_picks
               WHERE stock_code=? AND date < ?
               ORDER BY date DESC LIMIT 1""",
            (stock_code, cur_date),
        ).fetchone()
        if not prev:
            break
        prev_chg = prev["change_pct"] if hasattr(prev, "keys") else prev[1]
        if prev_chg is None or prev_chg < LIMIT_UP_THRESHOLD:
            break
        streak += 1
        cur_date = prev["date"] if hasattr(prev, "keys") else prev[0]
        if streak > 30:
            break  # 안전 가드 (FLR-AGT-002 — 무한 루프 방지)
    return streak


def _is_bullish(prev_close, open_p, high, low, close) -> bool:
    """REQ-039 — 키움 강세 수식. 5개 가격 필드 모두 truthy일 때만 평가.
    NULL/0 케이스는 False (FLR-AGT-002 거짓 충실성 차단)."""
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


def _prefetch_bullish_info(
    conn, today: str, codes: list[str]
) -> dict[str, tuple[bool, int, list[str]]]:
    """REQ-039 — 종목별 강세 streak + per-day bullish dates 일괄 prefetch.

    반환: {code: (is_bullish_today, bullish_streak, bullish_dates)}.
    - is_bullish_today: bool — dailybars(today) 본문 강세 평가 결과
    - bullish_streak: int — 연속 강세 영업일 수 (0~30)
    - bullish_dates: list[str] — dailybars 본문 강세로 평가된 모든 영업일 dates
      (오름차순, YYYY-MM-DD ISO 8601). today 포함 연속 streak N + 과거 30일 내
      비연속 강세 발생일까지 모두 포함 (P0-21 REQ-004 본문 본질).

    SSOT 정책 (Q-CYCLE22-BULLISH-STREAK-DAILYBARS, 2026-05-20):
    - **dailybars 기반 streak 평가** (build_theme_stats._streak_from_dailybars 패턴 통일).
    - daily_picks 등장 영업일만 비교하는 기존 path는 종목별 부재 영업일을 "자연 스킵"
      → 연속 강세 의미 위반 (수젠텍 253840 4/20→5/20 30일 gap streak=2 mismatch).
    - dailybars는 모든 영업일 row 보유 → 영업일 gap 자연 break + OHLC 정합 (Q-20260512
      high/low close 동일 결함 등 daily_picks OHLC 신뢰 문제도 함께 봉쇄).
    - today 강세 평가도 dailybars row 사용 (daily_picks fallback 제거 — Q-CYCLE22 본질).

    P0-21 본질 (REQ-004, 2026-05-21):
    - bullish_dates 본문 dailybars 30일 range 내 모든 강세 발생일 visible
      (영웅문 본문 정합 본문 분홍 vertical line 본문 각 종목 각 날짜마다 표시 본질).
    - streak 본문 today 기준 연속 강세 정보 별도 유지 (badge 본문 cascade).

    Logic:
    - today 강세: dailybars(today) OHLC + dailybars(prev_business_day) close → _is_bullish.
    - streak: dailybars 영업일 backward chain. 각 영업일 row OHLC + 직전 영업일 close
      _is_bullish 평가, False 시 streak break (단 dates 본문 계속 누적 — 비연속 강세 포함).
    """
    if not codes:
        return {}

    placeholders = ",".join("?" * len(codes))

    # 1) 종목별 dailybars row (today 이전 30일 내림차순) 일괄 fetch.
    #    daily_picks source 분기 불필요 (dailybars는 SSOT).
    rows = conn.execute(
        f"""SELECT code, date, open, high, low, close FROM dailybars
            WHERE code IN ({placeholders}) AND date <= ?
            ORDER BY code, date DESC""",
        (*codes, today),
    ).fetchall()
    by_code: dict[str, list[dict]] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(
            {
                "date": r["date"],
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
            }
        )

    # P0-21 본질: dates 본문 30일 range 한정 (영웅문 본문 정합 + cost 제어).
    # daily_20 본문 frontend 본문 본질 visible 20영업일 본문 정합 + 약간의 buffer (30일).
    # SQL LIMIT per-code 본문 복잡 → Python slice 본문 본질 (cost 본문 미세).
    BULLISH_RANGE_DAYS = 30

    out: dict[str, tuple[bool, int, list[str]]] = {}
    for code in codes:
        seq_full = by_code.get(code, [])
        # P0-21: 30일 range 한정 (per-code).
        seq = seq_full[:BULLISH_RANGE_DAYS]
        # today row가 첫 원소 (date desc) 아니면 = 오늘 dailybars 부재 → streak 0 + dates []
        if not seq or seq[0]["date"] != today:
            out[code] = (False, 0, [])
            continue

        # 영업일 row 1건만 (직전 영업일 base 부재) → streak 0 + dates []
        # (P0-21: 단일 row는 prev_close 비교 불가, 본문 강세 평가 불가.)
        if len(seq) < 2:
            out[code] = (False, 0, [])
            continue

        # P0-21 본질: 30일 range 내 강세 영업일 dates 누적 (비연속 포함).
        # seq[i] = eval, seq[i+1] = base (직전 영업일).
        # streak 본문 today 연속만 계산 (False 시 break), dates 본문 30일 range 전체 누적.
        bullish_dates_desc: list[str] = []  # 내림차순 누적 (오늘 first)
        streak = 0
        streak_break = False
        for i in range(len(seq) - 1):
            eval_row = seq[i]
            base_row = seq[i + 1]
            is_bull = _is_bullish(
                base_row["close"],
                eval_row["open"],
                eval_row["high"],
                eval_row["low"],
                eval_row["close"],
            )
            if is_bull:
                bullish_dates_desc.append(eval_row["date"])
                if not streak_break:
                    streak += 1
                    if streak > 30:
                        streak_break = True  # 안전 가드 (FLR-AGT-002 무한 루프 방지)
            else:
                # streak 본문 첫 false 시 break (today 연속만 의미).
                # dates 본문 계속 누적 (과거 비연속 강세 visible 본질).
                streak_break = True

        is_bull_today = streak >= 1  # streak 1+ = today 강세 확정
        bullish_dates = list(reversed(bullish_dates_desc))  # 오름차순 (오늘 마지막)
        out[code] = (is_bull_today, streak, bullish_dates)

    return out


def is_market_closed(date_str: str) -> bool:
    """KRX 휴장일 여부. holidays.json 참조, 없으면 주말만 체크."""
    holidays_path = HOMEPAGE / "data" / "holidays.json"
    if holidays_path.exists():
        try:
            data = json.loads(holidays_path.read_text())
            return date_str in data.get("market_closed", {})
        except Exception:
            pass
    # 폴백: 주말만 체크
    from datetime import datetime as _dt

    d = _dt.strptime(date_str, "%Y-%m-%d")
    return d.weekday() >= 5  # 토(5), 일(6)


def _macro_signature_tokens(cand: dict) -> set:
    """매크로 후보의 의미 식별 토큰 집합.

    summary 전체(인과 사슬 모든 마디)를 토큰화. macro_events 테이블엔
    causal_chain/sector 컬럼이 없으므로(schema.sql L67-77), 실재 텍스트(summary)
    만으로 의미 동일성을 판정한다(FLR-AGT-002 추정 금지).

    keyword 는 시그니처에서 제외 — 같은 사건이라도 변형 키워드(디지털자산/블록체인
    /암호화폐진출 …)가 매번 달라 토큰 집합을 희석(dilute)시켜 동일 사건 검출을
    방해하기 때문(5/28 실데이터 검증). summary 본문에는 사건의 공통 핵심 엔티티
    (두나무 / SK하이닉스·1조달러 등)가 반복 등장해 식별력이 높다.
    """
    from .extract_macros import tokenize

    return set(tokenize(cand.get("summary") or ""))


# Q-20260608-138 (대표 verbatim "당일 시장 상황·뉴스 우선순위·중요도 고려"):
# 매크로 노출 순서 = 당일 시장 중요도(시장 영향 크기) × 긴급도(시간 민감성) 우선.
# source_count(언론사 보도량) 단일 proxy 가 개별 종목 뉴스를 지수·시스템 리스크
# 위로 올리던 root 봉쇄(6/8 라이브: '검은월요일 공포'가 4위, '사이드카 발동'이 8위).
# 결정적(deterministic) 키워드 매칭 — LLM 재요약이 순위를 흔들지 않도록 score 기반
# 고정(FLR-20260527-TEC-001 정신: 추정·proxy 최소화, 실 summary 텍스트 토큰 기반).
#
# 중요도(MARKET-WIDE IMPACT): 지수·전섹터·시스템 리스크 = 최상위 가중. 개별 종목·
# 단일 섹터 IR 은 0. macro_events 에 섹터·종목 영향 데이터가 없으므로(stock_codes_json
# =언론사명) summary 본문의 시장 영향 어휘로 판정.
_MACRO_IMPACT_INDEX = (  # 지수·시장 전체·시스템 리스크 (최상위 +10)
    "코스피",
    "코스닥",
    "지수",
    "증시",
    "검은월요일",
    "검은 월요일",
    "사이드카",
    "서킷브레이커",
    "환율",
    "원화",
    "금리",
    "유동성",
    "시스템 리스크",
    "글로벌 경기",
    "경기 둔화",
    "경기둔화",
    "패닉",
    "공포",
    "폭락",
    "전섹터",
    "일제",
)
_MACRO_IMPACT_MACRO = (  # 거시·정책·지정학 (섹터 횡단, +6)
    "트럼프",
    "관세",
    "무역전쟁",
    "미중",
    "연준",
    "Fed",
    "FOMC",
    "중동",
    "이란",
    "전쟁",
    "유가",
    "원유",
    "지정학",
)
# 긴급도(URGENCY): 시간 민감·당일 장중 즉발 이벤트 (신선도 요소). hit 당 +2, cap 8.
_MACRO_URGENCY_HOT = (
    "사이드카",
    "서킷브레이커",
    "장중",
    "장초",
    "붕괴",
    "급락",
    "급등",
    "발동",
    "검은월요일",
    "검은 월요일",
    "공포",
    "패닉",
    "폭락",
    "긴급",
)


def _macro_text(cand: dict) -> str:
    return (cand.get("summary") or "") + " " + (cand.get("title") or "")


def _macro_impact_score(cand: dict) -> int:
    """시장 영향 크기 — 지수·전섹터(+10) / 거시·지정학(+6). 개별 종목=0."""
    txt = _macro_text(cand)
    score = 0
    if any(w in txt for w in _MACRO_IMPACT_INDEX):
        score += 10
    if any(w in txt for w in _MACRO_IMPACT_MACRO):
        score += 6
    return score


def _macro_urgency_score(cand: dict) -> int:
    """시간 민감성 — 장중 급락·사이드카 등 즉발 이벤트. hit 당 +2, cap 8."""
    txt = _macro_text(cand)
    hits = sum(1 for w in _MACRO_URGENCY_HOT if w in txt)
    return min(hits, 4) * 2


def _dedup_and_rank_macros(candidates: list, limit: int = 10) -> list:
    """매크로 후보를 의미중복 병합 후 중요도 가중으로 top-N 선발.

    DOC-20260605-DSN(arch-pipeline §7.11). build_daily.py 매크로 표시부 전용.

    1. dedup: 후보를 토큰 overlap coefficient(교집합/작은쪽 크기) 로 그리디
       single-link 클러스터링. 같은 사건(예 '삼성·두나무 인수', 'SK하이닉스 시총
       1조')의 변형 키워드를 1건으로 병합 — 대표 1건(verified·source_count 우선순위
       최상위)만 유지, source_count 합산, 병합 개수(cluster_size) 기록.
       overlap coefficient 채택 이유(Jaccard 대비): 변형 매크로의 summary 길이·
       키워드별 고유 토큰이 제각각이라 Jaccard 는 union 팽창으로 동일 사건도
       0.1~0.3 으로 희석됨(5/28 실측). overlap 은 짧은 쪽 기준이라 공통 핵심
       엔티티(두나무 / SK하이닉스·1조달러) 반복 사건을 안정 검출.
    2. 정렬: source_count 단일 정렬을 중요도 가중으로 교체.
       score = verified 가중 + 합산 source_count + cluster_size(사건 광범위 proxy).
       macro_events 에 종목·섹터 영향 데이터가 없으므로(stock_codes_json=언론사명),
       여러 언론사·여러 변형으로 보도된 사건(cluster_size 大)을 광범위 proxy 로 사용.
    3. tie-break: 동점 시 created_at(발생 origin 시각) 빠른 쪽 우선
       → morning_pin. 오전 발생 핵심이 오후 신규에 밀려 탈락하던 root 봉쇄.

    반환: [{"title", "summary"}, ...] (limit 개) — 기존 출력 shape 불변.
    입력 candidates 는 SQL 정렬(verified>source priority>source_count) 순서 가정.

    한계(anti-proxy 정직, FLR-20260527-TEC-001 정신): causal_chain/sector 데이터
    부재로 순수 텍스트 클러스터링은 의미 동일성을 완전 복원하지 못한다. 동일
    섹터의 의미상 별개 사건(예 'AI 데이터센터 부품 수급' vs 'SK하이닉스 시총 1조')은
    공통 토큰이 적으면 분리 유지될 수 있고, 이는 과병합 회피상 의도된 보수적 동작.
    """
    # 유사도 임계 — overlap coefficient 0.5 이상이면 동일 사건으로 병합.
    # 5/28 실데이터 튜닝: 삼성·두나무 변형 / SK하이닉스 변형이 single-link 로
    # 묶이되, 무관 사건(양자보안·풍력 등)은 분리 유지되는 경계값.
    _SIM_THRESHOLD = 0.5

    # 각 원소: {"rep": cand, "tokens": set(누적 union), "sum_sc": int, "size": int}
    clusters = []
    for cand in candidates:
        sig = _macro_signature_tokens(cand)
        merged = False
        for cl in clusters:
            base = cl["tokens"]
            if not base or not sig:
                continue
            inter = len(base & sig)
            # overlap coefficient: 교집합 / 두 집합 중 작은 쪽 크기
            denom = min(len(base), len(sig))
            if denom and inter / denom >= _SIM_THRESHOLD:
                # single-link: 클러스터 토큰을 union 으로 누적 → 전이적 병합 허용.
                # 대표는 첫(최상위 우선순위) 후보 유지.
                cl["tokens"] |= sig
                cl["sum_sc"] += cand.get("source_count") or 0
                cl["size"] += 1
                # Q-138: 클러스터 내 변형들의 중요도·긴급도는 max 채택 — 같은 사건의
                # 여러 보도 중 가장 강한 시장 영향 신호를 대표에 보존.
                cl["impact"] = max(cl["impact"], _macro_impact_score(cand))
                cl["urgency"] = max(cl["urgency"], _macro_urgency_score(cand))
                merged = True
                break
        if not merged:
            clusters.append(
                {
                    "rep": cand,
                    "tokens": set(sig),
                    "sum_sc": cand.get("source_count") or 0,
                    "size": 1,
                    "impact": _macro_impact_score(cand),
                    "urgency": _macro_urgency_score(cand),
                }
            )

    def _score(cl):
        # Q-138: 당일 시장 중요도(impact) × 긴급도(urgency) 최우선.
        # 시장 전체·지수·시스템 리스크(impact) + 장중 즉발 이벤트(urgency)가
        # 언론사 보도량(base) 위에서 순위를 지배 → 개별 종목 IR 은 자연 하위.
        rep = cl["rep"]
        verified_w = 5 if rep.get("verified") else 0
        # 광범위 proxy: 병합 변형 수(size) — 여러 각도 보도 = 중요 사건
        breadth = cl["size"]
        base = verified_w + cl["sum_sc"] + breadth
        return cl["impact"] + cl["urgency"] + base

    # 1차 score 내림차순, 동점 시 created_at 빠른(오전) 쪽 우선(morning_pin)
    clusters.sort(key=lambda cl: (-_score(cl), cl["rep"].get("created_at") or "~"))
    return [
        {"title": cl["rep"]["title"], "summary": cl["rep"]["summary"]}
        for cl in clusters[:limit]
    ]


def build():
    from .config import pipeline_date

    today = pipeline_date()

    # 휴장일이면 빌드 스킵
    if is_market_closed(today):
        reason = "주말"
        holidays_path = HOMEPAGE / "data" / "holidays.json"
        if holidays_path.exists():
            try:
                data = json.loads(holidays_path.read_text())
                reason = data.get("market_closed", {}).get(today, "휴장일")
            except Exception:
                pass
        print(f"[build_daily] {today} 은(는) {reason} — 빌드 스킵")
        return None

    kiwoom, data_source = load_kiwoom_volume_list()

    # HEROSHIK STRICT OVERRIDE 활성 시 union skip (대표 결정 P0 2026-05-08).
    # heroshik 적용 일자(예: 5/6)는 condition_500eok* / pipeline_chg / kiwoom_ka10017
    # union 적용 X → KOSPI100 mega-cap 재포함 방지 (mission Step 5 정합).
    _heroshik_active = data_source == "heroshik_strict"
    if _heroshik_active:
        print(
            f"[build_daily] HEROSHIK STRICT 활성 — 영웅문식 daily_picks union + 상한가 union skip "
            f"({today}, primary={len(kiwoom)}종)"
        )

    # SC 정책 = Option C (영웅문식 ∪ LU). 대표 결정 2026-05-08 09:57: "c가 내 규칙이야".
    # togusa 17일 audit catch (2026-05-08): daily_picks에 영웅문식 백필 source
    # ('condition_500eok%' or 'pipeline_chg' 등)로 적재된 종목들이 build SC에 반영 X →
    # 영웅문식 ⊂ SC 위반 (5/6 30건 KOSPI 초대형주 누락 등). 본 union으로 정합 봉쇄.
    # HEROSHIK STRICT 활성 시 본 union skip (KOSPI100 mega-cap 재포함 방지).
    try:
        if _heroshik_active:
            raise _HeroshikSkipUnion()
        with connect() as _conn_dp:
            _dp_rows = _conn_dp.execute(
                """SELECT stock_code, source, price, trade_amount, change_pct
                   FROM daily_picks WHERE date=?""",
                (today,),
            ).fetchall()
        _existing_codes = {(k.get("code") or k.get("ticker")) for k in kiwoom}
        _added_dp = []
        for _r in _dp_rows:
            _code = _r["stock_code"]
            if not _code or _code in _existing_codes:
                continue
            _src = _r["source"] or ""
            # 영웅문식 백필 source만 union (kiwoom source는 이미 daily_top에 있음)
            # condition_500eok_*, condition_500eok_OR_limitup_*, pipeline_chg, kiwoom_ka10017 등
            if _src.startswith("condition_500eok") or _src in (
                "pipeline_chg",
                "kiwoom_ka10017",
            ):
                _added_dp.append(
                    {
                        "ticker": _code,
                        "name": "",
                        "last_price": _r["price"],
                        "max_trade_amount": _r["trade_amount"],
                        "trade_amount": _r["trade_amount"],
                        "max_change_pct": None,
                        "change_pct": _r["change_pct"],
                        "appearances": 1,
                        "first_seen": "",
                        "last_seen": "",
                        "open": None,
                        "high": _r["price"],
                        "low": _r["price"],
                        "min_change_pct": None,
                        "_source_union": f"daily_picks:{_src}",
                    }
                )
                _existing_codes.add(_code)
        if _added_dp:
            kiwoom = list(kiwoom) + _added_dp
            print(
                f"[build_daily] 영웅문식 daily_picks union: {len(_added_dp)}건 추가 → "
                f"stocks={len(kiwoom)}"
            )
    except _HeroshikSkipUnion:
        pass
    except Exception as _e_dp:
        print(f"[build_daily] 영웅문식 union skip (오류): {_e_dp}")

    # === 상한가 종목 union 제거 (D축, 대표 확정 2026-06-16) ===
    # 종전 REQ-080 §1 은 거래대금 상위 base(조건검색 '500억이상') 에 ka10017
    # stock_status_badges 기반 상한가 종목을 별도 union 했다.
    # D축 전환: 조건검색이 '500억이상' → 'v1' 으로 통합되어 v1 결과 목록 자체가
    # 거래대금 500억 ∪ 당일 상한가를 한 번에 반환한다 (kiwoom-scraper main.py
    # docstring "v1 이 거래대금 500억 ∪ 당일 상한가를 한 번에 반환"). 따라서
    # 상한가 종목은 이미 kiwoom 리스트(latest_stocks/daily_top)에 포함 → 별도
    # ka10017 union 불요. 상한가 판정은 본 리스트의 change_pct >= 29.79 (아래
    # status_badges 부착부, LIMIT_UP_THRESHOLD). collect_kiwoom_limit_up(ka10017)
    # 폐기와 정합 (DSN-001 §2.3/§2.5).

    # Q-20260513-047 (대표 정책 2026-05-13 23:39): ka10032 일상 사용 금지.
    # change_pct 1순위 = dailybars SoT (collect_dailybars KRX 정식 일봉). 외부 ranking
    # (latest.json) 의존 폐기. fallback 4단(dailybars → 조건검색 k.get → daily_picks
    # → intraday)은 본 함수 호출측 L2978~ 그대로 유지. ka10032 호출은 백필/정합성/대표
    # 명시 지시 시에만 별도 script(collect_ranking.py)로 수동 단발. rules/data-continuity.md
    # §ka10032 사용 정책 정합. recurring lead 환각 11회차 봉쇄 (§1.1 grep 3종 사전 catch).

    # daily_picks 저장
    # HEROSHIK STRICT 활성 시 daily_picks 재적재 skip (이미 'heroshik_strict_<m_d>' source로 적재됨).
    if _heroshik_active:
        print(
            "[build_daily] HEROSHIK STRICT — _save_daily_picks skip "
            "(daily_picks 'heroshik_strict_*' source 이미 존재)"
        )
    else:
        _save_daily_picks(today, kiwoom, source=data_source)
        # Q-20260527-OG-MISMATCH-INPUT-LAYER (2026-05-28 옵션 A):
        # _save_daily_picks 직후 reconciliation pass 1회 — dailybars SoT 기준
        # daily_picks(today) 전건 UPDATE. Fix B/C 분기 미진입 stale row 봉쇄
        # (5/27 18종 cp mismatch >1.0%p 영구 봉쇄).
        _reconcile_daily_picks_with_dailybars(today, source=data_source)

    # 과거 OHLC null 행 자동 복구
    backfill_ohlc()

    prev_stocks, prev_date = _load_prev_day_stocks(today)
    if prev_date:
        print(f"prev day fallback: {prev_date} ({len(prev_stocks)} stocks with news)")

    stocks_out = []
    news_total = 0
    import re as _re

    _CODE_FILTER = _re.compile(r"^\d{6}$")

    with connect() as conn:
        # kt20016 신용가능 종목 (당일 스냅샷)
        credit_eligible = set()
        try:
            rows = conn.execute(
                "SELECT stock_code FROM credit_eligible WHERE date=?", (today,)
            ).fetchall()
            credit_eligible = {r[0] for r in rows}
        except Exception:
            pass

        # 분봉 스냅샷 (ka10080 — sparkline 렌더용)
        intraday_by_code: dict = {}
        try:
            rows = conn.execute(
                """SELECT stock_code, step_min, open, prices_json
                   FROM intraday_snapshot WHERE date=?""",
                (today,),
            ).fetchall()
            intraday_by_code = {r["stock_code"]: r for r in rows}
        except Exception:
            pass

        # kt20017 키움증권 오늘자 신용융자 상태 (당일)
        stock_status = {}
        try:
            rows = conn.execute(
                """SELECT stock_code, limit_exceeded, raw_status
                   FROM credit_stock_status WHERE date=?""",
                (today,),
            ).fetchall()
            stock_status = {r[0]: (r[1], r[2]) for r in rows}
        except Exception:
            pass

        # R3 PR1: prev_pick_info 일괄 prefetch (N+1 제거)
        _valid_codes = [
            c
            for c in (k.get("code") or k.get("ticker") for k in kiwoom)
            if c and _CODE_FILTER.match(c)
        ]
        prev_pick_cache = _prefetch_prev_pick_info(conn, today, _valid_codes)

        # REQ-039 — 강세 배지 prefetch (daily_picks SSOT, 신규 테이블 X).
        # REQ-048 — cf51990 결함 정정: assignment 누락 → bullish_cache NameError 회귀.
        bullish_cache = _prefetch_bullish_info(conn, today, _valid_codes)

        # 임무 C (2026-04-24 시정): 지수(KOSPI/KOSDAQ) 당일/전일 close prefetch.
        # index_multiple_current = stock_change_pct / index_change_pct (ratio, 배수).
        # FLR-20260424-FLR-001 (1차 spread 방식 오류 시정): KRX 원문 재확인 결과
        #   "주가상승률이 같은 기간 종합주가지수 상승률의 5배 이상" = ratio 기준.
        # 경계 처리:
        #   - index_chg ≈ 0 (|x|<EPS): ratio 미정의 → None
        #   - index_chg < 0 (지수 하락): 규정은 "상승률" 조건이므로 상승 국면 한정
        #     → 종목이 상승이어도 condition 미성립. met_eligible=False로 전파.
        #   - 둘 다 음수(동반하락) 역시 "상승률" 조건 밖 → met_eligible=False.
        index_change_pct: dict[str, float | None] = {"KOSPI": None, "KOSDAQ": None}
        try:
            for idx_code in ("KOSPI", "KOSDAQ"):
                rows = conn.execute(
                    """SELECT date, close FROM index_dailybars
                       WHERE index_code=? AND date<=? AND close>0
                       ORDER BY date DESC LIMIT 2""",
                    (idx_code, today),
                ).fetchall()
                if len(rows) >= 2 and rows[1]["close"] > 0:
                    today_close = float(rows[0]["close"])
                    prev_close = float(rows[1]["close"])
                    index_change_pct[idx_code] = round(
                        (today_close - prev_close) / prev_close * 100, 2
                    )
        except Exception as _e:
            # index_dailybars 없거나 부재 → ratio 미적용 (graceful degrade)
            pass

        def _compute_index_ratio(
            stock_chg: float | None, idx_chg: float | None
        ) -> tuple[float | None, bool]:
            """KRX 투자경고/위험 ratio 계산 (FLR-20260424-FLR-001 시정).

            규정 원문: "주가상승률이 같은 기간 종합주가지수 상승률의 5배(또는 3배) 이상"
            → ratio = stock_chg / idx_chg. 단 "상승률" 전제이므로 하락 국면은 조건 미성립.

            Returns:
                (ratio, met_eligible) —
                  ratio: 수치(표시용). idx_chg≈0 또는 데이터 부재 시 None.
                  met_eligible: ratio 조건 평가 자체가 가능한지 (상승 국면 + 종목 상승).
                    False면 호출측은 met 플래그를 False로 간주해야 함.
            """
            if stock_chg is None or idx_chg is None:
                return None, False
            if not isinstance(stock_chg, (int, float)) or not isinstance(
                idx_chg, (int, float)
            ):
                return None, False
            # 경계 1: 지수 등락률 ≈ 0 → ratio 미정의
            if abs(idx_chg) < INDEX_CHG_EPS:
                return None, False
            ratio = round(float(stock_chg) / float(idx_chg), 2)
            # 경계 2: "상승률" 조건이므로 지수·종목 둘 다 양수일 때만 eligible
            met_eligible = float(stock_chg) > 0 and float(idx_chg) > 0
            return ratio, met_eligible

        for k in kiwoom:
            code = k.get("code") or k.get("ticker")
            if not code or not _CODE_FILTER.match(code):
                continue
            master = conn.execute(
                "SELECT * FROM stocks WHERE code=?", (code,)
            ).fetchone()
            fb = fetch_fallback(code, prev_stocks, prev_date)
            news_total += len(fb.get("news", []))

            # 전일 오늘의 종목 정보 (R3 PR1: 일괄 prefetch dict lookup)
            prev_pick, pick_total = prev_pick_cache.get(code, (None, 0))
            # REQ-039 — 강세 배지 (daily_picks SSOT)
            # P0-21 REQ-004: bullish_dates 본문 per-day history 필드 신축 (3-tuple unpack).
            bullish_today, bullish_streak, bullish_dates = bullish_cache.get(
                code, (False, 0, [])
            )
            prev_info = None
            if prev_pick:
                prev_info = {
                    "date": prev_pick["date"],
                    "rank": prev_pick["rank"],
                    "change_pct": prev_pick["change_pct"],
                    "trade_amount": prev_pick["trade_amount"],
                }

            rank = k.get("rank") or (kiwoom.index(k) + 1)
            # FLR-20260507-TEC (cross-source 2nd, 49건 mismatch 봉쇄):
            # trade_amount source 통일 — daily_picks → dailybars fallback (LU 정합 a4adf16).
            trade_amt = (
                k.get("trade_amount")
                or k.get("max_trade_amount")
                or k.get("trade_amount_won")
            )
            if trade_amt is None:
                _db_ta = conn.execute(
                    "SELECT trade_amount FROM dailybars WHERE code=? AND date=?",
                    (code, today),
                ).fetchone()
                if _db_ta and _db_ta["trade_amount"]:
                    trade_amt = _db_ta["trade_amount"]
            # 등락률 (FLR cross-source 2nd): close 기준만 통일.
            # Q-20260513-047 (대표 정책 2026-05-13 23:39): ka10032 일상 사용 금지로 우선순위 재정렬.
            #   1순위 dailybars.close 직접 계산 (SoT — KRX 정식 일봉, Fix D 정합 b800ef9)
            #   2순위 kiwoom change_pct (조건검색 main.py — 장중 즉시 값)
            #   3순위 daily_picks.price 전일 close 직접 계산
            #   4순위 intraday_snapshot.open (신규 상장 종목)
            #   ka10032 ranking 의존 폐기 (대표 정책 + 사용처 = 백필/정합성/명시 지시만).
            #   max_change_pct (장중 high 기준) 최후 fallback 제거 — high vs close mismatch 차단.
            # Option C 정합 (대표 결정 2026-05-08 09:57): close 기준 SSOT
            # 본 close_price_raw는 영웅문 last_price (장중 cur_prc 또는 종가).
            # last_price 부재 시 dailybars.close → 둘 다 없으면 None.
            # FLR-20260508-TEC (chg=0.0 fallback 결함, 31/39건 가짜 0):
            #   기존 `if not change` 진입 조건은 0.0(falsy)도 fallback 진입 → fallback 결과
            #   prev_close==today_close 시 0.0 산출 → 가짜 0.0 적재. `is None` 으로 정정.
            # Fix D (2026-05-13, Q-20260513-039/042 DOC-20260513-FLR-002):
            #   JSON entry build 경로도 daily_picks DB 분기(L2137~2157 Fix C)와 동형
            #   dailybars 우선 게이트 적용. kiwoom k 캐시가 stale (장중 마지막 폴링 또는
            #   intraday cached) 인 경우 SoT(dailybars) override 후 close/OHLC 4값 정합.
            #   동일 4값 stale (collect_dailybars 봉 미확정 cur_prc 응답) 시 skip 후
            #   kiwoom k 유지 (Fix C 패턴 동형).
            #   대표 catch (010170 5/13): JSON top-level close 29100 (kiwoom k stale)
            #     vs daily_20[today] close 24750 (dailybars SoT). 같은 JSON 내 mismatch.
            _db_ohlc_e = conn.execute(
                "SELECT open, high, low, close FROM dailybars WHERE code=? AND date=?",
                (code, today),
            ).fetchone()
            _db_ohlc_e_stale = False
            if _db_ohlc_e and _db_ohlc_e["close"]:
                _eo = _db_ohlc_e["open"] or 0
                _eh = _db_ohlc_e["high"] or 0
                _elo = _db_ohlc_e["low"] or 0
                _ec = _db_ohlc_e["close"] or 0
                if (
                    _eo > 0
                    and _eh > 0
                    and _elo > 0
                    and _ec > 0
                    and _eo == _eh == _elo == _ec
                ):
                    # Q-20260519-CYCLE12-JOLSS (2026-05-19): LU/LD 점상 stale 오판 면제.
                    # change_pct ±29.0% 이상 (KRX 상하한가 ±30%) 시 정합 점상 (장 마감
                    # 후 정상 일봉). Fix C 동형 — L2199 daily_picks 분기와 일관 처리.
                    # 졸스 018700 5/19 catch (open_price=NULL → miniCandle '|' 표시).
                    _k_chg_e = k.get("change_pct")
                    _is_limit_point_e = (
                        _k_chg_e is not None and abs(float(_k_chg_e)) >= 29.0
                    )
                    if not _is_limit_point_e:
                        _db_ohlc_e_stale = True
            close_price_raw = k.get("last_price") or k.get("close")
            entry_open = k.get("open") or None
            entry_high = k.get("high") or None
            entry_low = k.get("low") or None
            if _db_ohlc_e and _db_ohlc_e["close"] and not _db_ohlc_e_stale:
                # dailybars SoT override (장 종료 후 KRX 정식 일봉 정합)
                close_price_raw = _db_ohlc_e["close"]
                entry_open = _db_ohlc_e["open"] if _db_ohlc_e["open"] else entry_open
                entry_high = _db_ohlc_e["high"] if _db_ohlc_e["high"] else entry_high
                entry_low = _db_ohlc_e["low"] if _db_ohlc_e["low"] else entry_low
            # Q-20260519-CYCLE12-JOLSS-V2 (2026-05-19): JSON entry OHLC NULL fallback.
            #   daily_picks DB row 가 stale (이전 cron 적재 NULL 잔존) + dailybars 적재
            #   타이밍 늦거나 부재한 경우 close_price_raw 로 fallback. miniCandle '|'
            #   표시 봉쇄. L2197 daily_picks INSERT 분기와 일관 처리 (전 종목 룰).
            #   대표 본질 룰 (2026-05-19 17:30+17:43) — 전체 로직 반영.
            if close_price_raw:
                if not entry_open:
                    entry_open = close_price_raw
                if not entry_high:
                    entry_high = close_price_raw
                if not entry_low:
                    entry_low = close_price_raw
            if close_price_raw is None:
                # 영웅문 last_price 매핑 실패 시 dailybars.close fallback (Option C 정합)
                _db_close = conn.execute(
                    "SELECT close FROM dailybars WHERE code=? AND date=?",
                    (code, today),
                ).fetchone()
                if _db_close and _db_close["close"]:
                    close_price_raw = _db_close["close"]
            # 1순위 dailybars.close SoT (KRX 정식 일봉, Fix D 정합)
            # Q-20260515-BUILD-DAILY-ADJ-RATIO (2026-05-15): 권리락 ratio 보정.
            #   raw prev_close * adjustment_ratio = adjusted prev_close. ratio 부재 시 1.0.
            #   cascade SQL 패턴 (heroshik_strict_5_6_v3.py L157~163 동형).
            #   예: 290690 5/15 ratio=1.688 → raw_prev 3300 → adj_prev 5570.4 → adj_chg -0.01%.
            change = None
            if close_price_raw:
                prev_db = conn.execute(
                    """SELECT close AS raw_close, date AS prev_date FROM dailybars
                       WHERE code=? AND date<? AND close>0
                       ORDER BY date DESC LIMIT 1""",
                    (code, today),
                ).fetchone()
                if prev_db and prev_db["raw_close"]:
                    _adj_e = conn.execute(
                        """SELECT ratio FROM dailybars_adjustments
                           WHERE code=? AND date > ? AND date <= ?
                           ORDER BY date DESC LIMIT 1""",
                        (code, prev_db["prev_date"], today),
                    ).fetchone()
                    _ratio_e = (
                        float(_adj_e["ratio"]) if _adj_e and _adj_e["ratio"] else 1.0
                    )
                    _adj_prev_e = float(prev_db["raw_close"]) * _ratio_e
                    change = round(
                        (float(close_price_raw) - _adj_prev_e) / _adj_prev_e * 100,
                        2,
                    )
            # 2순위 조건검색 chg_pct (장중 즉시 값, k 캐시)
            # FLR-20260605-TEC-001 (데이터 필드 변종): close_price_raw 가드 추가.
            #   기존 L3309/L3334/L3347 fallback은 모두 `and close_price_raw` 가드가
            #   있으나, 본 2순위 k.get("change_pct")만 가드 누락 → 키움 매핑 실패로
            #   close=None인데 조건검색 캐시 change_pct(어제값 잔존)만 채워지는
            #   부분상태 생성(095610 change_pct=5.96 + close=None). close 없이
            #   등락률 단독 채택 금지 (추정 금지, FLR-AGT-002 동형).
            if change is None and close_price_raw:
                change = k.get("change_pct")
            if change is None and close_price_raw:
                # daily_picks.price fallback (dailybars 부재 시)
                prev_row = conn.execute(
                    """SELECT price FROM daily_picks
                       WHERE stock_code=? AND date < ? AND price > 0
                       ORDER BY date DESC LIMIT 1""",
                    (code, today),
                ).fetchone()
                if prev_row and prev_row["price"]:
                    change = round(
                        (close_price_raw - prev_row["price"]) / prev_row["price"] * 100,
                        2,
                    )
            if change is None and close_price_raw:
                # 신규 상장 종목 fallback (Q-20260512-FRESH-LISTING-CHANGE-PCT):
                #   dailybars/daily_picks 직전 영업일 row 부재 시 (= 상장 첫날) intraday_snapshot.open
                #   (= 시초가, 키움 ka10080 첫 분봉 open_pric, tic_scope="5") 대비 등락률 산출.
                #   공모가 source 부재 → 시초가 대비 (키움 HTS 정합). 일반 종목 룰은 위 4단 우선.
                _intra = intraday_by_code.get(code)
                if _intra:
                    _intra_open = _intra["open"] if "open" in _intra.keys() else None
                    if _intra_open and float(_intra_open) > 0:
                        change = round(
                            (float(close_price_raw) - float(_intra_open))
                            / float(_intra_open)
                            * 100,
                            2,
                        )
            # max_change_pct (장중 high 기준) fallback 제거 — cross-source 2nd FLR 차단.
            # change=None이면 None 유지 (UI에서 N/A 표시, 가짜 0.0 차단).

            # FLR-20260605-TEC-001 부분상태 불변식 가드 (데이터 필드 변종):
            #   한 카드의 (change_pct, close/OHLC)는 all-present 또는 all-absent.
            #   close_price_raw가 None(키움 매핑 실패 + dailybars 미적재 = 데이터
            #   수집 중)이면 change/OHLC도 모두 None으로 묶어 일관 "수집 중" 상태로
            #   강제. 서로 다른 소스(조건검색 캐시 vs dailybars)가 일부만 채워
            #   어제값 섞이는 부분상태 생성 차단. daily_20은 dailybars 독립 쿼리라
            #   close=None이면 자연히 None(전체 부재) → 3중 소스 일관.
            if close_price_raw is None:
                change = None
                entry_open = None
                entry_high = None
                entry_low = None

            # 공시 데이터 조회 (해당 날짜 + 종목, 전체)
            disc_rows = conn.execute(
                """SELECT summary, source_url, disclosure_cat, sentiment, source,
                          title, period_start, period_end, condition_text, regulation_period
                   FROM disclosures
                   WHERE stock_code=? AND date=?
                   ORDER BY ABS(sentiment) DESC""",
                (code, today),
            ).fetchall()
            disclosures = []
            for dr in disc_rows:
                if dr["summary"]:
                    d = {
                        "title": dr["summary"],
                        "category": dr["disclosure_cat"] or "",
                        "sentiment": dr["sentiment"] or 0,
                        "url": dr["source_url"] or "",
                        "is_cb": dr["disclosure_cat"] == "CB",
                        "source": dr["source"] or "DART",
                        "raw_title": dr["title"] or "",
                    }
                    if dr["period_start"]:
                        d["period_start"] = dr["period_start"]
                    if dr["period_end"]:
                        d["period_end"] = dr["period_end"]
                    if dr["condition_text"]:
                        d["condition"] = dr["condition_text"]
                    if dr["regulation_period"]:
                        d["regulation_period"] = dr["regulation_period"]
                    disclosures.append(d)

            # 테마 조립 + 카페 유래 테마 union (2단계 b, read-time only).
            _base_themes = _normalize_themes(
                _get_cumulative_themes(conn, code, master)
                or fb.get("themes", [])
                # industry 폴백 제거 (산업분류 ≠ 테마, 대표 결정 4/14)
                or ([])
            )
            _entry_themes, _cafe_added = _merge_cafe_themes(code, _base_themes)

            entry = {
                "code": code,
                "name": k.get("name") or (master["name"] if master else ""),
                "rank": rank,
                "trade_amount": trade_amt,
                "change_pct": change,
                # close_price: 영웅문 last_price 우선 → dailybars.close fallback (Option C)
                # Fix D (2026-05-13): OHLC도 dailybars SoT 우선 (entry_open/high/low),
                #   kiwoom k 캐시 stale 차단 (Q-20260513-039/042 DOC-20260513-FLR-002)
                "close_price": close_price_raw,
                "open_price": entry_open,
                "high_price": entry_high,
                "low_price": entry_low,
                "industry": master["industry"] if master else fb.get("industry"),
                "sector": (master["sector"] if master else None) or fb.get("sector"),
                "themes": _entry_themes,
                "theme_paths": _get_theme_paths(conn, _entry_themes),
                "pick_count": pick_total,
                "prev_pick": prev_info,
                # REQ-039 — 강세 배지 (키움 수식). bullish_streak >= 1 일 때만 의미.
                "bullish_today": bullish_today,
                "bullish_streak": bullish_streak,
                # P0-21 REQ-004 — per-day bullish flag history.
                # dailybars 본문 30일 range 내 강세 영업일 dates (오름차순, today 마지막).
                # frontend 본문 분홍 vertical line 본문 각 종목 각 날짜마다 visible 본질
                # (영웅문 본문 정합). graceful backward derive 본문 본 필드 직접 사용.
                "bullish_dates": bullish_dates,
                # B1 fix: fb를 spread하되, themes/industry/sector는 위에서 이미 정의했으므로 제외
                # (fb의 themes가 빈 리스트로 entry의 themes를 덮어쓰는 버그 수정)
                **{
                    k: v
                    for k, v in fb.items()
                    if k not in ("themes", "industry", "sector")
                },
            }
            if disclosures:
                entry["disclosures"] = disclosures
            # 카페 유래 테마 마킹(2단계 b) — themes 는 순수 문자열 유지, 카페 추가분만
            # 별도 필드로 구분 노출(프론트가 향후 별도 스타일링 가능). 추가 0건 시 생략.
            if _cafe_added:
                entry["cafe_themes"] = _cafe_added

            # 임무 C 시정 (FLR-20260424-FLR-001): index_multiple_current = ratio (배수).
            # market 분기: KOSPI/KOSDAQ 외(예: KONEX, 해외)는 None.
            # NOTE: 현재 종합지수(KOSPI/KOSDAQ)만 기준. KRX 원문 "10개 초과 업종은
            # 업종산업별주가지수 적용" 조항은 업종지수 수집 추가 후 2차 반영 예정
            # (stocks.industry가 KRX 표준 업종 아닌 통계청 세분류 → 매핑 테이블 필요).
            try:
                _mkt = (master["market"] if master else None) or ""
                _stock_chg = entry.get("change_pct")
                _idx_chg = index_change_pct.get(_mkt)
                _ratio, _met_eligible = _compute_index_ratio(_stock_chg, _idx_chg)
                if _idx_chg is not None:
                    entry["index_change_pct"] = _idx_chg
                if _ratio is not None:
                    entry["index_multiple_current"] = _ratio
                    entry["index_multiple_eligible"] = _met_eligible
            except Exception:
                pass

            # 240영업일 가격 레인지 (REQ-001 Phase 2, v4 옵션 B — 시점별 재계산)
            # 우선순위:
            #  1) dailybars 시계열에서 today 이전 240영업일로 재계산 (옵션 B, 시점별 정확)
            #  2) fallback: stocks 6컬럼 (오늘 기준 단일 값, 옵션 A)
            range_obj = _calc_range_240d_at(conn, code, today, entry.get("close_price"))
            if not range_obj:
                try:
                    m_high = master["price_high_240d"] if master else None
                    m_low = master["price_low_240d"] if master else None
                    if m_high and m_low and m_high > 0 and m_low > 0:
                        cur_for_range = entry.get("close_price")
                        range_obj = {
                            "high": m_high,
                            "high_date": (
                                master["price_high_240d_date"] if master else ""
                            )
                            or "",
                            "high_pct": master["pct_from_high_240d"]
                            if master
                            else None,
                            "low": m_low,
                            "low_date": (
                                master["price_low_240d_date"] if master else ""
                            )
                            or "",
                            "low_pct": master["pct_from_low_240d"] if master else None,
                            "current": cur_for_range,
                        }
                except (IndexError, KeyError):
                    range_obj = None
            if range_obj:
                entry["range_240d"] = range_obj

            # REQ-pm320-ux-cycle #3 — 20영업일 일봉 OHLC 시계열 (카드 head 캔들 SVG용).
            # range_240d와 동일 dailybars 소스, LIMIT 20만 분리 SELECT (성능 영향 미미).
            d20 = _calc_daily_20(conn, code, today)
            if d20:
                entry["daily_20"] = d20

            # 종목 상태 뱃지 — 공시 기반 거래소 조치
            # FLR-20260422 대표 지적: label 기준 dedup 집합 — 기존 dict 비교는
            # summary/period 미세차이 통과 → 화면 중복 노출(퍼스텍 '투자경고 예고' 2회).
            # label(정확 문자열) 1회만 허용. 공시 기반 → 14일 히스토리 → predicted 순서로
            # 우선순위, 이미 존재하는 label은 이후 단계에서 skip.
            status_badges: list[dict] = []
            seen_badge_labels: set[str] = set()
            badge_cats = {
                "투자주의": "caution",
                "투자경고": "warning",
                "투자위험": "danger",
                "거래소조치": "danger",
                "단기과열": "hot",
                "관리종목": "danger",
                "상장폐지": "danger",
                # REQ-047 — 거래정지 카테고리 (KRX 매매거래정지 공시).
                # SK증권/파두 케이스: cat='거래정지' 공시 1건 → status_badge 부착 + effect_badges 산출.
                # 대표 발화 (2026-04-28 03:08 KST): 라이브 effect_badges=[] 회귀 차단.
                "거래정지": "danger",
            }
            for d in disclosures:
                cat = d.get("category") or ""
                rtitle = d.get("raw_title") or d.get("title") or ""
                # FLR-010: KRX 공식 단계만 라벨로. cat이 진짜 단계, '지정예고' 패턴이면 '예고'.
                # rtitle 키워드 추측 폐기 (예: '[투자주의]투자경고종목 지정예고' → cat=투자경고가 정답)
                for badge_cat, severity in badge_cats.items():
                    if cat == badge_cat or (not cat and badge_cat in rtitle):
                        is_notice = (
                            ("지정예고" in rtitle)
                            or ("(예고)" in rtitle)
                            or (badge_cat in ("단기과열",) and "예고" in rtitle)
                            # REQ-051 — 거래정지 예고 분리 (FLR-AGT-002 차단).
                            # KRX 공식 표기: raw_title='매매거래정지 예고' (조건부 시장감시규정 §5의3).
                            # 본문 분석으로 추가 차단:
                            #   - title에 '있어 예고' / '있을 경우' / '있는 경우' / '될 수 있다는 예고'
                            # 대표 비판 (2026-04-28 03:14 KST): "파두도 내일 거래정지가 되는게 아니라 예고였네"
                            or (
                                badge_cat == "거래정지"
                                and (
                                    "예고" in rtitle
                                    or "있어 예고" in (d.get("title") or "")
                                    or "있을 경우" in (d.get("title") or "")
                                    or "있는 경우" in (d.get("title") or "")
                                    or "될 수 있다" in (d.get("title") or "")
                                )
                            )
                        )
                        label = f"{badge_cat} 예고" if is_notice else badge_cat
                        # FLR-20260422: 동일 label 중복 차단 (공시 DB에 동일 단계 여러 건
                        # 등록된 경우 — 퍼스텍 '투자경고 예고' 2회 노출 사례)
                        if label in seen_badge_labels:
                            break
                        badge: dict = {"label": label, "severity": severity}
                        ps = d.get("period_start")
                        pe = d.get("period_end")
                        if ps:
                            badge["start"] = ps
                        if pe:
                            badge["end"] = pe
                        # 상세 사유
                        b_summary = d.get("title") or ""
                        if b_summary:
                            badge["summary"] = b_summary
                        b_cond = d.get("condition")
                        if b_cond:
                            badge["condition"] = b_cond
                        b_reg = d.get("regulation_period")
                        if b_reg:
                            badge["regulation"] = b_reg
                        # FLR-011 v5: view_date(t) + 익일 거래일 주입.
                        # renderer가 b.end(미래)를 "현재"로 잘못 표기하던 버그 차단.
                        badge["view_date"] = today
                        try:
                            badge["next_trading_day"] = _add_trading_days(today, 1)
                        except Exception:
                            pass
                        badge["single_price"] = _single_price_for_badge(badge)
                        # REQ-019 §V (DSN-004 v9.4): disclosure 카드 source 명시
                        # — utils.js renderEntryWindow 가드용
                        badge["source"] = "disclosure"
                        # REQ-019 §I: 투자경고 예고 entry_threshold_* 5필드 부착
                        _attach_entry_threshold(conn, badge, code, today)
                        # FLR-20260422: label-set dedup (dict 비교는 미세차이 통과 결함)
                        if label not in seen_badge_labels:
                            status_badges.append(badge)
                            seen_badge_labels.add(label)

                        # 임계가격 계산 -- 현재가 기반
                        close_price = entry.get("close_price")
                        if close_price and badge.get("condition"):
                            thresholds = _calc_thresholds(
                                conn, code, close_price, today, badge["condition"]
                            )
                            if thresholds:
                                badge["thresholds"] = thresholds
                        # 단기과열 만료일 자동 계산 — "N거래일 이내" 패턴 + period_start
                        cond_text = badge.get("condition") or ""
                        if (
                            "단기과열" in label
                            and badge.get("start")
                            and not badge.get("end")
                        ):
                            import re as _re3

                            m_days = _re3.search(r"(\d+)\s*거래일\s*이내", cond_text)
                            if m_days:
                                n_days = int(m_days.group(1))
                                badge["end"] = _add_trading_days(badge["start"], n_days)

                        # 날짜 명확화: "N일간" -> 실제 날짜 범위
                        # REQ-010 §2 (4/24 027360): LLM summary가 이미 "YYYY-MM-DD N일간"
                        # 형태로 prefix 날짜를 박은 경우, prefix까지 함께 매칭/치환 하여
                        # 중복 방지. (이전: "2026-04-27 1일간" → "2026-04-27 2026-04-27 당일")
                        # 단일 regex로 prefix optional 처리 — 중첩 매칭 회피.
                        if badge.get("summary") and badge.get("start"):
                            import re as _re

                            def _clarify_days(match, _badge=badge):
                                n = int(match.group(2))
                                s = datetime.strptime(_badge["start"], "%Y-%m-%d")
                                e = s + timedelta(days=n - 1)
                                if n == 1:
                                    return f"{_badge['start']} 당일"
                                return f"{_badge['start']} ~ {e.strftime('%Y-%m-%d')} ({n}일간)"

                            badge["summary"] = _re.sub(
                                r"(\d{4}-\d{2}-\d{2}\s+)?(?<!\()(\d+)일간",
                                _clarify_days,
                                badge["summary"],
                            )

                        break

            # 최근 14일 공시에서도 현재 유효한 상태 확인
            if not status_badges:
                since_14d = (
                    datetime.strptime(today, "%Y-%m-%d") - timedelta(days=14)
                ).strftime("%Y-%m-%d")
                hist_discs = conn.execute(
                    """SELECT title, disclosure_cat, period_start, period_end,
                              summary, condition_text, regulation_period, source_url
                       FROM disclosures
                       WHERE stock_code=? AND date BETWEEN ? AND ?
                       AND disclosure_cat IN ('투자주의','투자경고','투자위험','거래소조치','관리종목','상장폐지','단기과열')
                       ORDER BY date DESC LIMIT 3""",
                    (code, since_14d, today),
                ).fetchall()
                for hd in hist_discs:
                    pe = hd["period_end"]
                    if pe and pe >= today:
                        hd_cat = hd["disclosure_cat"] or ""
                        hd_title = hd["title"] or ""
                        for badge_cat, severity in badge_cats.items():
                            if hd_cat == badge_cat or (
                                not hd_cat and badge_cat in hd_title
                            ):
                                is_notice = (
                                    ("지정예고" in hd_title)
                                    or ("(예고)" in hd_title)
                                    or (
                                        badge_cat in ("단기과열",)
                                        and "예고" in hd_title
                                    )
                                )
                                label = f"{badge_cat} 예고" if is_notice else badge_cat
                                badge = {
                                    "label": label,
                                    "severity": severity,
                                    "start": hd["period_start"],
                                    "end": pe,
                                }
                                # 상세 사유
                                hd_summary = hd["summary"] or hd["title"] or ""
                                if hd_summary:
                                    badge["summary"] = hd_summary
                                if hd["condition_text"]:
                                    badge["condition"] = hd["condition_text"]
                                if hd["regulation_period"]:
                                    badge["regulation"] = hd["regulation_period"]
                                # FLR-011 v5: view_date(t) + 익일 거래일.
                                badge["view_date"] = today
                                try:
                                    badge["next_trading_day"] = _add_trading_days(
                                        today, 1
                                    )
                                except Exception:
                                    pass
                                badge["single_price"] = _single_price_for_badge(badge)
                                # REQ-019 §V (DSN-004 v9.4): disclosure 카드 source 명시
                                badge["source"] = "disclosure"
                                # REQ-019 §I: 투자경고 예고 entry_threshold_* 5필드 부착
                                _attach_entry_threshold(conn, badge, code, today)
                                # FLR-20260429: history fallback badge에도 disclosure_url + disclosure_title 부착.
                                # L3081 별도 매칭 루프는 당일 disclosures 변수만 검사 → history fallback badge는
                                # 매칭 누락. 4/29 058430 케이스(당일 공시 0건, 14d 히스토리만 보유)에서 url 0건.
                                if hd["source_url"]:
                                    badge["disclosure_url"] = hd["source_url"]
                                    badge["disclosure_title"] = hd["title"] or ""
                                # FLR-20260422: label-set dedup 일관 적용
                                if label in seen_badge_labels:
                                    break
                                status_badges.append(badge)
                                seen_badge_labels.add(label)

                                # 임계가격 계산 (14일 히스토리)
                                close_price = entry.get("close_price")
                                if close_price and badge.get("condition"):
                                    thresholds = _calc_thresholds(
                                        conn,
                                        code,
                                        close_price,
                                        today,
                                        badge["condition"],
                                    )
                                    if thresholds:
                                        badge["thresholds"] = thresholds

                                # 날짜 명확화 (REQ-010 §2: prefix 중복 시 swallow)
                                if badge.get("summary") and badge.get("start"):
                                    import re as _re2

                                    def _clarify_days2(match, _badge=badge):
                                        n = int(match.group(2))
                                        s = datetime.strptime(
                                            _badge["start"], "%Y-%m-%d"
                                        )
                                        e = s + timedelta(days=n - 1)
                                        if n == 1:
                                            return f"{_badge['start']} 당일"
                                        return f"{_badge['start']} ~ {e.strftime('%Y-%m-%d')} ({n}일간)"

                                    badge["summary"] = _re2.sub(
                                        r"(\d{4}-\d{2}-\d{2}\s+)?(?<!\()(\d+)일간",
                                        _clarify_days2,
                                        badge["summary"],
                                    )
                                break

            # v4: 공시 무관 사전 조건 자체 검사 (대표 v4 지시 #2)
            # REQ-003 (2026-04-24): 투자위험 predicted는 "투자경고 entered + 투자위험 predicted"
            # 이중 상태를 위해 status_badges 존재 여부와 무관하게 평가한다 (DSN-001 §20.7 목업 D).
            # 다른 predicted 라벨(투자주의/투자경고/단기과열 근접)은 기존 가드 유지 —
            # 공시 확정 배지 있으면 중복 회피.
            if entry.get("close_price"):
                predicted = _predict_status_from_dailybars(
                    conn, code, today, entry["close_price"]
                )
                try:
                    _next_td = _add_trading_days(today, 1)
                except Exception:
                    _next_td = None
                _has_confirmed = bool(status_badges)
                for pb in predicted:
                    pb_label = pb.get("label", "")
                    if not pb_label:
                        continue
                    # 이중 상태 허용 라벨: '투자위험 근접' — 투자경고 확정 배지가 있어도 추가.
                    _is_dual_allowed = pb_label == "투자위험 근접"
                    if _has_confirmed and not _is_dual_allowed:
                        continue
                    if pb_label in seen_badge_labels:
                        continue
                    # 투자위험 확정 라벨('투자위험' 또는 '투자위험 예고')이 이미 있으면
                    # predicted 중복은 회피 (상위 단계 우선).
                    if pb_label == "투자위험 근접" and any(
                        (lb.startswith("투자위험") and "근접" not in lb)
                        for lb in seen_badge_labels
                    ):
                        continue
                    pb["view_date"] = today
                    if _next_td:
                        pb["next_trading_day"] = _next_td
                    # DSN-004 v9.1 §I.2: predicted 시제 칩 분기 결정론화.
                    # frontend renderer가 viewDate 기준 다음 거래일을 재산출하지
                    # 않도록 build 시점에 predicted 전용 필드 주입. 캐시 일관성.
                    # 1차 소스: togusa rules/_whitelist/korea-trading-holidays.json
                    # (commit 08dc440). 2027 estimated hit 시 process 1회 console warn.
                    _ntd_strict, _ntd_grade = _next_trading_day_strict(today)
                    if _ntd_strict:
                        pb["next_trading_day_for_predicted"] = _ntd_strict
                        pb["next_trading_day_source"] = _ntd_grade
                    pb["single_price"] = _single_price_for_badge(pb)
                    status_badges.append(pb)
                    seen_badge_labels.add(pb_label)

            if status_badges:
                # DSN-20260422-001 v5.1 §7.4: 공시 예고 배지에 reason_text/prev_stage 주입.
                # prev_stage = 동일 종목의 "지정 중"(is_notice=False) 배지 label 재사용
                # (tempo=active ≈ label에 '예고' 미포함). 없으면 null → frontend 폴백.
                # REQ-010 §4 (togusa 권고): predicted source 배지는 active_label 후보에서
                # 제외. predicted 배지(예: '투자위험 근접')는 "다음 단계 예측"이므로 공시
                # 예고의 prev_stage(이전 단계)로 매핑하면 의미 swap이 발생한다 — 4/24 027360
                # 사례에서 '투자경고 예고'.prev_stage="투자위험 근접" 발견 (REQ-003 §결함 #4).
                active_label = next(
                    (
                        b.get("label")
                        for b in status_badges
                        if b.get("label")
                        and "예고" not in b["label"]
                        and b.get("source") != "predicted"
                    ),
                    None,
                )
                for b in status_badges:
                    b_label = b.get("label") or ""
                    if "예고" not in b_label:
                        continue
                    # reason_text: DART/KIND 공시 요약이 있을 때만 주입.
                    # 임무 3 (DOC-20260423 야간): placeholder 'X 요건 충족' 금지 —
                    # LLM 해석본 없으면 reason_text 필드 미주입 → renderer에서 §2 사유 블록 미노출.
                    # 과거 placeholder는 "근거 없는 추측 텍스트"로 대표 원칙 위반.
                    if b.get("summary"):
                        b["reason_text"] = b["summary"]
                    # prev_stage: 동일 종목 active 단계 배지 label (없으면 null)
                    b["prev_stage"] = active_label
                # REQ-020 v9.5 §V — 효과 배지 산출 (build_daily.py SSOT).
                # status_badges 1건당 effect_badges[] 부착. 카드 단위 머지·dedup·정렬은
                # utils.js collectEffectBadges 책임.
                # P0 함정 차단: _compute_effect_badges 내부 view_date 거래일 가드.
                # REQ-020a — predicted strict 3 AND 평가 위해 all_badges 전달 (인접 검증).
                for b in status_badges:
                    b["effect_badges"] = _compute_effect_badges(b, today, status_badges)
                # REQ-027 §C — disclosure_url 부착 (SPEC-001 §V.3).
                # status_badge.label과 disclosures[].category 매칭하여 공시 url 부착.
                # 사유 박스 row의 url 필드로 전달 → 클라이언트에서 "공시" 칩 노출.
                # REQ-063 §A — disclosure_title(raw_title) 동시 부착.
                # 링크 텍스트를 풀 제목으로 표시 (cal-disc-item과 정합 — "공시" 일반 텍스트 폐기).
                for b in status_badges:
                    bl = b.get("label") or ""
                    if not bl or b.get("disclosure_url"):
                        continue
                    for d in disclosures or []:
                        cat = d.get("category") or ""
                        if cat and cat in bl and d.get("url"):
                            b["disclosure_url"] = d["url"]
                            # REQ-063 — raw_title 우선, 폴백으로 title (LLM 가공 제목).
                            b["disclosure_title"] = (
                                d.get("raw_title") or d.get("title") or ""
                            )
                            break
                # REQ-029 §V.2 롤백 (SPEC-001 22:58 KST 갱신) — REQ-027 §D 부정확 처리 제거.
                # 정확한 KRX 코스닥 업무규정 제23조의2: 예고 ≠ 지정. 예고일은 평가 기간 시작일.
                # 평가일(예고+1~10거래일) 4가지 모두(주가상승률·거래회전율·주가변동성·종가상승) 충족 시 익일 지정.
                # 즉 예고일 종가 상승만으로 익일 단기과열 지정 자동 보장 X (4가지 충족 시점이 익일 이후일 수 있음).
                # 거짓 충실성 차단 (FLR-AGT-002): 예고 시점 신용불가/단일가(내일) 배지 부착 = 거짓 확정성.
                # 단기과열 예고 effect_badges는 _compute_effect_badges에서 빈 배열([]) 유지 (REQ-023 v9.8 정합).
                # 지정 확정 공시 후 본 지정 분기(L1058-1070)에서만 effect_badges 부착.
                pass  # status_badges 후속 ka10017 머지에서 entry 부착

            # ─── 상한가 판정 = v1 결과목록 등락률 >= 29.79% (D축, 대표 확정 2026-06-16) ──
            # 종전 REQ-080 ka10017 stock_status_badges SoT 머지(+ OHLC-SOT-UNIFY /
            # IPO-BYPASS / STALE-GUARD cascade) 전면 폐기. 사유:
            #   조건검색이 'v1'(거래대금 500억 ∪ 당일 상한가)로 통합되어 v1 결과
            #   목록 자체가 상한가 종목을 포함하고, 각 항목에 등락률(change_pct)을
            #   이미 제공한다 (kiwoom-scraper parse_kiwoom_stock field 12 / 1000).
            #   따라서 상한가 = "v1 목록 중 등락률 >= 29.79%" 단일 기준으로 판정.
            #   ka10017 별도조회·payload cur_prc/flu_rt 재조정 로직 불요
            #   (collect_kiwoom_limit_up 폐기와 정합, DSN-001 §2.3/§2.5).
            # SoT 판정값 = k.get("change_pct") (v1 목록 항목 등락률). 카드 노출
            #   등락률(change = 1순위 dailybars / 2순위 k.change_pct, L3426~)과
            #   동일 v1 source 계열이라 카드↔배지 mismatch 구조적 무발생
            #   (종전 OHLC-SOT-UNIFY 가 싸우던 ka10017 vs dailybars 괴리 소멸).
            _v1_chg = k.get("change_pct")
            _is_limit_up = _v1_chg is not None and float(_v1_chg) >= LIMIT_UP_THRESHOLD
            if _is_limit_up and "상한가" not in seen_badge_labels:
                # 배지 표기 등락률 = 카드 노출값(change) 우선, 부재 시 v1 목록값.
                # 둘 다 v1 계열이므로 정합 (카드와 동일 숫자 노출).
                _badge_chg = change if change is not None else float(_v1_chg)
                # REQ-082 — consecutive_count: daily_picks.change_pct chain
                # (LIMIT_UP_THRESHOLD 정합, _calc_limit_up_streak).
                consecutive_count = _calc_limit_up_streak(conn, code, today)
                limit_up_badge = {
                    "label": "상한가",
                    "severity": "hot",
                    "source": "v1_condition",
                    "view_date": today,
                    "summary": f"{k.get('name', '')} 상한가 ({_badge_chg:.2f}%)",
                    "cur_prc": entry.get("close_price") or k.get("last_price"),
                    "flu_rt": _badge_chg,
                    "trde_qty": k.get("volume"),
                    "trde_prica_calc": k.get("trade_amount")
                    or k.get("max_trade_amount"),
                    "consecutive_count": consecutive_count,
                    # REQ-082 Phase2 — dsn-v95-effect-badges 시스템 통합.
                    # 살아있는 chip 라인은 status_badges[].effect_badges[] →
                    # utils.js collectEffectBadges. consecutive_count 보존 →
                    # dsnV95FormatEffectBadge가 cc>=2 시 +N 부착.
                    "effect_badges": [
                        {
                            "effect": "limit-up",
                            "when": "today",
                            "severity": "hot",
                            "source_label": "상한가",
                            "source_kind": "limit_up",
                            "consecutive_count": consecutive_count,
                        }
                    ],
                }
                try:
                    limit_up_badge["next_trading_day"] = _add_trading_days(today, 1)
                except Exception:
                    pass
                status_badges.append(limit_up_badge)
                seen_badge_labels.add("상한가")

            if status_badges:
                entry["status_badges"] = status_badges

            # 분봉 스냅샷 (sparkline)
            # base는 전일 종가 — 상한가(span=0) 회피 + 등락률 기준선 일치
            ir = intraday_by_code.get(code)
            if ir:
                try:
                    close_price = k.get("last_price") or k.get("close")
                    prev_close = None
                    if close_price and change is not None:
                        try:
                            prev_close = round(close_price / (1 + float(change) / 100))
                        except Exception:
                            prev_close = None
                    entry["intraday"] = {
                        "step_min": ir["step_min"],
                        "open": ir["open"],
                        "base": prev_close or ir["open"],
                        "prices": json.loads(ir["prices_json"]),
                    }
                except Exception:
                    pass

            # 신용불가 판정 — KIND 거래소조치 공시 기반
            # TODO: stocks.listed_at 컬럼 추가 후 상장일+7일 신규상장 윈도우 병합
            credit_risk_keywords = (
                "투자주의",
                "투자경고",
                "투자위험",
                "매매거래정지",
                "관리종목",
                "상장폐지",
                "불성실공시",
            )
            credit_risk_cats = ("관리종목", "상장폐지", "거래소조치")
            credit_risk_by_disc = False
            for d in disclosures:
                title = d.get("title") or ""
                cat = d.get("category") or ""
                if any(kw in title for kw in credit_risk_keywords):
                    credit_risk_by_disc = True
                    break
                if cat in credit_risk_cats:
                    credit_risk_by_disc = True
                    break

            # 신용불가 판정 (정답 우선순위):
            # 1) kt20017 단건 조회 (당일 실시간) — 있으면 정답. "가능"이면 확정 가능.
            # 2) kt20016 등급별 풀 — 폴백. kt20017 결과 없을 때만 사용.
            # 3) 공시 기반 — 거래소 조치(관리/상장폐지) 공시 있으면 위험 플래그
            ss = stock_status.get(code) if stock_status else None
            if ss is not None:
                if ss[0]:  # limit_exceeded=1
                    credit_risk = True
                else:
                    credit_risk = credit_risk_by_disc
            elif credit_eligible:
                credit_risk = credit_risk_by_disc or (code not in credit_eligible)
            else:
                credit_risk = credit_risk_by_disc
            if credit_risk:
                entry["credit_risk"] = True
                entry["credit_reason"] = _build_credit_reason(
                    disclosures, today, code=code, stock_status=stock_status
                )

            # 토구사/휴지 검증 결과 반영
            togusa = conn.execute(
                "SELECT verdict, is_theme_leader, theme_leader_rank FROM togusa_verdicts WHERE date=? AND stock_code=?",
                (today, code),
            ).fetchone()
            if togusa:
                entry["togusa_verdict"] = togusa["verdict"]
                entry["theme_leader"] = bool(togusa["is_theme_leader"])

            gate = conn.execute(
                "SELECT grade, reason FROM hugepark_gate WHERE date=? AND stock_code=?",
                (today, code),
            ).fetchone()
            if gate:
                entry["hugepark_grade"] = gate["grade"]

            stocks_out.append(entry)
            # stock_themes 매핑 갱신
            for t in stocks_out[-1]["themes"]:
                try:
                    from .theme_normalizer import link_stock_theme

                    link_stock_theme(code, t, today, "ishikawa")
                except Exception:
                    pass

    out_path = OUT_DIR / f"stock-{today}.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 기존 파일의 해석 데이터 보존 (이시카와/토구사 해석이 파이프라인 재실행으로 소실 방지)
    # [FLR 참조: FLR-20260410-TEC-001] 매크로 요약은 DB가 SoT — JSON 폴백 제거
    _prev_interp = {}
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text())
            for ps in prev.get("stocks", []):
                if ps.get("causal_chain") or ps.get("themes"):
                    _prev_interp[ps["code"]] = ps
        except Exception:
            pass

    for s in stocks_out:
        if s["code"] in _prev_interp:
            pi = _prev_interp[s["code"]]
            if not s.get("themes") and pi.get("themes"):
                # 정규 테마만 보존 (업종명 순환 방지)
                from .interpret_stocks import _load_theme_dictionary

                _canonical = set(_load_theme_dictionary())
                preserved = [
                    t for t in _normalize_themes(pi["themes"]) if t in _canonical
                ]
                if preserved:
                    s["themes"] = preserved
            if not any(n.get("causal_chain") for n in s.get("news", [])):
                s["causal_chain"] = pi.get("causal_chain", "")
                s["parent_theme"] = pi.get("parent_theme", "")
                for n in s.get("news", []):
                    if not n.get("causal_chain"):
                        n["causal_chain"] = pi.get("causal_chain", "")

    # 매크로 이벤트 — DB가 Source of Truth
    macro_events = []
    # 로봇기사 접두사 패턴 — summary에서 제거
    _ROBOT_PREFIXES = (
        "[강세 토픽] ",
        "[급등] ",
        "[특징주] ",
        "[시황] ",
        "[마감시황] ",
        "[장중시황] ",
        "[개장시황] ",
    )

    def _has_excessive_ascii(text: str, threshold: float = 0.5) -> bool:
        """ASCII 비율이 threshold 이상이면 영문이 과다하다고 판단."""
        if not text:
            return False
        ascii_chars = sum(1 for c in text if ord(c) < 128 and c.isalpha())
        alpha_chars = sum(1 for c in text if c.isalpha())
        if alpha_chars == 0:
            return False
        return ascii_chars / alpha_chars > threshold

    with connect() as conn:
        # LLM/interpret 매크로 (고품질 요약) 우선, 그 다음 extract 폴백
        # DOC-20260605-DSN(arch-pipeline §7.11): LIMIT 10 제거 — 후보 풀 전체를 가져온
        # 뒤 Python 단에서 의미중복 병합(dedup) + 중요도 가중 정렬 후 top-10 선발.
        # source_count 단일 정렬이 오후 신규 매크로에 오전 핵심을 밀어내던 root 봉쇄.
        # created_at = 발생 origin 시각(extract_macros morning_pin 보존분) — tie-break 에 사용.
        rows = conn.execute(
            """SELECT DISTINCT keyword, summary, source_count, source,
                      COALESCE(verified, 1) as verified, created_at
               FROM macro_events
               WHERE date=? AND summary IS NOT NULL
                 AND (source IN ('interpret', 'llm') OR summary != keyword)
               ORDER BY
                 CASE WHEN COALESCE(verified, 1) = 0 THEN 1 ELSE 0 END,
                 CASE WHEN source = 'interpret' THEN 0
                      WHEN source = 'llm' THEN 1
                      WHEN source = 'fallback' THEN 3
                      ELSE 2 END,
                 source_count DESC""",
            (today,),
        ).fetchall()
        # 후보 정제: 접두사 제거 + 영문과다 제외 (정렬·dedup 전 단계)
        _candidates = []
        for r in rows:
            summary = r["summary"]
            # 기존 DB에 남아있을 수 있는 "(미검증)" 접두사 제거
            if summary.startswith("(미검증) "):
                summary = summary[len("(미검증) ") :]
            # 로봇기사 접두사 제거
            for prefix in _ROBOT_PREFIXES:
                if summary.startswith(prefix):
                    summary = summary[len(prefix) :]
                    break
            # 영문 과다 감지 — LLM이 번역을 누락한 경우 경고 + 제외
            if _has_excessive_ascii(summary):
                print(
                    f"[build_daily] WARNING: 영문 과다 매크로 제외 — {r['keyword']}: {summary[:80]}"
                )
                continue
            _candidates.append(
                {
                    "title": r["keyword"],
                    "summary": summary,
                    "source_count": r["source_count"] or 0,
                    "verified": int(r["verified"] or 0),
                    "created_at": r["created_at"] or "",
                }
            )
        # 의미중복 병합 + 중요도 가중 정렬 → top-10 선발 (DSN §7.11)
        macro_events = _dedup_and_rank_macros(_candidates, limit=10)
        # 내러티브도 DB에서
        narr_rows = conn.execute(
            "SELECT narrative FROM daily_narratives WHERE date=? ORDER BY id",
            (today,),
        ).fetchall()
        narratives = [r["narrative"] for r in narr_rows]

        # Phase 5 (REQ-20260420-REQ-004): themes.db read-only 참조 (있을 때만)
        # 기존 macro_events/narratives는 유지(동일성 회귀 H6 방지). 추가 필드로만 노출.
        themes_macro_summary = []
        themes_tree = None
        try:
            tm_rows = conn.execute(
                """SELECT keyword, summary, source_count, verified
                   FROM themes_db.macro_summary
                   WHERE date=? ORDER BY rank ASC""",
                (today,),
            ).fetchall()
            themes_macro_summary = [
                {
                    "keyword": r["keyword"],
                    "summary": r["summary"],
                    "source_count": r["source_count"],
                    "verified": bool(r["verified"]),
                }
                for r in tm_rows
            ]
            tree_row = conn.execute(
                "SELECT tree_json FROM themes_db.theme_map WHERE date=?",
                (today,),
            ).fetchone()
            if tree_row:
                themes_tree = json.loads(tree_row["tree_json"])
        except Exception:
            # themes.db 미배포 또는 쿼리 실패 시 graceful skip
            pass

    out_path.write_text(
        json.dumps(
            {
                "date": today,
                "generated_at": datetime.now().isoformat(),
                "data_source": data_source,
                "macro_events": macro_events,
                "narratives": narratives,
                "themes_macro_summary": themes_macro_summary,
                "themes_tree": themes_tree,
                "stocks": stocks_out,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    _update_calendar_index(today, len(stocks_out), news_total)
    print(
        f"wrote {out_path} ({len(stocks_out)} stocks, {news_total} news, {len(macro_events)} macros)"
    )

    # B3 fix: 테마 통계 + 트리 자동 갱신 (build_daily 실행 시 항상)
    try:
        from .build_theme_stats import build as build_theme_stats

        build_theme_stats()
    except Exception as e:
        print(f"[build_daily] WARNING: theme stats rebuild failed — {e}")

    # REQ-024 §1.5: sw.js CACHE_NAME 자동 동기화 (utils.js 버전 토큰 기준)
    try:
        _sync_sw_cache_name()
    except Exception as e:
        print(f"[build_daily] WARNING: sw.js cache sync failed — {e}")

    # cycle22 Q-CYCLE22-001 Phase 3: per-stock dailybars 240영업일 JSON emit
    # 본 emit 본질: 카드 일봉캔들 클릭 확대 차트 (REQ-001/SPEC-001) lazy fetch 데이터 공급.
    # 일자 JSON inline 추가 시 모바일 페이로드 +180~300 KB/일자 폭증 → per-stock 분리.
    # POC P0-2 결정 옵션 C 정합 (서버 raw 공급 / 클라이언트 8종 + 후행 서버 5종 사전 계산).
    # 출력: HOMEPAGE/data/dailybars/{code}.json (cron 화이트리스트 pipeline.sh + kiwoom_cron.sh 정합).
    # 실패 시 graceful skip — 한 종목 실패가 전체 build 차단하지 않음 (DSN §7 data 정합 자동 검증 정합).
    try:
        from .emit_dailybars_per_stock import emit_for_stocks

        codes_with_names = [(s["code"], s.get("name") or "") for s in stocks_out]
        result = emit_for_stocks(codes_with_names, today)
        print(
            f"[build_daily] dailybars per-stock emit: "
            f"emitted={result['emitted']} skipped={result['skipped']} "
            f"total={result['total']}"
        )
    except Exception as e:
        print(f"[build_daily] WARNING: dailybars per-stock emit failed — {e}")

    return out_path


def backfill_ohlc():
    """daily_picks에서 OHLC가 null인 행을 키움 일봉 데이터로 복구.

    키움 JSON의 daily_top/accumulated_stocks에서 해당 날짜의 open/high/low를
    찾아 UPDATE. JSON에도 없으면 건너뜀 (API 호출 불필요).
    """
    kiwoom_dir = HOMEPAGE / "data" / "kiwoom"
    updated = 0
    with connect() as conn:
        nulls = conn.execute(
            """SELECT date, stock_code FROM daily_picks
               WHERE open_price IS NULL OR high_price IS NULL OR low_price IS NULL"""
        ).fetchall()
        if not nulls:
            print("[backfill_ohlc] OHLC null 행 없음 — 스킵")
            return 0

        # 날짜별로 키움 JSON 캐싱
        kiwoom_cache: dict[str, dict] = {}
        for row in nulls:
            dt, code = row["date"], row["stock_code"]
            if dt not in kiwoom_cache:
                fpath = kiwoom_dir / f"{dt}.json"
                if fpath.exists():
                    try:
                        data = json.loads(fpath.read_text())
                        by_ticker = {}
                        for src_key in (
                            "accumulated_stocks",
                            "daily_top",
                            "latest_stocks",
                        ):
                            items = data.get(src_key, {})
                            if isinstance(items, dict):
                                items = list(items.values())
                            for item in items:
                                t = item.get("ticker") or item.get("code")
                                if t and item.get("open"):
                                    by_ticker[t] = item
                        kiwoom_cache[dt] = by_ticker
                    except Exception:
                        kiwoom_cache[dt] = {}
                else:
                    kiwoom_cache[dt] = {}

            stock_data = kiwoom_cache[dt].get(code)
            if not stock_data:
                continue

            o = stock_data.get("open")
            h = stock_data.get("high")
            lo = stock_data.get("low")
            if not (o or h or lo):
                continue

            conn.execute(
                """UPDATE daily_picks
                   SET open_price=COALESCE(open_price, ?),
                       high_price=COALESCE(high_price, ?),
                       low_price=COALESCE(low_price, ?)
                   WHERE date=? AND stock_code=?""",
                (o, h, lo, dt, code),
            )
            updated += 1
        conn.commit()
    print(f"[backfill_ohlc] {updated}/{len(nulls)} 행 OHLC 복구 완료")
    return updated


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--backfill-ohlc":
        backfill_ohlc()
    elif len(sys.argv) > 1 and sys.argv[1] == "--rebuild-calendar-index":
        # calendar/index.json days 맵 DB 기준 full rebuild (멱등, 누락 자동 복구).
        # 서빙(cron worktree) 대상 1회 재빌드 진입점 — full build 없이 index 만 복구.
        # 사용: M1S_HOMEPAGE=/Users/seongjinpark/company/100m1s-homepage-cron \
        #       python3 -m scripts.news_pipeline.build_daily --rebuild-calendar-index
        days = rebuild_calendar_index()
        print(f"[rebuild-calendar-index] wrote {CAL_INDEX} ({len(days)} trading days)")
    elif len(sys.argv) > 1 and sys.argv[1].startswith("--date="):
        # REQ-048 — 특정 날짜 백필 빌드 (PIPELINE_DATE 환경변수 활용 — config.py 기존 지원).
        # 사용: python3 -m scripts.news_pipeline.build_daily --date=2026-04-27
        # 4/28 새벽 시점에 4/27 데이터 백필 필요 케이스 (REQ-039 강세 배지 후속).
        target_date = sys.argv[1].split("=", 1)[1]
        import os

        os.environ["PIPELINE_DATE"] = target_date
        build()
    else:
        build()
