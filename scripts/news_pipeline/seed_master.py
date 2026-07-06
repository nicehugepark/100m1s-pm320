"""
종목 마스터 DB 시드
소스: KIND 상장법인목록 (https://kind.krx.co.kr/corpgeneral/corpList.do)
     → EUC-KR HTML 테이블, 회사명/시장구분/종목코드/업종/주요제품 포함
pykrx는 2026-04 시점 KRX 차단으로 동작 불가 → KIND 직접 사용으로 전환

CLI:
  python -m scripts.news_pipeline.seed_master                # 전체 KIND fetch + UPSERT
  python -m scripts.news_pipeline.seed_master --codes 439960,123456
                                                             # 특정 코드만 시드 (신규 daily_picks 진입 종목 catch-up)
"""

from __future__ import annotations

import argparse
from datetime import datetime
from io import BytesIO
from urllib.request import Request, urlopen

import pandas as pd

from .db import connect, init_schema

KIND_URL = (
    "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
)


def fetch_kind():
    req = Request(KIND_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as resp:
        raw = resp.read()
    df = pd.read_html(BytesIO(raw), encoding="euc-kr")[0]
    # 종목코드 6자리 zero-pad
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    return df


def _upsert_rows(df, codes_filter: set[str] | None = None) -> int:
    """KIND DataFrame → stocks UPSERT. codes_filter 주어지면 해당 코드만."""
    now = datetime.now().isoformat()
    inserted = 0
    with connect() as conn:
        for _, r in df.iterrows():
            code = r["종목코드"]
            if codes_filter is not None and code not in codes_filter:
                continue
            name = r["회사명"]
            market_raw = str(r.get("시장구분", ""))
            market = (
                "KOSPI"
                if "유가" in market_raw
                else (
                    "KOSDAQ"
                    if "코스닥" in market_raw
                    else ("KONEX" if "코넥스" in market_raw else market_raw)
                )
            )
            industry = r.get("업종") if pd.notna(r.get("업종")) else None
            sector = r.get("주요제품") if pd.notna(r.get("주요제품")) else None
            conn.execute(
                """INSERT INTO stocks(code, name, market, industry, sector, last_updated)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name,
                     market=excluded.market,
                     industry=excluded.industry,
                     sector=excluded.sector,
                     last_updated=excluded.last_updated""",
                (code, name, market, industry, sector, now),
            )
            inserted += 1
        conn.commit()
    return inserted


def seed():
    init_schema()
    df = fetch_kind()
    n = _upsert_rows(df)
    print(f"seeded {n} stocks from KIND")


def seed_codes(codes: list[str]) -> int:
    """주어진 종목 코드만 KIND에서 lookup + stocks UPSERT.

    신규 daily_picks 진입 종목 catch-up 용도 (P0 본질 fix 2026-05-11).
    KIND는 전체 fetch만 지원하므로 응답 후 코드 필터링으로 한정 UPSERT.
    """
    if not codes:
        print("[seed_codes] 대상 코드 없음")
        return 0
    init_schema()
    df = fetch_kind()
    n = _upsert_rows(df, codes_filter=set(codes))
    print(f"[seed_codes] requested={len(codes)} seeded={n} from KIND")
    if n < len(codes):
        missing = set(codes) - set(df["종목코드"].tolist())
        if missing:
            print(f"[seed_codes] KIND 미발견 (ETF/스팩/구코드 의심): {sorted(missing)}")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--codes",
        default=None,
        help="comma-separated codes (e.g. 439960,123456) — 전체 KIND fetch 후 해당 코드만 UPSERT",
    )
    args = ap.parse_args()
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        seed_codes(codes)
    else:
        seed()
