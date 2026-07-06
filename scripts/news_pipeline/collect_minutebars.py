"""키움 ka10080 1분봉 차트 수집 — PM320 백테스트 MDD 분단위 정밀화용 백필.

요청 시각: 2026-05-24 (일) / 타치코마 → 개발팀 (대표 "확인하고 진행" 지시)

목적
----
PM320 3일·3.2% 백테스트의 MDD(최대낙폭)를 일봉 대신 1분봉 기준으로 재계산하기 위한
1분봉 원천 데이터 백필. raw 틱 대신 1분봉(ka10080) 채택 — 키움 history 약 160일 +
MDD 정밀도로 충분.

🔴 프로덕션 데이터 절대 미접촉
-----------------------------
본 스크립트는 `scripts/news_pipeline/db.connect()` (프로덕션 stocks.db SSOT)를
**사용하지 않는다**. 신규 격리 파일 `projects/pm320/data/minutes.db` 에만 write.
read/UPSERT 모두 minutes.db 한정. SSOT 4종 무수정.

SSOT 패턴 출처 (verbatim 복제, §11.15 cross-check 완료)
------------------------------------------------------
- `collect_intraday.py` — ka10080 엔드포인트/헤더/body/응답 schema 검증된 출처.
    엔드포인트 POST `{KIWOOM_BASE}/api/dostk/chart`, 헤더 `api-id=ka10080`,
    body `{stk_cd: "A{code}", tic_scope, upd_stkpc_tp: "1"}`,
    응답 리스트 key `stk_min_pole_chart_qry`, 시각 필드 `cntr_tm`(YYYYMMDDHHMMSS),
    가격 `open_pric/high_pric/low_pric/cur_prc`, 거래량 `trde_qty`.
- `collect_dailybars.py:fetch_daily_chart` — cont-yn/next-key 연속조회 헤더 패턴 출처.
    (collect_intraday는 당일 단일 페이지만 사용 → 백필은 연속조회 필수로 확장)
- 키움 가이드 https://openapi.kiwoom.com/m/guide/apiguide?jobTpCode=03 +
    WebSearch 2건 cross-check (tic_scope="1"=1분봉, upd_stkpc_tp="1"=수정주가).

인증: pm320/poc/.env (모의투자 키, 만료 2026-07-05). 차트 TR은 모의/실전 동일 응답.

사용법
------
  # STEP 1 — POC (1종목, 4/8 도달 실측)
  python3 -m scripts.news_pipeline.collect_minutebars --poc 047040

  # STEP 2 — 백필 (POC 성공 후)
  python3 -m scripts.news_pipeline.collect_minutebars --backfill

  # 커스텀 기간/종목
  python3 -m scripts.news_pipeline.collect_minutebars --backfill \
      --start 2026-04-08 --end 2026-05-26
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

from .kiwoom_client import get_token  # noqa: F401

# --- 인증 env (cycle25 env-unification, 2026-05-28) ---------------------------
# 메인 .env 단일 source 일원화. 우선순위: shell export > MAIN_ENV > POC_ENV fallback.
MAIN_ENV = Path("/Users/seongjinpark/company/100m1s/.env")
POC_ENV = Path("/Users/seongjinpark/company/100m1s/projects/pm320/poc/.env")
for env_path in (MAIN_ENV, POC_ENV):
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# 모의투자 우선 (feedback_mock_trading_first.md). 차트 TR은 모의/실전 동일 raw.
KIWOOM_BASE = os.environ.get("KIWOOM_BASE_URL", "https://mockapi.kiwoom.com")
KIWOOM_APPKEY = os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_SECRETKEY")

# --- 격리 DB 경로 (프로덕션 stocks.db 절대 미접촉) ---------------------------
MINUTES_DB = Path("/Users/seongjinpark/company/100m1s/projects/pm320/data/minutes.db")

# 백필 기본 기간 (요청: 2026-04-08 ~ 2026-05-26)
DEFAULT_START = "2026-04-08"
DEFAULT_END = "2026-05-26"

# ka10080 1분봉. 연속조회 안전 상한 (900rows/page × ~2.3일/page → 4/8 도달에 ~20p 필요 추정).
TIC_SCOPE = "1"
MAX_PAGES = 60  # 종목당 페이지 상한 (무한루프 방지, 약 138일분 안전 마진)
PAGE_SLEEP = 0.3  # 페이지 간 throttle (collect_dailybars 동일)
CODE_SLEEP = 0.5  # 종목 간 throttle (collect_intraday 동일)


# --- DB --------------------------------------------------------------------
def connect_minutes() -> sqlite3.Connection:
    """minutes.db 연결 (신규 격리 파일). 프로덕션 db.connect() 미사용."""
    MINUTES_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MINUTES_DB))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS minute_bars (
            code   TEXT NOT NULL,
            dt     TEXT NOT NULL,
            open   INT,
            high   INT,
            low    INT,
            close  INT,
            volume INT,
            PRIMARY KEY (code, dt)
        )"""
    )
    conn.commit()
    return conn


# --- 인증 (collect_intraday.py verbatim) ------------------------------------
def _get_token() -> str:
    # SSOT: kiwoom_client.get_token 위임 (본문 byte-identical, 기존 문구 보존).
    return get_token(KIWOOM_BASE, KIWOOM_APPKEY, KIWOOM_SECRETKEY)


def _pint(raw: object) -> int | None:
    """가격/거래량 정수 파싱 (collect_intraday abs() 통일 — 부호·콤마 제거)."""
    if raw is None:
        return None
    try:
        return abs(int(str(raw).strip().replace(",", "")))
    except (ValueError, TypeError, AttributeError):
        return None


def fetch_minute_bars(
    code: str,
    token: str,
    start_ymd: str,
    tic_scope: str = TIC_SCOPE,
    max_pages: int = MAX_PAGES,
) -> list[tuple[str, int, int, int, int, int]]:
    """ka10080 1분봉 연속조회 — start_ymd(YYYYMMDD) 까지 닿을 때까지 cont-yn 페이지 순회.

    응답 리스트 key `stk_min_pole_chart_qry`, 각 row 최신→과거.
    cntr_tm(YYYYMMDDHHMMSS)을 dt 'YYYY-MM-DD HH:MM' 로 변환.
    start_ymd 이전 봉이 page에 나타나면 그 시점에 조기 종료 (불필요 page 회피).

    반환: [(dt, open, high, low, close, volume), ...] (정렬 무관, UPSERT가 흡수).
    연속조회: 헤더 cont-yn=Y + next-key (collect_dailybars.fetch_daily_chart 패턴).
    """
    base_headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10080",
    }
    body = {"stk_cd": f"A{code}", "tic_scope": tic_scope, "upd_stkpc_tp": "1"}

    out: list[tuple[str, int, int, int, int, int]] = []
    seen_dt: set[str] = set()
    cont = False
    next_key = ""
    reached_start = False

    for page in range(max_pages):
        headers = dict(base_headers)
        if cont:
            headers["cont-yn"] = "Y"
            headers["next-key"] = next_key

        data = None
        for attempt in range(4):
            try:
                r = requests.post(
                    f"{KIWOOM_BASE}/api/dostk/chart",
                    json=body,
                    headers=headers,
                    timeout=20,
                )
            except Exception as e:
                print(f"[collect_minutebars] {code} p{page} exception: {e}")
                return out
            if r.status_code == 429:
                back = 2 ** (attempt + 1)
                print(f"[collect_minutebars] {code} 429, backoff {back}s")
                time.sleep(back)
                continue
            if r.status_code != 200:
                print(
                    f"[collect_minutebars] {code} p{page} http {r.status_code}: "
                    f"{r.text[:200]}"
                )
                return out
            try:
                data = r.json()
            except Exception:
                return out
            if data.get("return_code") == 5:
                back = 2 ** (attempt + 1)
                print(f"[collect_minutebars] {code} rc=5, backoff {back}s")
                time.sleep(back)
                continue
            if data.get("return_code") != 0:
                return out
            break
        else:
            # 4회 재시도 소진
            return out

        if data is None:
            return out
        rows = data.get("stk_min_pole_chart_qry") or []
        if not rows:
            return out

        for row in rows:
            cntr = (row.get("cntr_tm") or "").strip()
            if len(cntr) < 12:
                continue
            ymd = cntr[:8]
            if ymd < start_ymd:
                # 기간 시작점 이전 봉 도달 → 더 깊이 갈 필요 없음 (조기 종료 신호)
                reached_start = True
                continue
            dt = f"{cntr[0:4]}-{cntr[4:6]}-{cntr[6:8]} {cntr[8:10]}:{cntr[10:12]}"
            if dt in seen_dt:
                continue
            o = _pint(row.get("open_pric"))
            h = _pint(row.get("high_pric"))
            lo = _pint(row.get("low_pric"))
            c = _pint(row.get("cur_prc"))
            vol = _pint(row.get("trde_qty"))
            if c is None or c <= 0:
                continue  # 종가 결측 행 제외
            seen_dt.add(dt)
            out.append((dt, o or c, h or c, lo or c, c, vol or 0))

        if reached_start:
            # 시작점 이전 봉이 등장 → 기간 커버 완료, 연속조회 중단
            break

        # 연속조회 판정 (collect_dailybars.fetch_daily_chart 패턴)
        cont_yn = (r.headers.get("cont-yn") or "").upper() == "Y" or (
            str(data.get("cont_yn") or "").upper() == "Y"
        )
        nk = r.headers.get("next-key") or data.get("next_key") or ""
        if cont_yn and nk:
            cont = True
            next_key = nk
            time.sleep(PAGE_SLEEP)
        else:
            break  # 더 이상 과거 데이터 없음 (history 한계 도달)

    return out


# --- POC -------------------------------------------------------------------
def run_poc(code: str, start: str) -> int:
    """1종목 ka10080 1분봉 연속조회 실측 — start(YYYY-MM-DD)까지 닿는지 확인.

    보고: 가장 오래된 분봉 일시 + 가장 최신 분봉 일시 + 총 분봉수 + 샘플 3행.
    start 못 닿으면 STOP 신호 (exit code 3). 억지 진행 금지 (§16 정직).
    """
    start_ymd = start.replace("-", "")
    print(
        f"[POC] code={code} target_start={start} (ka10080 tic_scope=1) base={KIWOOM_BASE}"
    )
    try:
        token = _get_token()
    except Exception as e:
        print(f"[POC] token FAIL: {e}")
        return 1

    bars = fetch_minute_bars(code, token, start_ymd)
    if not bars:
        print(f"[POC] {code} 분봉 0건 — 데이터 부족, STOP")
        return 3

    bars_sorted = sorted(bars, key=lambda x: x[0])
    oldest = bars_sorted[0][0]
    newest = bars_sorted[-1][0]
    oldest_ymd = oldest[:10].replace("-", "")
    reached = oldest_ymd <= start_ymd

    print(f"[POC] 총 분봉수: {len(bars_sorted)}")
    print(f"[POC] 가장 오래된 분봉: {oldest}")
    print(f"[POC] 가장 최신 분봉:   {newest}")
    print(
        f"[POC] {start} 도달: {'YES' if reached else 'NO'} "
        f"(oldest_ymd={oldest_ymd} vs target={start_ymd})"
    )
    print("[POC] 샘플 3행 (dt, open, high, low, close, volume):")
    for b in bars_sorted[:3]:
        print(f"  {b}")
    print("  ...")
    for b in bars_sorted[-1:]:
        print(f"  {b}")

    if not reached:
        print(f"[POC] ❌ {start} 미도달 — STOP. history 한계 보고. (억지 진행 금지)")
        return 3
    print(f"[POC] ✅ {start} 도달 확인 — STEP 2 백필 진행 가능.")
    return 0


# --- 백필 ------------------------------------------------------------------
def _load_target_codes() -> list[str]:
    """두 백테스트 결과파일의 distinct code 합집합 (재귀 추출).

    martingale_results.json: trades[].code
    filter_first_results.json: trades 부재 → universe_*.{excluded,by_rank,buy_date_grid} 중첩.
    → 재귀 walk로 모든 'code' key 추출 (§16: trades 가정 금지, 실 구조 반영).
    """
    research = Path(
        "/Users/seongjinpark/company/100m1s/projects/pm320/research/backtest-3d-3.2pct"
    )
    files = ["martingale_results.json", "filter_first_results.json"]
    codes: set[str] = set()

    def walk(o: object) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "code" and isinstance(v, (str, int)):
                    cs = str(v).zfill(6)
                    if cs.isdigit() and len(cs) == 6:
                        codes.add(cs)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    for f in files:
        p = research / f
        if not p.exists():
            print(f"[collect_minutebars] WARN: {p} 부재 — skip")
            continue
        walk(json.loads(p.read_text()))
    return sorted(codes)


def run_backfill(start: str, end: str, codes: list[str] | None = None) -> int:
    """대상 종목 1분봉 백필 → minutes.db.minute_bars UPSERT.

    end는 응답 필터 상한 (end 이후 봉 제외). 종목 간 throttle 준수.
    반환: 적재 성공 종목 수.
    """
    start_ymd = start.replace("-", "")
    end_ymd = end.replace("-", "")
    if codes is None:
        codes = _load_target_codes()
    if not codes:
        print("[collect_minutebars] 대상 종목 없음 — STOP")
        return 0

    print(
        f"[collect_minutebars] backfill {len(codes)} codes {start}~{end} → {MINUTES_DB}"
    )
    try:
        token = _get_token()
    except Exception as e:
        print(f"[collect_minutebars] token FAIL: {e}")
        return 0

    conn = connect_minutes()
    saved_codes = 0
    total_rows = 0
    failed: list[str] = []

    for i, code in enumerate(codes):
        bars = fetch_minute_bars(code, token, start_ymd)
        # end 상한 필터 (백필 기간 외 봉 제외)
        bars = [b for b in bars if b[0][:10].replace("-", "") <= end_ymd]
        if not bars:
            print(f"  [{i + 1}/{len(codes)}] {code} SKIP (no data in range)")
            failed.append(code)
        else:
            conn.executemany(
                """INSERT INTO minute_bars (code, dt, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code, dt) DO UPDATE SET
                     open=excluded.open, high=excluded.high, low=excluded.low,
                     close=excluded.close, volume=excluded.volume""",
                [(code, *b) for b in bars],
            )
            conn.commit()
            saved_codes += 1
            total_rows += len(bars)
            oldest = min(b[0] for b in bars)
            newest = max(b[0] for b in bars)
            print(
                f"  [{i + 1}/{len(codes)}] {code} OK rows={len(bars)} "
                f"{oldest} ~ {newest}"
            )
        if i < len(codes) - 1:
            time.sleep(CODE_SLEEP)

    # 커버리지 요약
    cur = conn.execute("SELECT MIN(dt), MAX(dt), COUNT(*) FROM minute_bars")
    mn, mx, cnt = cur.fetchone()
    conn.close()

    print(
        f"[collect_minutebars] DONE: {saved_codes}/{len(codes)} codes saved, "
        f"this-run rows={total_rows}, db total rows={cnt}, coverage {mn} ~ {mx}"
    )
    if failed:
        print(f"[collect_minutebars] 결측 종목 {len(failed)}: {failed}")
    return saved_codes


def main() -> int:
    ap = argparse.ArgumentParser(description="키움 ka10080 1분봉 수집 (PM320 MDD 백필)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--poc", metavar="CODE", help="POC: 1종목 4/8 도달 실측")
    g.add_argument("--backfill", action="store_true", help="백필: 대상 종목 전체")
    ap.add_argument(
        "--start", default=DEFAULT_START, help=f"시작일 (기본 {DEFAULT_START})"
    )
    ap.add_argument("--end", default=DEFAULT_END, help=f"종료일 (기본 {DEFAULT_END})")
    ap.add_argument(
        "--codes", nargs="*", help="백필 종목 직접 지정 (생략 시 결과파일 합집합)"
    )
    args = ap.parse_args()

    # 장중(09:00~15:35 KST) 연구수집 차단 — 프로덕션 폴링과 키움 앱키·호출한도
    # 경합 원천 차단 (대표 지시 2026-05-26). ALLOW_INTRADAY_RESEARCH=1 시만 허용.
    from .config import guard_intraday_research

    guard_intraday_research("collect_minutebars (PM320 MDD 백필)")

    if args.poc:
        return run_poc(args.poc, args.start)
    return 0 if run_backfill(args.start, args.end, args.codes) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
