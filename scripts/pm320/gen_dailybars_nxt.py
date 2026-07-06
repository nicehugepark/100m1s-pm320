#!/usr/bin/env python3
"""PM320 피보나치 멀티스케일 저점 앵커용 — NXT 포함 일봉 1회 정적 산출.

2026-06-10 대표 GO (lead 1단계). NXT(넥스트레이드) 장 포함 봉 = 대표가 보는 차트 기준.
minutes_nxt.db (NXT 통합 1분봉, 커버 4/8~6/8) 를 일 OHLC 로 집계해서 KRX dailybars 와 splice:

  - date >= NXT 커버 시작: NXT 포함 일봉 (low=min(전세션 low), high=max(전세션 high),
    open=정규장 첫 시가, close=정규장 마지막 종가, v/ta=KRX 값 보존)
  - date < NXT 커버 시작 OR NXT 미커버 종목/날짜: KRX dailybars 그대로 (한계 — 3월 등 NXT 미커버)

산출물: <CRON_REPO>/data/dailybars-nxt/{code}.json (KRX dailybars 와 동일 schema).
차트(renderer.js + fibonacci.js)가 1차 fetch, 부재 시 KRX dailybars fallback.

본 스크립트는 1회성. 실행 후 산출물(JSON)만 cron repo 에 commit. 일일 자동 갱신은 2단계(별건 큐).
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

# NXT 1분봉 DB: pm320 레포 자기 데이터. env override 우선, 미설정 시 레포 로컬 data/.
# (옛 메인레포 절대경로 `100m1s/projects/pm320/data/` 제거 — 자립성 게이트 forbidden.
#  __file__ 기준 parents[2] = 레포 루트: scripts/pm320/gen_dailybars_nxt.py → 레포.)
_REPO_ROOT = Path(__file__).resolve().parents[2]
NXT_DB = Path(
    os.environ.get("M1S_MINUTES_NXT_DB") or (_REPO_ROOT / "data" / "minutes_nxt.db")
)
CRON_REPO = Path.home() / "company/100m1s-homepage-cron"
KRX_DIR = CRON_REPO / "data/dailybars"
OUT_DIR = CRON_REPO / "data/dailybars-nxt"


def nxt_daily_ohlc(con, code):
    """NXT 포함 일 OHLC 집계. {date: {o,h,l,c}}.

    low = min(전세션 low), high = max(전세션 high) — NXT 프리/애프터 포함.
    open = 정규장 첫 봉 시가, close = 정규장 마지막 봉 종가 (공식 일봉 정합).
    정규장 봉이 없는 날(extended-only)은 제외 (정상 일봉 아님).
    """
    cur = con.cursor()
    # 전세션 low/high
    lohi = {}
    for d, lo, hi in cur.execute(
        "SELECT substr(dt,1,10) d, MIN(low), MAX(high) "
        "FROM minute_bars WHERE code=? GROUP BY substr(dt,1,10)",
        (code,),
    ):
        lohi[d] = [lo, hi]
    # 정규장 open(첫)/close(마지막)
    reg_open, reg_close = {}, {}
    for d, o, c in cur.execute(
        "SELECT substr(dt,1,10) d, open, close FROM minute_bars "
        "WHERE code=? AND session='regular' ORDER BY dt",
        (code,),
    ):
        if d not in reg_open:
            reg_open[d] = o  # 첫 정규장 봉
        reg_close[d] = c  # 마지막 정규장 봉 (덮어쓰기)
    out = {}
    for d, (lo, hi) in lohi.items():
        if d not in reg_close:
            continue  # 정규장 봉 없는 날 제외
        out[d] = {"o": reg_open[d], "h": hi, "l": lo, "c": reg_close[d]}
    return out


def splice_one(con, code):
    krx_path = KRX_DIR / f"{code}.json"
    if not krx_path.exists():
        return None
    krx = json.loads(krx_path.read_text())
    rows = krx.get("rows", [])
    if not rows:
        return None
    nxt = nxt_daily_ohlc(con, code)
    covered = 0  # NXT 데이터 존재 일수 (집계 대상)
    changed = 0  # 실제 OHLC 값이 KRX 와 달라진 일수 (NXT extended 가 KRX range 초과)
    for r in rows:
        d = r.get("d")
        if d in nxt:
            n = nxt[d]
            before = (r["o"], r["h"], r["l"], r["c"])
            # NXT 포함 OHLC 로 교체 (v/ta 는 KRX 보존 — NXT 거래량 별도 미집계)
            r["o"], r["h"], r["l"], r["c"] = n["o"], n["h"], n["l"], n["c"]
            covered += 1
            if before != (r["o"], r["h"], r["l"], r["c"]):
                changed += 1
    krx["_nxt_covered_days"] = covered
    krx["_nxt_changed_days"] = changed
    krx["_nxt_source"] = "minutes_nxt.db"
    return krx, covered, changed


def main():
    if not NXT_DB.exists():
        print(f"FATAL: NXT DB 부재 {NXT_DB}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{NXT_DB}?mode=ro", uri=True)
    codes = sorted(p.stem for p in KRX_DIR.glob("*.json"))
    total, with_cover, with_change = 0, 0, 0
    for code in codes:
        res = splice_one(con, code)
        if res is None:
            continue
        payload, covered, changed = res
        (OUT_DIR / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        total += 1
        if covered > 0:
            with_cover += 1
        if changed > 0:
            with_change += 1
    con.close()
    print(
        f"산출 완료: {total} 종목 → {OUT_DIR} "
        f"(NXT 커버 {with_cover} 종목 / 실제 OHLC 변동 {with_change} 종목)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
