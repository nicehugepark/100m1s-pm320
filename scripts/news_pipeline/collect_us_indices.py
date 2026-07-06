"""미국 지수 수집 — Yahoo Finance chart endpoint (Q-20260605-103 Phase 3).

야간 미장요약(US market overnight digest)용 지수 시계열 수집기.
NASDAQ(^IXIC) / S&P500(^GSPC) / Dow(^DJI) 3종을 Yahoo chart API 로 수집해
homepage 산출물 `data/us-indices/{kstDate}.json` 으로 빌드한다.

설계 계약 (프론트 병렬 구현 중 — schema 임의 변경 금지, 필드 추가만 허용)
------------------------------------------------------------------
{
  "trade_date_local": "2026-06-04",         # 미국 거래일 (KST 아님)
  "indices": [
    {"name": "NASDAQ", "point": 26830.96, "change_pct": -0.32,
     "spark": [25800.1, ...],               # 최근 ~1개월 종가 배열 (sparkline)
     "candle": {"o": 26579.3, "h": 26923.7, "l": 26554.2, "c": 26830.96}},
    ...
  ],
  "news_chips": [...]                        # build_us_digest.py 가 채움 (본 모듈 미관여)
}

엔드포인트 (Phase 1 dev-usidx-feasibility 실 curl 22행 PASS evidence + 2026-06-05
재실측 PASS: HTTP 200, regularMarketPrice/chartPreviousClose/timestamp/quote OHLC)
----------------------------------------------------------------------
GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d
응답: chart.result[0].meta.{regularMarketPrice, chartPreviousClose}
      chart.result[0].timestamp[]  (epoch sec)
      chart.result[0].indicators.quote[0].{open,high,low,close}[]

실패 시 전일 JSON 유지 (부분 산출물 금지 — FLR-20260605-TEC-001 §1 변종 정합).
collect 는 수집만 담당, JSON 쓰기/전일 보존은 build_us_digest.py 가 판단한다
(이 모듈은 dict 반환만, side-effect 없음 — 원자적 배포 게이트 단일화).

Alpha Vantage fallback 은 키 미발급으로 stub 분기만 (실호출 0건).

사용:
  python3 -m scripts.news_pipeline.collect_us_indices          # 3 지수 수집 → stdout JSON
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# 수집 대상 (표시명, Yahoo 심볼)
US_INDEX_TARGETS = [
    ("NASDAQ", "^IXIC"),
    ("S&P500", "^GSPC"),
    ("DOW", "^DJI"),
]

YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
# Yahoo 는 빈/기본 UA 를 종종 429/403 처리 → 명시 UA 의무 (실측 confirmed).
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# spark 표본 상한 (5분봉 1일 ≈ 78~92봉 → 최근 N만 노출, 미니 스파크 과밀 방지).
# 정규장 지수 spark = 직전 미장 세션 당일 장중 5분봉 종가열(parse_intraday). collect()
# 에서 첫 점에 당일 시가(candle.o)를 prepend → 방향(시가→현재) = candle o→c 색과 항상
# 일치 (대표 결정 2026-06-08: "미장 정규장 스파크도 선물과 동일하게 당일 장중 분봉").
# 22일 추세는 daily_expanded(일봉 캔들 차트)가 담당(역할 분리). 선물 collect_us_futures.py
# 의 SPARK_MAX(40)·prepend 로직과 동형. range=1y&interval=1d 는 candle/range_240d/
# daily_expanded 산출용으로 유지 (spark override 만 장중 5분봉으로 환원).
SPARK_MAX = 40

# range_240d 계산 윈도우 (최근 N 거래일, 종목카드 240일 정책 정합).
RANGE_WINDOW = 240

# sanity: 미국 주요 지수 합리적 범위 (2026-06 현재 NASDAQ ~26800, S&P ~6700,
# Dow ~44000). 스케일/심볼 오인 즉시 탐지 (FLR-20260406-TEC-001 동형 방지).
# Yahoo 는 실지수 그대로(스케일 보정 불요) — 키움 ×100 과 다름.
SANITY_RANGE = {
    "NASDAQ": (5000.0, 100000.0),
    "S&P500": (1500.0, 30000.0),
    "DOW": (15000.0, 200000.0),
}


def is_us_regular_open(now_et: datetime | None = None) -> bool:
    """미국 정규장(NYSE/NASDAQ regular session) 거래 중 여부 — 미 동부(ET) 벽시계 판정.

    거래 구간 (Q-20260608 정규장 장중 실시간, FLR-20260408-TEC-001 외부 spec 사전 검증):
      평일 09:30~16:00 ET. 주말(토·일) 휴장. ZoneInfo("America/New_York") 의 벽시계
      hour/minute 는 EDT(서머타임)/EST 를 자동 반영하므로 별도 DST 분기 불요
      (collect_us_futures.is_cme_futures_open 검증 패턴 동형 재사용 — 추가 외부
      의존 0, lead-meta §11.15 정합).
    보수적 판정 (애매하면 False → 정규장 마감 데이터 보존, FLR-AGT-002 거짓 충실성 차단):
      - 토/일 = 휴장
      - 평일 09:30 이전 / 16:00 이후 = 휴장 (장중만 True)
    미국 공휴일 / 반일장(early close 13:00 ET)은 best-effort 미반영 — 휴장일에 True 가
    나와도 fetch_intraday 가 당일 봉 0건 → 마감 데이터 보존(fail-safe, 회귀 0).
    """
    if now_et is None:
        now_et = datetime.now(ZoneInfo("America/New_York"))
    dow = now_et.weekday()  # 0=월 ... 5=토 6=일
    if dow >= 5:  # 토·일 = 휴장
        return False
    minutes = now_et.hour * 60 + now_et.minute
    # 09:30 = 570분, 16:00 = 960분. [09:30, 16:00) 구간만 거래 중.
    return 570 <= minutes < 960


def _round2(v) -> float | None:
    """소수 2자리 반올림 (None 통과)."""
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def fetch_chart(symbol: str, max_retries: int = 3) -> dict | None:
    """Yahoo chart API 1개 심볼 호출. 재시도 포함. 실패 시 None.

    range=1mo&interval=1d → 최근 ~21 거래일 OHLC + meta.
    """
    url = f"{YAHOO_CHART_BASE}/{symbol}"
    # range=1y → 240거래일 OHLC (range_240d 범위바용). 동일 1콜 (대표 2026-06-05 B안).
    params = {"range": "1y", "interval": "1d"}
    headers = {"User-Agent": _UA}
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            print(f"[us-indices {symbol}] exception: {exc}", file=sys.stderr)
            time.sleep(2**attempt)
            continue
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"[us-indices {symbol}] 429, backoff {back}s", file=sys.stderr)
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(
                f"[us-indices {symbol}] http {r.status_code}: {r.text[:200]}",
                file=sys.stderr,
            )
            time.sleep(2**attempt)
            continue
        try:
            return r.json()
        except ValueError:
            print(f"[us-indices {symbol}] JSON parse fail", file=sys.stderr)
            return None
    return None


def fetch_intraday(symbol: str, max_retries: int = 3) -> dict | None:
    """Yahoo chart 5분봉 호출 (range=1d&interval=5m). 실패 시 None.

    정규장 지수 spark = 직전 미장 세션 당일 장중 path 용 (선물 collect_us_futures.py
    fetch_chart 와 동형 — params 만 5분봉). KR 낮 시간(미장 마감 후) 호출 시 직전
    세션의 장중 5분봉 시계열을 반환. candle/range_240d/daily_expanded 용 1y 일봉은
    fetch_chart 가 별도 1콜로 담당(역할 분리 — 추가 1콜만 증가).
    """
    url = f"{YAHOO_CHART_BASE}/{symbol}"
    params = {"range": "1d", "interval": "5m"}
    headers = {"User-Agent": _UA}
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            print(f"[us-indices-intraday {symbol}] exception: {exc}", file=sys.stderr)
            time.sleep(2**attempt)
            continue
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(
                f"[us-indices-intraday {symbol}] 429, backoff {back}s", file=sys.stderr
            )
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(
                f"[us-indices-intraday {symbol}] http {r.status_code}: {r.text[:200]}",
                file=sys.stderr,
            )
            time.sleep(2**attempt)
            continue
        try:
            return r.json()
        except ValueError:
            print(f"[us-indices-intraday {symbol}] JSON parse fail", file=sys.stderr)
            return None
    return None


def parse_intraday(name: str, raw: dict | None) -> list | None:
    """5분봉 응답 → 장중 종가열 (None 제외). 실패/빈 데이터 시 None.

    선물 parse_chart 의 spark 산출(closes_clean[-SPARK_MAX:]) 과 동형. 첫 점 candle.o
    prepend 는 collect() 에서 수행(일봉 candle 과 단일 소스 정합). 반환 = prepend 전
    장중 5분봉 종가 배열(최근 SPARK_MAX). sanity 는 collect() 의 일봉 point 가 담당.
    """
    if not raw:
        return None
    try:
        result = raw["chart"]["result"][0]
        result["meta"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        print(f"[us-indices-intraday {name}] 응답 구조 불일치", file=sys.stderr)
        return None

    closes = quote.get("close") or []
    closes_clean = [c for c in closes if c is not None]
    if not closes_clean:
        return None
    return [_round2(c) for c in closes_clean][-SPARK_MAX:]


def parse_intraday_quote(name: str, raw: dict | None) -> dict | None:
    """5분봉 응답 → 당일 정규장 장중 quote dict. 실패/빈 데이터 시 None.

    정규장 장중 실시간 갱신용 (Q-20260608, refresh_intraday 가 개장 시 사용). 추가
    API 콜 0 — spark 용으로 이미 받는 fetch_intraday(1d/5m) raw 를 재활용해 당일
    point/change_pct/candle/trade_date_local 을 산출한다(대표 결정 최소비용 경로).

    반환 {point, change_pct, candle:{o,h,l,c}, trade_date_local}:
      - point         = 당일 마지막 유효 5분봉 close (현재가)
      - change_pct     = meta.chartPreviousClose(전일 종가) 대비 등락률
      - candle.o       = 당일 첫 유효 open (시가)
      - candle.h/l     = 당일 장중 high/low (5분봉 전체 max/min)
      - candle.c       = point
      - trade_date_local = 마지막 봉 timestamp 의 미 동부 거래일
    sanity(SANITY_RANGE) 위반 시 None — 스케일/심볼 오인 차단(parse_chart 동형).
    """
    if not raw:
        return None
    try:
        result = raw["chart"]["result"][0]
        meta = result["meta"]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        print(f"[us-indices-intraday-quote {name}] 응답 구조 불일치", file=sys.stderr)
        return None

    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []

    # 마지막 유효 close = 현재가.
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

    # sanity: 스케일/심볼 오인 차단.
    lo, hi = SANITY_RANGE.get(name, (0.0, float("inf")))
    if not (lo <= point <= hi):
        print(
            f"[us-indices-intraday-quote {name}] sanity FAIL: point={point}",
            file=sys.stderr,
        )
        return None

    # change_pct = 전일 종가(chartPreviousClose) 대비. 일봉 parse_chart 가 쓰는
    # "series 마지막 두 종가"는 장중 5분봉엔 부적합 → meta 전일 종가 사용.
    change_pct = None
    prev_close = meta.get("chartPreviousClose")
    if prev_close:
        try:
            change_pct = round((point - float(prev_close)) / float(prev_close) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None

    # candle: 당일 시가(첫 유효 open) + 장중 high/low + 현재가.
    open_v = next((o for o in opens if o is not None), None)
    highs_clean = [h for h in highs if h is not None]
    lows_clean = [low for low in lows if low is not None]
    candle = {
        "o": _round2(open_v),
        "h": _round2(max(highs_clean)) if highs_clean else None,
        "l": _round2(min(lows_clean)) if lows_clean else None,
        "c": point,
    }

    trade_date_local = None
    if last_idx < len(timestamps):
        trade_date_local = _ts_to_date(timestamps[last_idx])

    return {
        "point": point,
        "change_pct": change_pct,
        "candle": candle,
        "trade_date_local": trade_date_local,
    }


def parse_chart(name: str, raw: dict | None) -> dict | None:
    """Yahoo chart 응답 1개 → index dict (계약 schema).

    실패(데이터 부재/구조 불일치/sanity 위반) 시 None — 부분 산출물 차단.
    spark 는 본 함수가 산출하지 않음 (장중 5분봉 parse_intraday 로 환원, collect()
    가 candle.o prepend 후 부착). candle/range_240d/daily_expanded 는 1y 일봉 유지.
    """
    if not raw:
        return None
    try:
        result = raw["chart"]["result"][0]
        result["meta"]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        print(f"[us-indices {name}] 응답 구조 불일치", file=sys.stderr)
        return None

    if not timestamps:
        return None

    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []

    # spark 는 여기서 산출하지 않음 (장중 5분봉 parse_intraday → collect() 에서
    # candle.o prepend 후 부착). 여기 1y 일봉은 candle/range_240d/daily_expanded 용.
    closes_clean = [c for c in closes if c is not None]
    if not closes_clean:
        return None

    # 최근 거래일 캔들: close 가 채워진 마지막 인덱스 기준 (장중 None 후행 방어).
    last_idx = None
    for i in range(len(closes) - 1, -1, -1):
        if closes[i] is not None:
            last_idx = i
            break
    if last_idx is None:
        return None

    # point = 최근 종가 (series 마지막 close). meta.regularMarketPrice 와 미세 차이가
    # 있으나 candle.c 와 point 정합을 위해 series close 단일 소스로 통일.
    point = _round2(closes[last_idx])
    if point is None:
        return None

    # change_pct = 전일 대비 (series 마지막 두 종가). meta.chartPreviousClose 는
    # range=1mo 일 때 "윈도 시작 직전 종가"(한 달 전)라 전일 대비 아님 — 사용 금지.
    # (2026-06-05 self-catch: meta.chartPreviousClose 사용 시 +7% 오산 발견).
    change_pct = None
    if len(closes_clean) >= 2 and closes_clean[-2]:
        try:
            prev = float(closes_clean[-2])
            change_pct = round((float(point) - prev) / prev * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None

    candle = {
        "o": _round2(opens[last_idx]) if last_idx < len(opens) else None,
        "h": _round2(highs[last_idx]) if last_idx < len(highs) else None,
        "l": _round2(lows[last_idx]) if last_idx < len(lows) else None,
        "c": _round2(closes[last_idx]),
    }

    # 미국 거래일 = 마지막 캔들 timestamp (UTC epoch) 의 미국 동부 날짜.
    # KST 아님 (혼선 금지 — 계약 §trade_date_local).
    trade_date_local = None
    if last_idx < len(timestamps):
        try:
            dt_et = datetime.fromtimestamp(
                timestamps[last_idx], ZoneInfo("America/New_York")
            )
            trade_date_local = dt_et.strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            trade_date_local = None

    # sanity: point 가 합리적 범위 밖이면 스케일/심볼 오인 → 차단.
    lo, hi = SANITY_RANGE.get(name, (0.0, float("inf")))
    if not (lo <= point <= hi):
        print(
            f"[us-indices {name}] sanity FAIL: point={point} (예상 {lo}~{hi})",
            file=sys.stderr,
        )
        return None

    # range_240d: 최근 RANGE_WINDOW 거래일 종가 최저/최고 + 날짜 + 현재가 대비 등락률.
    # 종목카드 범위 바와 동일 의미: low_change_pct = 현재가 최저 대비 상승률(+),
    # high_change_pct = 현재가 최고 대비 등락률(최고 미만이면 -). close 가 None 인
    # 인덱스는 제외하되 timestamp 정렬 유지 (low_date/high_date 정합).
    range_240d = _calc_range_240d(name, point, closes, timestamps)

    # daily_expanded: 1y 일봉 OHLCV 전체 직렬화 (확대 차트 ChartTV 용, 대표 2026-06-05
    # 20:55). 이미 받은 range=1y series 그대로. close 가 None 인 봉(미체결)은 제외.
    # v(거래량)는 야후 응답에 있으면 포함, 없으면 키 생략 (필드 optional).
    volumes = quote.get("volume") or []
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

    return {
        "name": name,
        "point": point,
        "change_pct": change_pct,
        # spark 미포함 — collect() 가 장중 5분봉(parse_intraday) + candle.o prepend 부착.
        "candle": candle,
        "range_240d": range_240d,
        "daily_expanded": daily_expanded,
        "_trade_date_local": trade_date_local,  # build 단에서 trade_date_local 결정에 사용
    }


def _ts_to_date(ts) -> str | None:
    """epoch(UTC) → 미국 동부 거래일 (YYYY-MM-DD)."""
    try:
        return datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).strftime(
            "%Y-%m-%d"
        )
    except (OSError, OverflowError, ValueError, TypeError):
        return None


def _calc_range_240d(
    name: str, point: float, closes: list, timestamps: list
) -> dict | None:
    """최근 RANGE_WINDOW 거래일 종가 기준 range_240d dict 계산.

    반환: {low, high, low_date, high_date, low_change_pct, high_change_pct}.
    데이터 부족(유효 종가 < 2) 시 None. min<max + 현재가 범위 sanity 는 호출측 검증.
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


def collect() -> dict | None:
    """3 지수 수집 → {trade_date_local, indices:[...]} dict.

    전부 실패 시 None (build 단이 전일 JSON 유지 판단). 일부만 성공해도 None
    (부분 산출물 금지 — 3 지수 all-present 불변식, FLR-20260605-TEC-001 §1).
    """
    indices: list[dict] = []
    for name, symbol in US_INDEX_TARGETS:
        raw = fetch_chart(symbol)
        parsed = parse_chart(name, raw)
        if parsed is None:
            print(
                f"[us-indices] {name}({symbol}) 수집 실패 — 부분 산출물 차단, "
                f"전체 None 반환 (전일 JSON 유지)",
                file=sys.stderr,
            )
            return None

        # spark = 직전 미장 세션 당일 장중 5분봉 종가열 + candle.o prepend (선물 동형).
        # overall 방향(spark[0]→spark[-1]) = candle o→c = 양봉/음봉 색과 구조적 일치.
        raw_intra = fetch_intraday(symbol)
        intraday = parse_intraday(name, raw_intra)
        if intraday is None:
            print(
                f"[us-indices] {name}({symbol}) 장중 5분봉 수집 실패 — 부분 산출물 "
                f"차단, 전체 None 반환 (전일 JSON 유지)",
                file=sys.stderr,
            )
            return None
        candle_o = parsed["candle"].get("o")
        if candle_o is not None:
            parsed["spark"] = [candle_o, *intraday]
        else:
            parsed["spark"] = intraday

        indices.append(parsed)

    # trade_date_local: 3 지수 동일 미국 거래일이어야 정상. 최빈값(가장 최근) 채택.
    dates = [i.pop("_trade_date_local", None) for i in indices]
    trade_date_local = next((d for d in dates if d), None)
    if not trade_date_local:
        return None

    return {"trade_date_local": trade_date_local, "indices": indices}


def collect_alpha_vantage_stub() -> dict | None:
    """Alpha Vantage fallback — 키 미발급 stub. 항상 None.

    키 발급 시 GLOBAL_QUOTE / TIME_SERIES_DAILY 분기 구현 예정.
    """
    print("[us-indices] Alpha Vantage fallback: 키 미발급 — stub None", file=sys.stderr)
    return None


def main():
    result = collect()
    if result is None:
        # fallback 시도 (현재 stub)
        result = collect_alpha_vantage_stub()
    if result is None:
        print("[us-indices] 전체 수집 실패", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
