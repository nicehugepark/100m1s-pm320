"""
키움 kt20016 신용융자 가능종목 수집.

실전키 전용 (KIWOOM_LIVE_*) — 모의투자 미제공.
조회 TR만 호출. 주문 금지.
1일 1회 배치. 로컬 고정 IP에서만 실행 (GitHub Actions/cron 원격 전환 시 중단).

실측 (2026-04-14):
  - api-id: kt20016, endpoint: POST /api/dostk/stkinfo
  - body: {"mrkt_deal_tp": "1", "crd_stk_grde_tp": "<A~E>"}
    · mrkt_deal_tp='1' 하나로 전 시장 커버 (10과 동일)
    · crd_stk_grde_tp는 A~E 등급별 조회 필수, 전체 합산 시 약 1,150개
  - response key: "crd_loan_pos_stk"
  - stk_cd는 "A005290" 형태 (A 접두사 제거 필요)
  - 연속조회: resp.headers['cont-yn']='Y', ['next-key']=<code>

REQ-001 S2 (2026-04-22): kt20017 호출 주기 조정
  - 기존: 매 cron마다 daily_picks 전 종목 kt20017 재조회 (N=40 기준 ~106s)
  - 변경: 장전/장후 full scan + 장중에는 신규 종목(오늘자 status 레코드 없음)만 증분
  - 기대: credit dur 106s → ~11s (-90%)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests

# cycle25 env-unification (2026-05-28): 메인 .env 단일 source 일원화.
# 우선순위: shell-level export (kiwoom_cron.sh) > MAIN_ENV setdefault > POC_ENV fallback (deprecated).
# kiwoom_cron.sh ENV_FILE = MAIN_ENV 정합. POC_ENV 는 직접 호출 (cron 외) 시 fallback only.
# S5 자립화 (DOC-20260707-REQ-001): 메인 레포 절대경로 → env(M1S_COMPANY) 우선 + pm320 레포 로컬 fallback.
_M1S_COMPANY = Path(os.environ.get("M1S_COMPANY", str(Path(__file__).resolve().parents[2])))
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

from .db import connect
from .kiwoom_client import get_token  # noqa: E402,F401

KIWOOM_LIVE_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL", "https://api.kiwoom.com")
KIWOOM_LIVE_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY")
KIWOOM_LIVE_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY")

# 실측 기반: A~E 등급만 유효
CRD_GRADES = ("A", "B", "C", "D", "E")


def _get_token() -> str:
    # SSOT: kiwoom_client.get_token 위임 (본문 byte-identical, 기존 LIVE_ 문구 보존).
    return get_token(
        KIWOOM_LIVE_BASE,
        KIWOOM_LIVE_APPKEY,
        KIWOOM_LIVE_SECRETKEY,
        key_label="KIWOOM_LIVE_APPKEY/SECRETKEY",
    )


def _fetch_grade(token: str, grade: str) -> list:
    """특정 등급 신용가능 종목 전체 페이지 수집. 429 자동 백오프."""
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "kt20016",
    }
    body = {"mrkt_deal_tp": "1", "crd_stk_grde_tp": grade}
    out = []
    for _page in range(50):
        backoff = 1.0
        for _attempt in range(6):
            r = requests.post(
                f"{KIWOOM_LIVE_BASE}/api/dostk/stkinfo",
                json=body,
                headers=headers,
                timeout=20,
            )
            if r.status_code == 429:
                print(f"[collect_credit] grade={grade} 429 retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            break
        if r.status_code != 200:
            print(f"[collect_credit] grade={grade} http {r.status_code}")
            break
        data = r.json()
        rc = data.get("return_code")
        if rc != 0:
            # 데이터 없음(507287)은 정상 — 등급 없음
            if "507287" not in (data.get("return_msg") or ""):
                print(
                    f"[collect_credit] grade={grade} rc={rc} msg={data.get('return_msg')}"
                )
            break
        stocks = data.get("crd_loan_pos_stk", []) or []
        out.extend(stocks)
        cont = r.headers.get("cont-yn") or r.headers.get("Cont-Yn")
        nk = r.headers.get("next-key") or r.headers.get("Next-Key")
        if cont == "Y" and nk:
            headers["cont-yn"] = "Y"
            headers["next-key"] = nk
        else:
            break
    return out


def fetch_credit_eligible() -> set:
    """전체 신용가능 종목코드(6자리, A 접두사 제거) set."""
    token = _get_token()
    eligible = set()
    for g in CRD_GRADES:
        stocks = _fetch_grade(token, g)
        for s in stocks:
            code = (s.get("stk_cd") or "").lstrip("A")
            if len(code) == 6 and code.isdigit():
                eligible.add(code)
        time.sleep(1.5)  # 등급 간 rate limit 예방
    return eligible


def _fetch_stock_limit(token: str, code: str) -> dict | None:
    """kt20017 단건 조회. 키움 계좌 당일 신용융자 가능 여부.

    반환: {"raw_status": str, "limit_exceeded": bool} or None (실패).
    body stk_cd는 'A' 접두사 포함 형태.
    """
    import time as _t

    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "kt20017",
    }
    body = {"stk_cd": f"A{code}"}
    for attempt in range(3):
        try:
            r = requests.post(
                f"{KIWOOM_LIVE_BASE}/api/dostk/stkinfo",
                json=body,
                headers=headers,
                timeout=20,
            )
        except Exception as e:
            print(f"[kt20017] {code} exception: {e}")
            return None
        if r.status_code == 429:
            back = 2**attempt
            print(f"[kt20017] {code} 429, backoff {back}s")
            _t.sleep(back)
            continue
        if r.status_code != 200:
            print(f"[kt20017] {code} http {r.status_code}: {r.text[:200]}")
            return None
        try:
            data = r.json()
        except Exception:
            return None
        # S-1 (2026-05-26, DOC-20260526-REVIEW-001 §S-1): kt20017 rc=5 (rate-limit soft)
        # 백오프 재시도. 다른 6개 collect 파일은 전부 rc=5 분기 보유하나 본 kt20017 루프만
        # 누락 → rc=5 (200 OK + body return_code=5) 시 곧장 line 아래 `return_code != 0`
        # 에서 조용히 return None → 해당 종목 신용정보 무로그 누락 (부분 데이터 유실).
        # backoff 는 같은 루프 429(line 위)와 동일 정책(2**attempt: 1→2→4s, 3시도) 채택
        # — within-function 일관성. (KiwoomClient SSOT 전환 시 단일 정책으로 흡수 예정.)
        if data.get("return_code") == 5:
            back = 2**attempt
            print(f"[kt20017] {code} rc=5, backoff {back}s")
            _t.sleep(back)
            continue
        if data.get("return_code") != 0:
            return None
        raw = (data.get("crd_alow_yn") or "").strip()
        if not raw:
            return None
        allowed = "불가능" not in raw
        # "< C군 신용융자 가능 >" → grade="C". "불가능"이면 None.
        import re as _re

        m = _re.search(r"([A-E])\s*군", raw) if allowed else None
        grade = m.group(1) if m else None
        return {
            "raw_status": raw,
            "credit_allowed": allowed,
            "grade": grade,
            "limit_exceeded": not allowed,
        }
    return None


def fetch_per_stock_limit(codes: list[str]) -> dict:
    """daily_picks 등 주어진 코드 목록에 대해 kt20017 단건 상태 수집.

    rate limit: 2.5초/호출 엄격. 50종목 기준 약 125초.
    반환: {code: {"raw_status": str, "limit_exceeded": bool}}
    """
    import time as _t

    token = _get_token()
    out: dict[str, dict] = {}
    for i, code in enumerate(codes):
        if not (len(code) == 6 and code.isdigit()):
            continue
        res = _fetch_stock_limit(token, code)
        if res is not None:
            out[code] = res
            mark = "X" if res["limit_exceeded"] else "O"
            print(f"  [{i + 1}/{len(codes)}] {code} {mark} {res['raw_status']}")
        else:
            print(f"  [{i + 1}/{len(codes)}] {code} SKIP")
        if i < len(codes) - 1:
            _t.sleep(2.5)
    return out


def save_stock_status(results: dict, date_str: str) -> None:
    with connect() as c:
        # 최신 스키마 (credit_allowed/grade 포함). 기존 설치는 migration으로 컬럼 추가.
        c.execute(
            """CREATE TABLE IF NOT EXISTS credit_stock_status (
                date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                credit_allowed INTEGER,
                grade TEXT,
                limit_exceeded INTEGER NOT NULL,
                raw_status TEXT,
                PRIMARY KEY (date, stock_code)
            )"""
        )
        cols = {
            r[1] for r in c.execute("PRAGMA table_info(credit_stock_status)").fetchall()
        }
        if "credit_allowed" not in cols:
            c.execute(
                "ALTER TABLE credit_stock_status ADD COLUMN credit_allowed INTEGER"
            )
        if "grade" not in cols:
            c.execute("ALTER TABLE credit_stock_status ADD COLUMN grade TEXT")
        # 증분 upsert — 동일 코드 재조회 시 최신값으로 갱신.
        c.executemany(
            """INSERT INTO credit_stock_status
                 (date, stock_code, credit_allowed, grade, limit_exceeded, raw_status)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(date, stock_code) DO UPDATE SET
                 credit_allowed=excluded.credit_allowed,
                 grade=excluded.grade,
                 limit_exceeded=excluded.limit_exceeded,
                 raw_status=excluded.raw_status""",
            [
                (
                    date_str,
                    code,
                    1 if r.get("credit_allowed") else 0,
                    r.get("grade"),
                    1 if r.get("limit_exceeded") else 0,
                    r["raw_status"],
                )
                for code, r in results.items()
            ],
        )
        c.commit()


def _should_run_full_scan(now=None) -> bool:
    """REQ-001 S2: 현재 시각이 full scan 윈도우(장전 09:00~09:15 또는 장후 15:40~16:10)인지 판정.

    - 장시작 윈도우: 09:00 ~ 09:15 (KRX 정규장 시작 직후 첫 cron)
    - 장마감 윈도우: 15:40 ~ 16:10 (정규장 마감 15:30 직후 cron 포함)
    - 그 외: 증분 모드 (daily_picks 신규 종목만)
    """
    from datetime import datetime
    from datetime import time as dtime

    try:
        from zoneinfo import ZoneInfo

        kst = ZoneInfo("Asia/Seoul")
    except Exception:
        kst = None
    n = now or (datetime.now(kst) if kst else datetime.now())
    t = n.time()
    morning_start = dtime(9, 0)
    morning_end = dtime(9, 15)
    evening_start = dtime(15, 40)
    evening_end = dtime(16, 10)
    return (morning_start <= t <= morning_end) or (evening_start <= t <= evening_end)


def collect_per_stock(
    codes: list[str] | None = None,
    date_str: str | None = None,
    mode: str | None = None,
) -> int:
    """daily_picks(또는 주어진 코드들)에 대해 kt20017 단건 상태 수집.

    Args:
        codes: 명시 코드 목록. None이면 daily_picks에서 로드.
        date_str: 기준 날짜. None이면 pipeline_date().
        mode: "full" | "incremental" | None(자동 판정).
              - full: 전 종목 재조회 (장전·장후)
              - incremental: credit_stock_status(오늘자)에 아직 없는 종목만 조회 (장중)
    """
    from .config import pipeline_date

    today = date_str or pipeline_date()
    if mode is None:
        mode = "full" if _should_run_full_scan() else "incremental"
    if codes is None:
        with connect() as c:
            rows = c.execute(
                "SELECT DISTINCT stock_code FROM daily_picks WHERE date=?", (today,)
            ).fetchall()
            codes = [r[0] for r in rows if r[0]]
    total_picks = len(codes)
    if mode == "incremental" and codes:
        with connect() as c:
            done_rows = c.execute(
                "SELECT stock_code FROM credit_stock_status WHERE date=?", (today,)
            ).fetchall()
            done = {r[0] for r in done_rows}
        codes = [cd for cd in codes if cd not in done]
        print(
            f"[collect_credit/kt20017] mode=incremental picks={total_picks} "
            f"already_done={total_picks - len(codes)} to_fetch={len(codes)}"
        )
    else:
        print(
            f"[collect_credit/kt20017] mode={mode} picks={total_picks} "
            f"to_fetch={len(codes)}"
        )
    if not codes:
        print("[collect_credit/kt20017] 대상 종목 없음 — skip")
        return 0
    print(f"[collect_credit/kt20017] {len(codes)}종목 조회 시작 (date={today})")
    try:
        results = fetch_per_stock_limit(codes)
    except Exception as e:
        print(f"[collect_credit/kt20017] FAIL: {e}")
        return 0
    if not results:
        print("[collect_credit/kt20017] 결과 0 — skip save")
        return 0
    save_stock_status(results, today)
    n_ex = sum(1 for r in results.values() if r["limit_exceeded"])
    print(
        f"[collect_credit/kt20017] {len(results)}건 저장 "
        f"(limit_exceeded={n_ex}) for {today} [mode={mode}]"
    )
    return len(results)


def save_snapshot(eligible: set, date_str: str) -> None:
    with connect() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS credit_eligible (
            date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            PRIMARY KEY (date, stock_code)
        )"""
        )
        c.execute("DELETE FROM credit_eligible WHERE date=?", (date_str,))
        c.executemany(
            "INSERT INTO credit_eligible (date, stock_code) VALUES (?,?)",
            [(date_str, code) for code in eligible],
        )
        c.commit()


def collect(date_str=None) -> int:
    from .config import pipeline_date

    today = date_str or pipeline_date()
    try:
        eligible = fetch_credit_eligible()
    except Exception as e:
        print(f"[collect_credit] FAIL: {e}")
        return 0
    if not eligible:
        print("[collect_credit] 0 eligible — skip save")
        return 0
    save_snapshot(eligible, today)
    print(f"[collect_credit] {len(eligible)} eligible stocks saved for {today}")
    # kt20017 단건 배치 이어서 실행 (회사한도초과 감지)
    try:
        collect_per_stock(date_str=today)
    except Exception as e:
        print(f"[collect_credit/kt20017] 단건 배치 FAIL: {e}")
    return len(eligible)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "per-stock":
        # kt20017만 단독 실행 (재배치/디버깅용)
        collect_per_stock()
    else:
        collect()
