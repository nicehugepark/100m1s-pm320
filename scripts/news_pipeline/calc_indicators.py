"""
기술지표 계산 모듈 — daily_picks OHLC 데이터 기반.
MA(5/10/20/60/120/240), RSI(14), MACD(12/26/9) 산출 → technical_indicators 저장.
장 마감 후 1회 실행. pipeline.sh에서 build_daily 이후 호출.
"""

from __future__ import annotations

from datetime import datetime

from .db import connect

MA_PERIODS = (5, 10, 20, 60, 120, 240)
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


def _fetch_price_history(conn, stock_code: str, limit: int = 300):
    """과거 종가를 오래된 순으로 반환. limit=300이면 MA240 + 여유분."""
    rows = conn.execute(
        """SELECT date, price FROM daily_picks
           WHERE stock_code=? AND price IS NOT NULL
           ORDER BY date ASC
           LIMIT ?""",
        (stock_code, limit),
    ).fetchall()
    return [(r["date"], float(r["price"])) for r in rows]


def _calc_sma(prices: list[float], period: int) -> float | None:
    """단순이동평균. 데이터 부족 시 None."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def _calc_rsi(prices: list[float], period: int = 14) -> float | None:
    """Wilder RSI. 최소 period+1개 데이터 필요."""
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(values: list[float], period: int) -> list[float]:
    """EMA 계산. 첫 period개는 SMA로 시드."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _calc_macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float | None, float | None, float | None]:
    """MACD line, signal, histogram. 데이터 부족 시 (None, None, None)."""
    if len(prices) < slow + signal:
        return None, None, None
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    # ema_fast는 prices[fast-1:]부터, ema_slow는 prices[slow-1:]부터
    # 정렬: 둘 다 같은 시점의 값끼리 빼야 함
    offset = slow - fast  # ema_fast에서 이만큼 건너뛴 지점이 ema_slow[0]과 동일 시점
    macd_line = [ema_fast[offset + i] - ema_slow[i] for i in range(len(ema_slow))]
    if len(macd_line) < signal:
        return None, None, None
    signal_line = _ema(macd_line, signal)
    if not signal_line:
        return None, None, None
    macd_val = macd_line[-1]
    sig_val = signal_line[-1]
    hist_val = macd_val - sig_val
    return macd_val, sig_val, hist_val


def calc_for_date(target_date: str = None):
    """target_date에 daily_picks가 있는 모든 종목의 기술지표 계산."""
    from .config import pipeline_date

    date = target_date or pipeline_date()
    now = datetime.now().isoformat()
    updated = 0

    with connect() as conn:
        # technical_indicators 테이블 보장
        conn.executescript(
            """CREATE TABLE IF NOT EXISTS technical_indicators (
                date TEXT NOT NULL, stock_code TEXT NOT NULL,
                ma5 REAL, ma10 REAL, ma20 REAL, ma60 REAL, ma120 REAL, ma240 REAL,
                rsi14 REAL, macd REAL, macd_signal REAL, macd_hist REAL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (date, stock_code));
            CREATE INDEX IF NOT EXISTS idx_ti_code
                ON technical_indicators(stock_code, date DESC);"""
        )

        codes = conn.execute(
            "SELECT DISTINCT stock_code FROM daily_picks WHERE date=?", (date,)
        ).fetchall()

        for row in codes:
            code = row["stock_code"]
            history = _fetch_price_history(conn, code, 300)
            if not history:
                continue
            prices = [p for _, p in history]

            mas = {}
            for period in MA_PERIODS:
                mas[period] = _calc_sma(prices, period)

            rsi = _calc_rsi(prices, RSI_PERIOD)
            macd_val, sig_val, hist_val = _calc_macd(
                prices, MACD_FAST, MACD_SLOW, MACD_SIGNAL
            )

            conn.execute(
                """INSERT INTO technical_indicators
                     (date, stock_code, ma5, ma10, ma20, ma60, ma120, ma240,
                      rsi14, macd, macd_signal, macd_hist, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(date, stock_code) DO UPDATE SET
                     ma5=excluded.ma5, ma10=excluded.ma10, ma20=excluded.ma20,
                     ma60=excluded.ma60, ma120=excluded.ma120, ma240=excluded.ma240,
                     rsi14=excluded.rsi14, macd=excluded.macd,
                     macd_signal=excluded.macd_signal, macd_hist=excluded.macd_hist,
                     created_at=excluded.created_at""",
                (
                    date,
                    code,
                    mas[5],
                    mas[10],
                    mas[20],
                    mas[60],
                    mas[120],
                    mas[240],
                    rsi,
                    macd_val,
                    sig_val,
                    hist_val,
                    now,
                ),
            )
            updated += 1

        conn.commit()
    print(f"technical_indicators: {updated} stocks updated for {date}")
    return updated


if __name__ == "__main__":
    calc_for_date()
