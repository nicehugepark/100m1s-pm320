"""키움 ka10080 1분봉 수집 (NXT/통합 거래소, 장전·장후 포함) — PM320 백테스트용.

요청: 2026-05-26 (화) 타치코마 → 개발팀 (대표 "NXT 대체거래소 분봉 빨리 구해와").

목적
----
기존 `collect_minutebars.py` (minutes.db) 는 KRX 정규장(09:00~15:35)만 적재됨.
본 스크립트는 **통합 거래소(KRX+NXT) 시세**를 종목코드 suffix `_AL` 로 조회하여
NXT 장전(08:00~09:00)·정규장·장후(15:40~20:00) 1분봉을 신규 격리 DB
`minutes_nxt.db` 에 적재한다.

🔴 프로덕션 데이터 절대 미접촉
-----------------------------
- production stocks.db: 미사용 (db.connect() 호출 0건).
- 기존 minutes.db: 미접촉 (read/write 모두 안 함).
- write 대상: 신규 파일 `projects/pm320/data/minutes_nxt.db` 한정.
- SSOT 4종 무수정.

거래소구분 spec 검증 (§11.15, 2026-05-26 실측 완료)
---------------------------------------------------
- WebSearch 2건 + i-whale ATS 블로그 + 키움 공식 guide cross-check.
- **결정적 실측** (read-only ka10080 probe, mock+live, 005930):
    * `stex_tp` body 파라미터 (1=KRX/2=NXT/3=통합) → ka10080 에는 **무효**.
      stex_tp=2/3 줘도 09:00~15:35 KRX-only 반환.
    * **종목코드 suffix 가 실제 동작**: `{code}_AL`(통합) / `{code}_NX`(NXT) →
      TOD 0800~1959 반환 (장전 50봉 + 장후 260+봉/page).
    * **mock API 는 NXT/통합 미지원** (suffix 시 rc=5). → live 키 필수.
    * cont-yn 연속조회로 4/7 도달 (4/8 target 초과 커버), 과거 page 도 장전/장후 유지.
      005930 _AL: 33일, 21600행, pre-0900 1595봉, post-1535 8134봉 실측.
- 따라서 본 스크립트는 **suffix `_AL` + live 키** 채택. stex_tp 미사용.

인증: KIWOOM_LIVE_* (실전키, 만료 2027-04-01). 차트 TR 은 조회 전용(주문 아님).
      mock 키는 NXT 미반환이므로 사용 불가 (실측 근거).

사용법
------
  # POC (1종목, 4/8 도달 + 장전/장후 봉 실존 확인)
  python3 -m scripts.news_pipeline.collect_minutebars_nxt --poc 005930

  # 백필 (POC 성공 후, 결과파일 합집합 종목)
  python3 -m scripts.news_pipeline.collect_minutebars_nxt --backfill

  # 커스텀
  python3 -m scripts.news_pipeline.collect_minutebars_nxt --backfill \
      --start 2026-04-08 --end 2026-05-26 --codes 005930 047040
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from .kiwoom_client import get_token  # noqa: F401

# --- 인증 env (cycle25 env-unification, 2026-05-28) ---------------------------
# 메인 .env 단일 source 일원화. 우선순위: shell export > MAIN_ENV > POC_ENV fallback.
# S5 자립화 (DOC-20260707-REQ-001): 메인 레포 절대경로 → env(M1S_COMPANY) 우선 + pm320 레포 로컬 fallback.
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

# 🔴 NXT/통합은 live 키만 반환 (실측). mock 키는 suffix 시 rc=5.
KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL", "https://api.kiwoom.com")
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY")

# --- 격리 DB 경로 (프로덕션 stocks.db / 기존 minutes.db 절대 미접촉) ---------
# S5 자립화 (DOC-20260707-REQ-001): 메인 레포 projects 경로 → pm320 레포 로컬 data/.
MINUTES_NXT_DB = _M1S_COMPANY / "data" / "minutes_nxt.db"

# 당일 display universe SSOT (Q-20260608-rewire): M1S_HOMEPAGE/data/interpreted.
# build_card_history.INTERPRETED_DIR 와 동일 source (load_card_universe L228 정합).
# M1S_HOMEPAGE 는 main() 의 config import 전이라 여기서 직접 환경변수 참조(부재 시
# config 가 main 진입 시 RuntimeError 로 차단하므로 default 경로 보강만).
INTERPRETED_DIR = (
    Path(os.environ.get("M1S_HOMEPAGE", str(_M1S_COMPANY))) / "data" / "interpreted"
)

DEFAULT_START = "2026-04-08"
DEFAULT_END = "2026-05-26"

# 거래소구분: _AL = 통합(KRX+NXT). suffix 가 실제 동작 (stex_tp 무효, 실측).
EXCHANGE_SUFFIX = "_AL"
TIC_SCOPE = "1"
# _AL 은 12시간(08~20시) 커버라 KRX-only 대비 봉수 ~2배 → page 상한 상향.
MAX_PAGES = 80
PAGE_SLEEP = 0.3
CODE_SLEEP = 0.5

# 정규장 경계 (session 마킹용). 09:00~15:35 = regular, 그 외 = extended(NXT).
REGULAR_START = "0900"
REGULAR_END = "1535"


def connect_minutes() -> sqlite3.Connection:
    """minutes_nxt.db 연결 (신규 격리 파일). session 컬럼으로 정규장/시간외 구분."""
    MINUTES_NXT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MINUTES_NXT_DB))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS minute_bars (
            code    TEXT NOT NULL,
            dt      TEXT NOT NULL,
            open    INT,
            high    INT,
            low     INT,
            close   INT,
            volume  INT,
            session TEXT NOT NULL,       -- 'regular' | 'extended'
            PRIMARY KEY (code, dt)
        )"""
    )
    conn.commit()
    return conn


def _session_of(hhmm: str) -> str:
    return "regular" if REGULAR_START <= hhmm <= REGULAR_END else "extended"


def _get_token() -> str:
    # SSOT: kiwoom_client.get_token 위임 (본문 byte-identical, LIVE_ 문구 보존).
    return get_token(
        KIWOOM_BASE,
        KIWOOM_APPKEY,
        KIWOOM_SECRETKEY,
        key_label="KIWOOM_LIVE_APPKEY/SECRETKEY",
    )


def _pint(raw: object) -> int | None:
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
) -> list[tuple[str, int, int, int, int, int, str]]:
    """ka10080 통합(_AL) 1분봉 연속조회 — start_ymd 까지 cont-yn 페이지 순회.

    반환: [(dt, open, high, low, close, volume, session), ...].
    stk_cd = '{code}_AL' (통합 거래소, 장전·장후 포함). 실측 verbatim.
    """
    base_headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10080",
    }
    body = {
        "stk_cd": f"{code}{EXCHANGE_SUFFIX}",
        "tic_scope": tic_scope,
        "upd_stkpc_tp": "1",
    }

    out: list[tuple[str, int, int, int, int, int, str]] = []
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
        r = None
        for attempt in range(5):
            try:
                r = requests.post(
                    f"{KIWOOM_BASE}/api/dostk/chart",
                    json=body,
                    headers=headers,
                    timeout=20,
                )
            except Exception as e:
                print(f"[collect_minutebars_nxt] {code} p{page} exception: {e}")
                return out
            if r.status_code == 429:
                back = 2 ** (attempt + 1)
                print(f"[collect_minutebars_nxt] {code} 429, backoff {back}s")
                time.sleep(back)
                continue
            if r.status_code != 200:
                print(
                    f"[collect_minutebars_nxt] {code} p{page} http {r.status_code}: "
                    f"{r.text[:200]}"
                )
                return out
            try:
                data = r.json()
            except Exception:
                return out
            if data.get("return_code") == 5:
                back = 2 ** (attempt + 1)
                print(f"[collect_minutebars_nxt] {code} rc=5, backoff {back}s")
                time.sleep(back)
                continue
            if data.get("return_code") != 0:
                return out
            break
        else:
            return out

        if data is None or r is None:
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
                reached_start = True
                continue
            hhmm = cntr[8:12]
            dt = f"{cntr[0:4]}-{cntr[4:6]}-{cntr[6:8]} {cntr[8:10]}:{cntr[10:12]}"
            if dt in seen_dt:
                continue
            o = _pint(row.get("open_pric"))
            h = _pint(row.get("high_pric"))
            lo = _pint(row.get("low_pric"))
            c = _pint(row.get("cur_prc"))
            vol = _pint(row.get("trde_qty"))
            if c is None or c <= 0:
                continue
            seen_dt.add(dt)
            out.append((dt, o or c, h or c, lo or c, c, vol or 0, _session_of(hhmm)))

        if reached_start:
            break

        cont_yn = (r.headers.get("cont-yn") or "").upper() == "Y" or (
            str(data.get("cont_yn") or "").upper() == "Y"
        )
        nk = r.headers.get("next-key") or data.get("next_key") or ""
        if cont_yn and nk:
            cont = True
            next_key = nk
            time.sleep(PAGE_SLEEP)
        else:
            break

    return out


def run_poc(code: str, start: str) -> int:
    """1종목 통합(_AL) 1분봉 실측 — 4/8 도달 + 장전/장후 봉 실존 확인."""
    start_ymd = start.replace("-", "")
    print(
        f"[POC-NXT] code={code}{EXCHANGE_SUFFIX} target_start={start} "
        f"base={KIWOOM_BASE} (live)"
    )
    try:
        token = _get_token()
    except Exception as e:
        print(f"[POC-NXT] token FAIL: {e}")
        return 1

    bars = fetch_minute_bars(code, token, start_ymd)
    if not bars:
        print(f"[POC-NXT] {code} 분봉 0건 — 데이터 부족, STOP")
        return 3

    bars_sorted = sorted(bars, key=lambda x: x[0])
    oldest = bars_sorted[0][0]
    newest = bars_sorted[-1][0]
    oldest_ymd = oldest[:10].replace("-", "")
    reached = oldest_ymd <= start_ymd
    n_reg = sum(1 for b in bars_sorted if b[6] == "regular")
    n_ext = sum(1 for b in bars_sorted if b[6] == "extended")
    n_pre = sum(1 for b in bars_sorted if b[0][11:16] < "09:00")
    n_post = sum(1 for b in bars_sorted if b[0][11:16] > "15:35")

    print(
        f"[POC-NXT] 총 분봉수: {len(bars_sorted)}  (regular={n_reg}, extended={n_ext})"
    )
    print(f"[POC-NXT] 장전(<09:00) 봉: {n_pre}   장후(>15:35) 봉: {n_post}")
    print(f"[POC-NXT] 가장 오래된 분봉: {oldest}")
    print(f"[POC-NXT] 가장 최신 분봉:   {newest}")
    print(
        f"[POC-NXT] {start} 도달: {'YES' if reached else 'NO'} "
        f"(oldest_ymd={oldest_ymd} vs target={start_ymd})"
    )
    print("[POC-NXT] 샘플 장전 3행 (dt, o, h, l, c, vol, session):")
    pre_rows = [b for b in bars_sorted if b[0][11:16] < "09:00"]
    for b in pre_rows[:3]:
        print(f"  {b}")
    print("[POC-NXT] 샘플 장후 3행:")
    post_rows = [b for b in bars_sorted if b[0][11:16] > "15:35"]
    for b in post_rows[:3]:
        print(f"  {b}")

    if not reached:
        print(f"[POC-NXT] ❌ {start} 미도달 — STOP. history 한계 보고.")
        return 3
    if n_pre == 0 and n_post == 0:
        print("[POC-NXT] ❌ 장전·장후 봉 0건 — 통합거래소 미반환. STOP.")
        return 3
    print(f"[POC-NXT] ✅ {start} 도달 + 장전/장후 실존 — 백필 진행 가능.")
    return 0


def _load_target_codes() -> list[str]:
    """default 분봉 수집 대상 = **당일 display universe** (그날 카드 종목).

    Q-20260608-rewire (lead 승인 2026-06-08 18:43): 분봉 수집 출처를 5/30 백테스트
    산출물(martingale_results.json + filter_first_results.json, mtime 고정 159종목)에서
    **당일 `interpreted/stock-{today}.json` stocks[]** 로 통일. 화면 카드 universe(거래대금
    상위, 매일 갱신)와 분봉 출처 불일치 해소 — build_card_history `load_card_universe`(L228)와
    동일 SSOT. daily cron이 본 default 로 그날 신규 카드 종목만 증분 수집(과거 union은
    `--codes` 일회성 백필이 커버).

    경로: M1S_HOMEPAGE/data/interpreted/stock-{today}.json (config.HOMEPAGE 정합).
    파일 부재 시 빈 list 반환(run_backfill 이 STOP) — 백테스트 파일 fallback 폐기.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    fp = INTERPRETED_DIR / f"stock-{today}.json"
    if not fp.exists():
        print(f"[collect_minutebars_nxt] WARN: 당일 카드 universe 부재: {fp} — 대상 0")
        return []
    codes: set[str] = set()
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            f"[collect_minutebars_nxt] FAIL: universe parse {type(exc).__name__} — 대상 0"
        )
        return []
    for s in d.get("stocks", []) or []:
        cs = str((s or {}).get("code", "")).zfill(6)
        if cs.isdigit() and len(cs) == 6:
            codes.add(cs)
    return sorted(codes)


def run_backfill(start: str, end: str, codes: list[str] | None = None) -> int:
    """통합(_AL) 1분봉 백필 → minutes_nxt.db UPSERT."""
    start_ymd = start.replace("-", "")
    end_ymd = end.replace("-", "")
    if codes is None:
        codes = _load_target_codes()
    if not codes:
        print("[collect_minutebars_nxt] 대상 종목 없음 — STOP")
        return 0

    print(
        f"[collect_minutebars_nxt] backfill {len(codes)} codes {start}~{end} "
        f"(통합 _AL, 장전·장후 포함) → {MINUTES_NXT_DB}"
    )
    try:
        token = _get_token()
    except Exception as e:
        print(f"[collect_minutebars_nxt] token FAIL: {e}")
        return 0

    conn = connect_minutes()
    saved_codes = 0
    total_rows = 0
    total_ext = 0
    failed: list[str] = []

    for i, code in enumerate(codes):
        bars = fetch_minute_bars(code, token, start_ymd)
        bars = [b for b in bars if b[0][:10].replace("-", "") <= end_ymd]
        if not bars:
            print(f"  [{i + 1}/{len(codes)}] {code} SKIP (no data in range)")
            failed.append(code)
        else:
            conn.executemany(
                """INSERT INTO minute_bars
                       (code, dt, open, high, low, close, volume, session)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code, dt) DO UPDATE SET
                     open=excluded.open, high=excluded.high, low=excluded.low,
                     close=excluded.close, volume=excluded.volume,
                     session=excluded.session""",
                [(code, *b) for b in bars],
            )
            conn.commit()
            saved_codes += 1
            total_rows += len(bars)
            n_ext = sum(1 for b in bars if b[6] == "extended")
            total_ext += n_ext
            oldest = min(b[0] for b in bars)
            newest = max(b[0] for b in bars)
            print(
                f"  [{i + 1}/{len(codes)}] {code} OK rows={len(bars)} "
                f"(ext={n_ext}) {oldest} ~ {newest}"
            )
        if i < len(codes) - 1:
            time.sleep(CODE_SLEEP)

    cur = conn.execute(
        "SELECT MIN(dt), MAX(dt), COUNT(*), "
        "SUM(CASE WHEN session='extended' THEN 1 ELSE 0 END) FROM minute_bars"
    )
    mn, mx, cnt, ext = cur.fetchone()
    conn.close()

    print(
        f"[collect_minutebars_nxt] DONE: {saved_codes}/{len(codes)} codes saved, "
        f"this-run rows={total_rows} (ext={total_ext}), "
        f"db total rows={cnt} (ext={ext}), coverage {mn} ~ {mx}"
    )
    if failed:
        print(f"[collect_minutebars_nxt] 결측 종목 {len(failed)}: {failed}")
    return saved_codes


def main() -> int:
    ap = argparse.ArgumentParser(
        description="키움 ka10080 1분봉 수집 (NXT/통합 _AL, 장전·장후 포함)"
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--poc", metavar="CODE", help="POC: 1종목 4/8 도달 + 장전/장후 실측")
    g.add_argument("--backfill", action="store_true", help="백필: 대상 종목 전체")
    ap.add_argument(
        "--start", default=DEFAULT_START, help=f"시작일 (기본 {DEFAULT_START})"
    )
    ap.add_argument("--end", default=DEFAULT_END, help=f"종료일 (기본 {DEFAULT_END})")
    ap.add_argument("--codes", nargs="*", help="백필 종목 직접 지정 (생략 시 합집합)")
    args = ap.parse_args()

    # 장중(09:00~15:35 KST) 연구수집 차단 — 프로덕션 폴링과 키움 앱키·호출한도
    # 경합 원천 차단 (대표 지시 2026-05-26). ALLOW_INTRADAY_RESEARCH=1 시만 허용.
    from .config import guard_intraday_research

    guard_intraday_research("collect_minutebars_nxt (NXT 분봉 백필)")

    if args.poc:
        return run_poc(args.poc, args.start)
    return 0 if run_backfill(args.start, args.end, args.codes) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
