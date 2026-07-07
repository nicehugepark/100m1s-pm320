"""
키움 'v1' 조건검색 스크레이퍼 (D축 cutover 2026-06-17, 종전 '500억이상')

매 10분 (KST 08:00~22:00, 평일) 키움 조건검색('v1')을 호출하여
거래대금 500억+ ∪ 당일 상한가 종목 목록을 수집·저장.
(상한가 SoT = v1 결과목록 등락률 >= 29.79%, build_daily.LIMIT_UP_THRESHOLD)

스케줄 (2026-06-17 확대):
  - GitHub Actions(.github/workflows/kiwoom-scrape.yml): KST 08:00~22:00 / 10분 / 평일.
  - 로컬 launchd(com.100m1s.kiwoom-scraper → kiwoom_cron.sh): 10분 fire 하나, 스크립트
    내부 장중 가드(09:00~15:40 KST, 2026-05-30 종가 가드) 가 그 밖 시각을 SKIP →
    로컬 경로 실수집 윈도우는 09:00~15:40 로 유지 (가드 미변경).

저장 구조:
  data/kiwoom/<YYYY-MM-DD>.json  — 그날 누적 + latest snapshot
  data/kiwoom/latest.json         — 가장 최근 스냅샷 (페이지 로딩용)
  data/kiwoom/index.json          — 보유 날짜 인덱스
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "kiwoom"
SCRIPT_DIR = Path(__file__).parent


def log(msg: str) -> None:
    print(f"[{datetime.now(KST).isoformat(timespec='seconds')}] {msg}", flush=True)


def parse_int(val) -> int:
    if val is None or val == "":
        return 0
    s = str(val).replace("+", "").replace(",", "").strip()
    if s.startswith("-"):
        try:
            return int(s)
        except ValueError:
            return 0
    s = s.lstrip("0") or "0"
    try:
        return int(s)
    except ValueError:
        return 0


def parse_float(val) -> float:
    if val is None or val == "":
        return 0.0
    s = str(val).replace("+", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def calc_ma(closes: list, period: int) -> float:
    """단순이동평균 계산. closes는 최근→과거 순서."""
    if len(closes) < period:
        return 0.0
    return sum(closes[:period]) / period


def calc_rsi(closes: list, period: int = 14) -> float:
    """RSI 계산. closes는 최근→과거 순서."""
    if len(closes) < period + 1:
        return 0.0
    gains, losses = [], []
    for i in range(period):
        diff = closes[i] - closes[i + 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(closes: list) -> dict:
    """MACD(12,26,9) 계산. closes는 최근→과거 순서."""
    if len(closes) < 26:
        return {}

    def ema(data, period):
        k = 2 / (period + 1)
        vals = list(reversed(data[: period * 2]))  # 과거→최근 순서로
        result = sum(vals[:period]) / period
        for v in vals[period:]:
            result = v * k + result * (1 - k)
        return result

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_val = ema12 - ema26
    return {"macd": round(macd_val, 2)}


def fetch_indicators(client, ticker: str) -> dict:
    """키움 일봉 조회 → MA/RSI/MACD 계산."""
    chart = client.get_daily_chart(ticker, count=250)
    if not chart:
        return {}
    # chart: [{"close":..., "open":..., ...}, ...] 최근→과거
    closes = []
    for c in chart:
        close = parse_int(c.get("close") or c.get("4") or c.get("stck_clpr", ""))
        if close > 0:
            closes.append(close)
    if len(closes) < 5:
        return {}
    result = {
        "ma5": round(calc_ma(closes, 5)),
        "ma10": round(calc_ma(closes, 10)),
        "ma20": round(calc_ma(closes, 20)),
        "ma60": round(calc_ma(closes, 60)),
        "ma120": round(calc_ma(closes, 120)),
        "ma240": round(calc_ma(closes, 240)),
        "rsi": round(calc_rsi(closes, 14), 2),
    }
    macd = calc_macd(closes)
    if macd:
        result.update(macd)
    return result


def parse_kiwoom_stock(s: dict, ka10081_trade_amount: int | None = None) -> dict:
    """키움 조건검색 응답 1건 → 표준 dict.

    키움 필드 코드 (조건검색 ka10172):
      9001: 종목코드 (A 접두)    302: 종목명
      10: 현재가  11: 전일대비  12: 등락률 (×1000)
      13: 거래량  14: 거래대금 (영구 부재 — 빈 문자열, Q-CYCLE11-004 verbatim 검증)
      16: 시가    17: 고가     18: 저가
      311: MACD  312: MACD시그널  313: MACD오실레이터  314: RSI

    trade_amount source 정합화 (Q-20260519-CYCLE11-004, 2026-05-19):
      ka10172 field 14 (거래대금)는 응답에 영구 부재 (빈 문자열 ''). 본 함수는
      ka10081 추가 호출 결과(ka10081_trade_amount)를 1순위 source로 사용.
      ka10081 trde_prica = KRX 정식 누적 거래대금 (원본). price × volume 단순곱
      fallback은 평균체결가 ≠ cur_prc 시 부정확 (5/19 영웅문 catch 26억 차이).

      Source 우선순위:
        1. ka10081 trde_prica × 1_000_000 (정확) — trade_amount_source='ka10081'
        2. ka10172 field 14 (현재 영구 부재) — trade_amount_source='ka10172' (사실상 미적용)
        3. price × volume fallback (부정확) — trade_amount_source='calc_fallback'
           (rules/data-continuity.md § 조건검색 trade_amount 정합화 위반 시 critical FLR)
    """
    code = str(s.get("9001", "")).lstrip("A")
    price = parse_int(s.get("10", ""))
    volume = parse_int(s.get("13", ""))
    # Q-CYCLE11-004 fix: ka10081 trde_prica 1순위 → ka10172 field 14 2순위 → calc fallback
    raw_amount_ka10172 = (
        parse_int(s.get("14", "")) * 1_000_000
    )  # 백만원 → 원 (영구 부재)
    if ka10081_trade_amount is not None and ka10081_trade_amount > 0:
        trade_amount = ka10081_trade_amount
        trade_amount_source = "ka10081"
    elif raw_amount_ka10172 > 0:
        trade_amount = raw_amount_ka10172
        trade_amount_source = "ka10172"
    else:
        trade_amount = price * volume  # 부정확 fallback (audit 추적용)
        trade_amount_source = "calc_fallback"
    result = {
        "ticker": code,
        "name": str(s.get("302", "")).strip(),
        "price": price,
        "open": parse_int(s.get("16", "")),
        "high": parse_int(s.get("17", "")),
        "low": parse_int(s.get("18", "")),
        "change": parse_int(s.get("11", "")),
        "change_pct": parse_float(s.get("12", ""))
        / 1000.0,  # FLR-20260408 등락률 스케일
        "volume": volume,
        "trade_amount": trade_amount,
        "trade_amount_source": trade_amount_source,
    }
    # 기술적 지표 — 조건검색 응답에 있으면 저장 (없으면 0)
    macd = parse_float(s.get("311", ""))
    macd_signal = parse_float(s.get("312", ""))
    macd_osc = parse_float(s.get("313", ""))
    rsi = parse_float(s.get("314", ""))
    if macd or macd_signal or rsi:
        result["macd"] = macd
        result["macd_signal"] = macd_signal
        result["macd_osc"] = macd_osc
        result["rsi"] = rsi
    return result


def merge_into_daily(daily: dict, snapshot: dict) -> None:
    """누적 종목 사전 갱신 (그날 한 번이라도 등장한 종목들)"""
    accum = daily.setdefault("accumulated_stocks", {})
    snap_time = snapshot["fetched_at"][11:16]  # "HH:MM"
    for st in snapshot["stocks"]:
        ticker = st["ticker"]
        if not ticker:
            continue
        if ticker in accum:
            ex = accum[ticker]
            ex["max_trade_amount"] = max(ex["max_trade_amount"], st["trade_amount"])
            ex["max_change_pct"] = max(ex["max_change_pct"], st["change_pct"])
            ex["min_change_pct"] = min(ex["min_change_pct"], st["change_pct"])
            ex["appearances"] = ex.get("appearances", 0) + 1
            ex["last_seen"] = snap_time
            ex["last_price"] = st["price"]
            # OHLC 갱신: high/low는 하루 중 최대/최소
            if st.get("high"):
                ex["high"] = max(ex.get("high", 0), st["high"])
            if st.get("low") and st["low"] > 0:
                ex["low"] = min(ex.get("low", st["low"]), st["low"])
            if st.get("open"):
                ex["open"] = st["open"]
        else:
            accum[ticker] = {
                "ticker": ticker,
                "name": st["name"],
                "max_trade_amount": st["trade_amount"],
                "max_change_pct": st["change_pct"],
                "min_change_pct": st["change_pct"],
                "first_seen": snap_time,
                "last_seen": snap_time,
                "appearances": 1,
                "last_price": st["price"],
                "open": st.get("open", 0),
                "high": st.get("high", 0),
                "low": st.get("low", 0),
            }


def run() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(SCRIPT_DIR))

    appkey = os.environ.get("KIWOOM_APPKEY")
    secret = os.environ.get("KIWOOM_SECRETKEY")
    if not appkey or not secret:
        log("❌ KIWOOM_APPKEY/KIWOOM_SECRETKEY 환경변수 없음")
        return 2

    try:
        from kiwoom_client import KiwoomClient
    except ImportError as e:
        log(f"❌ kiwoom_client 임포트 실패: {e}")
        return 3

    client = KiwoomClient()

    log("토큰 발급…")
    client.get_token()
    if not client.token:
        log("❌ 토큰 발급 실패")
        return 4

    try:
        log("조건검색 목록 조회…")
        conditions = client.condition_list()
        log(f"등록된 조건식 {len(conditions)}개")

        # D축 cutover (2026-06-17): '500억이상' → 'v1' 조건식 전환.
        # v1 = 거래대금 500억 ∪ 당일 상한가 를 한 번에 반환 (상한가 SoT가
        # v1 결과목록 등락률 >= 29.79% 로 통일됨, build_daily.LIMIT_UP_THRESHOLD).
        # CNSRLST 는 flat [seq, name] 리스트만 반환 (폴더 계층 없음) → 이름 매칭.
        # 정확 일치('v1') 우선 + 폴백 부분 매칭. 짧은 토큰 'v1' 의 우연 포함
        # 오탐 회피 (FLR-20260409-TEC-001 사명 부분문자열 오탐 교훈).
        target_name_query = "v1"
        target_seq = None
        target_name = None
        for seq, name in conditions:
            if str(name).strip() == target_name_query:
                target_seq = seq
                target_name = name
                break
        if not target_seq:
            for seq, name in conditions:
                if target_name_query in str(name):
                    target_seq = seq
                    target_name = name
                    break

        if not target_seq:
            available = [n for _, n in conditions]
            log(f"❌ '{target_name_query}' 조건검색 미등록 (등록된: {available})")
            return 5

        log(f"조건검색 실행: [{target_seq}] {target_name}")
        raw_stocks = client.condition_search(target_seq)
        log(f"종목 {len(raw_stocks)}개 수신")

        if not raw_stocks:
            log("⚠️ 결과 0건 (장 시간외 또는 조건 불충족)")
            return 0

        # Q-CYCLE11-004: 종목별 ka10081 추가 호출 → 정확 trade_amount (영웅문 정합)
        # rate limit 안전 마진: 0.4s sleep × ~13종목 = ~5s 추가 latency (cron 10분 주기 대비 무시)
        # client.get_today_trade_amount 내부 429 백오프 retry (1s,2s,4s,8s) 포함
        import time as _t

        ka10081_amounts: dict[str, int] = {}
        log(f"ka10081 trade_amount 추가 호출 시작 ({len(raw_stocks)}종목)…")
        for s in raw_stocks:
            code = str(s.get("9001", "")).lstrip("A")
            if not code:
                continue
            ta = client.get_today_trade_amount(code)
            if ta is not None:
                ka10081_amounts[code] = ta
            _t.sleep(0.4)
        log(f"ka10081 trade_amount {len(ka10081_amounts)}/{len(raw_stocks)} 수신")

        stocks = [
            parse_kiwoom_stock(
                s,
                ka10081_trade_amount=ka10081_amounts.get(
                    str(s.get("9001", "")).lstrip("A")
                ),
            )
            for s in raw_stocks
        ]
        # 거래대금 desc 정렬
        stocks = [s for s in stocks if s["ticker"]]
        stocks.sort(key=lambda x: x["trade_amount"], reverse=True)
        for i, s in enumerate(stocks):
            s["rank"] = i + 1

        now = datetime.now(KST)
        today = now.strftime("%Y-%m-%d")
        snap_iso = now.isoformat(timespec="seconds")

        # MA/기술지표 계산 — 장 마감 후 1회만 (15:30 이후 첫 실행)
        ma_flag = DATA_DIR / f".ma-done-{today}"
        if (
            now.hour > 15 or (now.hour == 15 and now.minute >= 30)
        ) and not ma_flag.exists():
            log("MA/기술지표 계산 시작 (상위 30종목)…")
            for s in stocks[:30]:
                try:
                    indicators = fetch_indicators(client, s["ticker"])
                    if indicators:
                        s.update(indicators)
                except Exception as e:
                    log(f"  {s['ticker']} 지표 실패: {e}")
            ma_flag.write_text(snap_iso)
            log("MA/기술지표 완료")

        snapshot = {
            "fetched_at": snap_iso,
            "stocks": stocks,
        }

        # 일별 파일 갱신
        daily_path = DATA_DIR / f"{today}.json"
        if daily_path.exists():
            try:
                daily = json.loads(daily_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                daily = {}
        else:
            daily = {}

        if not daily:
            daily = {
                "date": today,
                "condition_name": target_name,
                "first_snapshot_at": snap_iso,
                "snapshot_count": 0,
                "accumulated_stocks": {},
            }

        daily["last_snapshot_at"] = snap_iso
        daily["snapshot_count"] = daily.get("snapshot_count", 0) + 1
        daily["latest_stocks"] = stocks[:30]

        merge_into_daily(daily, snapshot)

        # 누적 종목을 max_trade_amount desc로 정렬한 daily_top 도출
        accum_list = list(daily["accumulated_stocks"].values())
        accum_list.sort(key=lambda x: x["max_trade_amount"], reverse=True)
        daily["daily_top"] = accum_list[:50]

        daily_path.write_text(
            json.dumps(daily, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # latest.json (페이지 빠른 로딩용)
        latest = {
            "date": today,
            "fetched_at": snap_iso,
            "snapshot_count": daily["snapshot_count"],
            "stocks": stocks[:30],
        }
        (DATA_DIR / "latest.json").write_text(
            json.dumps(latest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # index.json
        idx_path = DATA_DIR / "index.json"
        if idx_path.exists():
            try:
                idx = json.loads(idx_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                idx = {}
        else:
            idx = {}
        idx.setdefault("dates", [])
        if today not in idx["dates"]:
            idx["dates"].insert(0, today)
            idx["dates"] = idx["dates"][:90]  # 최근 90일
        idx["updated_at"] = snap_iso
        idx_path.write_text(
            json.dumps(idx, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        log(f"✓ {today} 스냅샷 #{daily['snapshot_count']} 저장 ({len(stocks)} 종목)")

    finally:
        try:
            client.revoke_token()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(run())
