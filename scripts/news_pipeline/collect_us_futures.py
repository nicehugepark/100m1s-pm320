"""미 지수 선물 장중 수집 — Yahoo Globex 5분봉 (Q-20260605-103 Phase 4).

국내장 중(한국 낮 시간) 미국 지수 선물 실시간 추적용. 야간 미장요약(Phase 3)이
*마감 데이터*라면, 본 모듈은 *장중 선물 living* 을 15분 주기로 갱신한다.

대상 (실측 PASS, dev-usfut-feasibility + 2026-06-05 재실측 HTTP 200):
  ES=F = S&P500 E-mini 선물 / NQ=F = 나스닥100 E-mini 선물 / YM=F = 다우 E-mini 선물
  endpoint: query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=5m
  meta.regularMarketPrice = 현재가, meta.chartPreviousClose = 직전 세션 settle
  (1d range 에서는 정상적 전일 대비 기준 — 1y daily 의 chartPreviousClose 와 의미 다름).

산출 (build_us_digest 가 JSON 의 futures 섹션에 부착):
  {as_of_kst, futures:[{name, label_note, point, change_pct, spark[]}]}
  - as_of_kst : 수집 시각 (KST) — 프론트 "N분 전 기준" stale 표시 의무 (실시간인 척 금지).
  - label_note: 현물 기준차 명시 ("나스닥100 선물" 등 — 현물 지수와 다른 기초자산).
  - change_pct: 직전 세션 settle 대비 (chartPreviousClose, fallback=첫 봉).

실패 시 None (build 단이 직전 JSON 유지 — stale 명시). 부분 산출물 금지(3종 all-present).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# 대상 (표시명, Yahoo 심볼, label_note=현물 기준차 명시)
US_FUTURES_TARGETS = [
    ("S&P500 선물", "ES=F", "S&P500 E-mini 선물 (현물 지수와 별개 기초자산)"),
    (
        "나스닥100 선물",
        "NQ=F",
        "나스닥100 E-mini 선물 (나스닥종합과 다른 100종목 기준)",
    ),
    ("다우 선물", "YM=F", "다우 E-mini 선물 (현물 지수와 별개 기초자산)"),
]

YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# range_240d 계산 윈도우 (최근 N 거래일) — 정규장 indices[] 와 동일 정책 (RANGE_WINDOW=240).
RANGE_WINDOW = 240

# 연속선물(front-month auto-roll) 만기 교체 갭 탐지 임계치 (일간 종가 절대 변동 %).
# Q-20260608-140 §3 rollover 처리: 본 수집기는 야후 *연속선물 ES=F/NQ=F/YM=F* 의
# 이미-롤된 시계열을 그대로 사용한다(단일 소스 = 야후 연속선물, 우리가 가격 조작 X).
# 실측(2026-06-08 ES/NQ/YM 252봉)상 >5% 갭 0건 — 지수선물 분기 롤 갭은 통상 <2%.
# 갭이 임계치를 넘으면 range_240d low/high 왜곡 위험 → ratio back-adjust(가격 합성)
# 대신 *보수적 갭 표기*만 수행(FLR-AGT-002 거짓 충실성 차단 — 실측 아닌 합성가 노출 금지).
# frontend/range 가 rollover_gaps 비어있지 않으면 "연속선물 롤 갭 존재" 각주 가능.
ROLLOVER_GAP_PCT = 5.0


def is_cme_futures_open(now_et: datetime | None = None) -> bool:
    """CME 주가지수 선물(ES/NQ/YM) 거래 중 여부 — 미 동부(ET) 벽시계 판정.

    거래 구간 (WebSearch 2건 corroborate + CME 공식, Q-20260608-139 §11.15):
      일요일 18:00 ET 개장 → 금요일 17:00 ET 마감, 매일 17:00~18:00 ET 유지보수 휴식(월~목).
    보수적 판정 (애매하면 False → frontend 정규장 fallback, FLR-AGT-002 거짓 충실성 차단):
      - 토요일 전일 / 일요일 18:00 이전 = 휴장
      - 금요일 17:00 이후 = 휴장
      - 평일 17:00~18:00 ET = 일일 유지보수 휴식 = 휴장
    국내장 윈도우(KST 09:00~15:30 = ET 19:00~01:30 직전야간)에서는 항상 거래 중이므로
    실사용 영향은 거의 없으나, edge(서버 시각 이상/장외 backfill)에서 stale settle 을
    실시간으로 위장하지 않도록 명시 플래그를 산출한다.
    """
    if now_et is None:
        now_et = datetime.now(ZoneInfo("America/New_York"))
    dow = now_et.weekday()  # 0=월 ... 5=토 6=일
    hour = now_et.hour
    if dow == 5:  # 토요일 = 전면 휴장
        return False
    if dow == 6:  # 일요일 = 18:00 ET 개장 후만 거래
        return hour >= 18
    if dow == 4 and hour >= 17:  # 금요일 17:00 ET 이후 마감
        return False
    # 월~금 평일: 17:00~18:00 ET 일일 유지보수 휴식만 휴장
    if hour == 17:
        return False
    return True


# spark 표본 상한 (5분봉 1일 ≈ 78~92봉 → 최근 N만 노출, 미니 스파크 과밀 방지).
# 선물 spark = 오늘 장중 5분봉 종가열(parse_chart). collect() 에서 첫 점에 당일
# 시가(candle.o)를 prepend → 방향(시가→현재) = candle o→c 색과 항상 일치
# (대표 결정 2026-06-08 14:55: "스파크 = 캔들의 확장, 당일 캔들과 같은 방향·색").
# 22일 추세는 daily_expanded(일봉 캔들 차트)가 담당(역할 분리).
SPARK_MAX = 40

# sanity: 선물 합리적 범위 (현물 지수 근방, 2026-06). 스케일/심볼 오인 탐지.
SANITY_RANGE = {
    "S&P500 선물": (1500.0, 30000.0),
    "나스닥100 선물": (5000.0, 100000.0),
    "다우 선물": (15000.0, 200000.0),
}


def _round2(v) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def fetch_chart(symbol: str, max_retries: int = 3) -> dict | None:
    """Yahoo 선물 5분봉 호출. 재시도 포함. 실패 시 None."""
    import urllib.parse

    enc = urllib.parse.quote(symbol)  # ES=F → ES%3DF
    url = f"{YAHOO_CHART_BASE}/{enc}"
    params = {"range": "1d", "interval": "5m"}
    headers = {"User-Agent": _UA}
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            print(f"[us-futures {symbol}] exception: {exc}", file=sys.stderr)
            time.sleep(2**attempt)
            continue
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"[us-futures {symbol}] 429, backoff {back}s", file=sys.stderr)
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(
                f"[us-futures {symbol}] http {r.status_code}: {r.text[:200]}",
                file=sys.stderr,
            )
            time.sleep(2**attempt)
            continue
        try:
            return r.json()
        except ValueError:
            print(f"[us-futures {symbol}] JSON parse fail", file=sys.stderr)
            return None
    return None


def fetch_daily(symbol: str, max_retries: int = 3) -> dict | None:
    """Yahoo 선물 일봉 호출 (range=1y&interval=1d → 252봉 OHLCV). 실패 시 None.

    5분봉 fetch_chart 와 동일 endpoint, params 만 다름. candle/range_240d/
    daily_expanded 산출용 (정규장 indices[] 와 동일 1y 단발 1콜). stale 패턴은
    호출측(collect)이 None → 직전 JSON 유지로 처리(부분 산출물 차단 불변식).
    """
    import urllib.parse

    enc = urllib.parse.quote(symbol)
    url = f"{YAHOO_CHART_BASE}/{enc}"
    params = {"range": "1y", "interval": "1d"}
    headers = {"User-Agent": _UA}
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            print(f"[us-futures-daily {symbol}] exception: {exc}", file=sys.stderr)
            time.sleep(2**attempt)
            continue
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"[us-futures-daily {symbol}] 429, backoff {back}s", file=sys.stderr)
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(
                f"[us-futures-daily {symbol}] http {r.status_code}: {r.text[:200]}",
                file=sys.stderr,
            )
            time.sleep(2**attempt)
            continue
        try:
            return r.json()
        except ValueError:
            print(f"[us-futures-daily {symbol}] JSON parse fail", file=sys.stderr)
            return None
    return None


def _ts_to_date(ts) -> str | None:
    """epoch(UTC) → 미국 동부 거래일 (YYYY-MM-DD). 정규장 indices[] 와 동일."""
    try:
        return datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).strftime(
            "%Y-%m-%d"
        )
    except (OSError, OverflowError, ValueError, TypeError):
        return None


def _detect_rollover_gaps(closes: list, timestamps: list) -> list[dict]:
    """연속선물 일간 종가 급변(롤 갭 후보) 탐지 → 표기용 리스트.

    가격 합성(back-adjust) 없이 *갭 위치·크기만* 산출(FLR-AGT-002 거짓 충실성:
    실측 아닌 합성가 노출 금지). 빈 리스트 = 갭 없음(실측 ES/NQ/YM 정상 케이스).
    """
    cleaned = [
        (c, timestamps[i] if i < len(timestamps) else None)
        for i, c in enumerate(closes)
        if c is not None
    ]
    gaps: list[dict] = []
    for i in range(1, len(cleaned)):
        prev_c = cleaned[i - 1][0]
        cur_c = cleaned[i][0]
        if not prev_c:
            continue
        pct = abs(cur_c - prev_c) / prev_c * 100
        if pct >= ROLLOVER_GAP_PCT:
            gaps.append(
                {
                    "date": _ts_to_date(cleaned[i][1]),
                    "from": _round2(prev_c),
                    "to": _round2(cur_c),
                    "gap_pct": round((cur_c - prev_c) / prev_c * 100, 2),
                }
            )
    return gaps


def _calc_range_240d(
    name: str, point: float, closes: list, timestamps: list
) -> dict | None:
    """최근 RANGE_WINDOW 거래일 종가 기준 range_240d dict. 정규장 indices[] 동일 로직.

    반환: {low, high, low_date, high_date, low_change_pct, high_change_pct}.
    유효 종가 < 2 시 None.
    """
    pairs = [
        (c, timestamps[i] if i < len(timestamps) else None)
        for i, c in enumerate(closes)
        if c is not None
    ]
    pairs = pairs[-RANGE_WINDOW:]
    if len(pairs) < 2:
        return None

    low_c, low_ts = min(pairs, key=lambda p: p[0])
    high_c, high_ts = max(pairs, key=lambda p: p[0])

    def _pct(base):
        try:
            return round((point - float(base)) / float(base) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    return {
        "low": _round2(low_c),
        "high": _round2(high_c),
        "low_date": _ts_to_date(low_ts),
        "high_date": _ts_to_date(high_ts),
        "low_change_pct": _pct(low_c),
        "high_change_pct": _pct(high_c),
    }


def parse_daily(name: str, raw: dict | None) -> dict | None:
    """선물 일봉 응답 → {candle, range_240d, daily_expanded, rollover_gaps}.

    정규장 indices[] parse_chart 의 candle/range_240d/daily_expanded 산출 로직을
    동일 shape 으로 재사용(frontend 가 같은 렌더 경로). 실패/구조 불일치 시 None
    → 부분 산출물 차단(collect 가 전체 None → 직전 JSON 유지, stale 명시).
    """
    if not raw:
        return None
    try:
        result = raw["chart"]["result"][0]
        result["meta"]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        print(f"[us-futures-daily {name}] 응답 구조 불일치", file=sys.stderr)
        return None

    if not timestamps:
        return None

    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    volumes = quote.get("volume") or []

    closes_clean = [c for c in closes if c is not None]
    if not closes_clean:
        return None

    # 마지막 유효 종가 인덱스 (장중 None 후행 방어) = 당일 candle 기준.
    last_idx = None
    for i in range(len(closes) - 1, -1, -1):
        if closes[i] is not None:
            last_idx = i
            break
    if last_idx is None:
        return None

    point = _round2(closes[last_idx])
    if point is None:
        return None

    # sanity 재검 (일봉 point 도 스케일/심볼 오인 차단 — 5분봉과 동일 SANITY_RANGE).
    lo, hi = SANITY_RANGE.get(name, (0.0, float("inf")))
    if not (lo <= point <= hi):
        print(
            f"[us-futures-daily {name}] sanity FAIL: point={point} (예상 {lo}~{hi})",
            file=sys.stderr,
        )
        return None

    candle = {
        "o": _round2(opens[last_idx]) if last_idx < len(opens) else None,
        "h": _round2(highs[last_idx]) if last_idx < len(highs) else None,
        "l": _round2(lows[last_idx]) if last_idx < len(lows) else None,
        "c": _round2(closes[last_idx]),
    }

    range_240d = _calc_range_240d(name, point, closes, timestamps)

    daily_expanded = []
    for i in range(len(closes)):
        c = closes[i]
        if c is None:
            continue
        d = _ts_to_date(timestamps[i]) if i < len(timestamps) else None
        if not d:
            continue
        bar = {
            "date": d,
            "o": _round2(opens[i]) if i < len(opens) else None,
            "h": _round2(highs[i]) if i < len(highs) else None,
            "l": _round2(lows[i]) if i < len(lows) else None,
            "c": _round2(c),
        }
        if i < len(volumes) and volumes[i] is not None:
            bar["v"] = volumes[i]
        daily_expanded.append(bar)

    rollover_gaps = _detect_rollover_gaps(closes, timestamps)

    # spark 는 여기서 산출하지 않음 (대표 결정 2026-06-08 14:55): 선물 spark = 오늘
    # 장중 path(parse_chart 의 5분봉 종가열) 로 환원 → 당일 candle 과 항상 같은 방향·색.
    # 22일 추세는 옆의 daily_expanded(일봉 캔들 차트)가 담당(역할 분리). collect() 에서
    # parsed.update(daily) 가 spark 를 덮지 않도록 본 dict 에 "spark" 키 미포함.
    return {
        "candle": candle,
        "range_240d": range_240d,
        "daily_expanded": daily_expanded,
        # 비어있으면 롤 갭 없음. 비어있지 않으면 frontend 각주("연속선물 롤 갭") 가능.
        # 가격 자체는 야후 연속선물 원본 유지(back-adjust 합성 X — 거짓 충실성 차단).
        "rollover_gaps": rollover_gaps,
    }


def parse_chart(name: str, label_note: str, raw: dict | None) -> dict | None:
    """선물 응답 1개 → futures dict. 실패/sanity 위반 시 None (부분 산출물 차단)."""
    if not raw:
        return None
    try:
        result = raw["chart"]["result"][0]
        meta = result["meta"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        print(f"[us-futures {name}] 응답 구조 불일치", file=sys.stderr)
        return None

    closes = quote.get("close") or []
    closes_clean = [c for c in closes if c is not None]
    if not closes_clean:
        return None

    # point = 최신가 (마지막 유효 종가). meta.regularMarketPrice 와 미세차 가능하나
    # spark 마지막과 단일소스 정합 위해 series 채택.
    point = _round2(closes_clean[-1])
    if point is None:
        return None

    # change_pct = 직전 세션 settle 대비 (1d range 의 chartPreviousClose = 정상 기준).
    # fallback = 당일 첫 봉 (settle 부재 시).
    prev = meta.get("chartPreviousClose")
    if not prev and closes_clean:
        prev = closes_clean[0]
    change_pct = None
    if prev:
        try:
            change_pct = round((float(point) - float(prev)) / float(prev) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None

    spark = [_round2(c) for c in closes_clean][-SPARK_MAX:]

    lo, hi = SANITY_RANGE.get(name, (0.0, float("inf")))
    if not (lo <= point <= hi):
        print(
            f"[us-futures {name}] sanity FAIL: point={point} (예상 {lo}~{hi})",
            file=sys.stderr,
        )
        return None

    return {
        "name": name,
        "label_note": label_note,
        "point": point,
        "change_pct": change_pct,
        "spark": spark,
    }


def collect() -> dict | None:
    """3 선물 수집 → {as_of_kst, futures:[...]} dict.

    전부/일부 실패 시 None (부분 산출물 금지 — 3종 all-present 불변식).
    build 단이 None 수신 시 직전 JSON 유지 (stale 명시).
    """
    futures: list[dict] = []
    for name, symbol, label_note in US_FUTURES_TARGETS:
        # 5분봉(spark+장중 point/change_pct) — 기존 유지.
        raw = fetch_chart(symbol)
        parsed = parse_chart(name, label_note, raw)
        if parsed is None:
            print(
                f"[us-futures] {name}({symbol}) 수집 실패 — 부분 산출물 차단, "
                f"전체 None (직전 JSON 유지)",
                file=sys.stderr,
            )
            return None
        # 일봉(candle·range_240d·daily_expanded) — 정규장 indices[] 동일 shape 부착.
        # Q-20260608-140 B안: 선물 카드 완전 페어. daily 실패도 전체 None (부분 차단).
        raw_daily = fetch_daily(symbol)
        daily = parse_daily(name, raw_daily)
        if daily is None:
            print(
                f"[us-futures] {name}({symbol}) 일봉 수집 실패 — 부분 산출물 차단, "
                f"전체 None (직전 JSON 유지)",
                file=sys.stderr,
            )
            return None
        # 정규장 indices[] 키명과 정확히 일치(candle/range_240d/daily_expanded) →
        # frontend 추가 작업 0 (같은 렌더 경로). rollover_gaps 는 표기 부가 필드.
        # daily 에 "spark" 키 없음 → parsed(장중 5분봉 spark) 보존.
        parsed.update(daily)

        # spark = 오늘 장중 path. 첫 점에 당일 시가(candle.o) prepend →
        # overall 방향(spark[0]→spark[-1]) = candle o→c = 양봉/음봉 색과 구조적 일치
        # (프론트 buildSparkline(spark, base=candle.o, candleDir) 정합). 보간 X.
        candle_o = daily["candle"].get("o")
        if candle_o is not None:
            intraday = parsed.get("spark") or []
            # 장중 5분봉 부족 시 최소 [o, c] 2점으로 방향 보장(단조 직선, 추정 보간 X).
            if not intraday:
                intraday = [parsed["point"]]
            parsed["spark"] = [candle_o, *intraday]
        futures.append(parsed)

    return {
        "as_of_kst": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        # 선물 거래 중 여부 (CME ET 윈도우). False = 직전 settle 보유(정규장 마감 fallback).
        "session_open": is_cme_futures_open(),
        "futures": futures,
    }


def main():
    result = collect()
    if result is None:
        print("[us-futures] 전체 수집 실패", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
