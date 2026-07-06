#!/usr/bin/env python3
"""NXT 동시상장 roster + 상장주식수 일일 수집 → nxt_roster.json (조니 단정 A·B 데이터 레이어).

목적: PM320 카드의 (A) NXT 동시상장 여부 marker + (B) 상장주식수(시총 산출) 를
  날짜별 스냅샷으로 적재. launchd com.100m1s.nxt-roster — 평일 07:50 + 15:15
  (07:50 = NXT 프리마켓 08:00 개장 전 당일 roster 확보 — 2026-06-12 19:00 대표 지시
   NXT 08:00~20:00 체제. 15:15 fire = 장중 신규 지정 대비 + 당일 픽 universe
   list_count 충전. 20시 마감 후 fire 는 의도적 미추가 — 당일 list_count 는 15:15 가
   충전 완료, 픽 universe 는 15:20 이후 불변, 애프터마켓 중 지정 변경은 차일 07:50
   스냅샷이 자연 수용. 무수요 수집 = FLR-AGT-002 정직성 원칙 위반 소지).

데이터 소스 — ka10099 종목정보 리스트 (spec = projects/pm320/poc/kiwoom_client.py:99-107
  + dev-probe-nxt-mcap 2026-06-12 16:52 probe + 본 스크립트 작성 직전 read-only 재실측
  2026-06-12 17:0x KST verbatim — FLR-20260408-TEC-001 외부 API 사전 검증):
  * POST /api/dostk/stkinfo, api-id=ka10099, body {"mrkt_tp": "0"(코스피)|"10"(코스닥)}
  * 응답 키 "list", cont-yn=N (단일 페이지, 연속조회 불요)
  * 필드: code(6자리), name, listCount(zero-padded 문자열 → int), nxtEnable("Y"/"N")
  * 실측: KOSPI 2465행/Y 358 + KOSDAQ 1823행/Y 283 = NXT 641종목
    (삼성전자 005930 Y·5,846,278,608주 / 고영 098460 N·68,654,755주)
  rate: 1 fire 당 2호출 — 부하 무시 수준.

산출: {M1S_HOMEPAGE}/pm320/data/nxt_roster.json — 날짜별 스냅샷 누적.
  스키마: {"fetched_at": KST ISO, "snapshots": {"YYYY-MM-DD":
    {"codes_nxt": [NXT Y 6자리 코드 정렬 배열],
     "list_count": {code: int — 그날 카드 universe 종목만}}}}
  조니 정직성 게이트 본질 = 시점 왜곡 금지: 과거 날짜 스냅샷은 보존만 (수정 0) —
  frontend 가 현재 roster 로 과거 카드를 칠하는 것을 데이터 구조가 차단.
  당일 스냅샷만 추가/교체 (멱등: 동일 날짜 재실행 + 내용 동일 → write 자체 skip = diff 0).

list_count universe (파일 비대 방지 — 전 종목 4288개 적재 금지, ~25-30/일):
  (A) 당일 픽 JSON (projects/pm320/data/daily/picks/{date}.json) 의
      picked/ranked/removed + orig1·orig2·new1·new2 코드 (일 ≤ ~12종목).
  (B) 거래대금 추이 카드 universe — kiwoom 일별 JSON ({M1S_HOMEPAGE}/data/kiwoom/{date}.json)
      latest_stocks[].ticker (일 ~21종목). frontend renderer._mcapMetaHtml 가 카드 가격 ×
      list_count 로 시총 표기 → 픽 외 카드 종목도 적재 (2026-06-14 시총 백필: 기존 (A)만
      적재해 카드 5/30 만 시총 표기되던 root = write-universe 필터 → (A)∪(B) 로 완화).
      신규 API 호출 0 — list_count_all(ka10099 전 종목) 에서 조인만.
  * 픽 JSON 은 15:20 생성 → 15:15 fire 는 15:23 까지 착지 대기 (push_pick_preview 동형,
    대기는 15:10~15:23 윈도우에서만 — 07:50 fire 는 무대기).
  * kiwoom 일별 JSON 은 장중 10분 간격 갱신 (first_snapshot ~10:35) → 15:15 fire 시 당일
    카드 universe 존재. 07:50 fire 시 당일분 부재 → 카드 코드 0 (과거 날짜 스냅샷이
    이미 보유 — 당일분은 15:15 fire 가 충전). 추정/이월 적재 금지 (FLR-AGT-002).

git 안전 (push_pick_preview.sync_push 동형 — FLR-20260519-TEC-001 / lead-meta §11.27):
  - pre-staged change 존재 시 add/commit SKIP (타 actor staged 보호)
  - add 는 본 스크립트 산출 1파일(pm320/data/nxt_roster.json) 화이트리스트만
  - pull --rebase --autostash → push HEAD:main (plain, force 금지), race 재시도 1회

env:
  M1S_HOMEPAGE   서빙 cron WT (기본 ~/company/100m1s-homepage-cron)
  KIWOOM_LIVE_APPKEY / KIWOOM_LIVE_SECRETKEY (.env — collect_kr_index_intraday 동형)

사용:
  python3 scripts/collect_nxt_roster.py [--no-sync]

exit: 0=PASS/no-op, 1=수집·write 실패, 2=git 실패, 3=push 실패
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# env 로드 — 메인 .env 단일 source (collect_kr_index_intraday 동형, shell export 우선)
# S5 자립화 (DOC-20260707-REQ-001): 메인 레포 절대경로 → env(M1S_COMPANY) 우선 + pm320 레포 로컬 fallback.
_M1S_COMPANY = Path(os.environ.get("M1S_COMPANY", str(Path(__file__).resolve().parents[1])))
MAIN_ENV = _M1S_COMPANY / ".env"
if MAIN_ENV.exists():
    for line in MAIN_ENV.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# 패키지 import 경로 보정 (단독 실행 시) — kiwoom_client 는 requests 만 의존
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.news_pipeline.kiwoom_client import get_token  # noqa: E402

KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL") or os.environ.get(
    "KIWOOM_BASE_URL", "https://api.kiwoom.com"
)
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
# S5 자립화 (DOC-20260707-REQ-001): pm320 레포에는 projects/ 부재 → 레포 로컬 data/daily/picks.
PICKS_DIR = REPO_ROOT / "data" / "daily" / "picks"
HOMEPAGE_DIR = Path(
    os.environ.get(
        "M1S_HOMEPAGE", str(Path.home() / "company" / "100m1s-homepage-cron")
    )
)
OUT_PATH = HOMEPAGE_DIR / "pm320" / "data" / "nxt_roster.json"

# 거래대금 추이(오늘의 종목) 카드 universe — build_daily 산출 kiwoom 일별 JSON.
# 카드의 시총 = 카드 가격 × list_count → 픽 universe 외 카드 종목도 list_count 적재 대상.
# 본 JSON 은 장중 10분 간격 갱신 (first_snapshot_at ~10:35) → 15:15 fire 시 당일분 존재,
# 07:50 fire 시 당일분 부재 (= 카드 코드 0, 과거 날짜는 자기 스냅샷 보유 — 추정 적재 금지).
KIWOOM_DAILY_DIR = HOMEPAGE_DIR / "data" / "kiwoom"

# 상한가(--lu) 카드 universe — build_daily 산출 interpreted 일별 JSON.
# renderer.js L2210-2227 의 union 정책: base(latest_stocks)에 없는 interpreted 종목 중
# status_badges.label == '상한가' 인 것을 카드로 append → 별도 _mcapMetaHtml 소비.
# 종래 card_universe_codes 가 latest_stocks 만 읽어 상한가 limit-up 9종 list_count 누락
# (R57 픽셀 재판정 NO 본질 — 상한가 카드 2/11 만 시총). frontend 와 동일 source·동일 필터.
KIWOOM_INTERPRETED_DIR = HOMEPAGE_DIR / "data" / "interpreted"

# (mrkt_tp, 라벨) — ka10099 시장 구분 (poc/kiwoom_client.get_stock_list 동일)
MARKETS = [("0", "KOSPI"), ("10", "KOSDAQ")]

# 픽 JSON 착지 대기 — push_pick_preview 동형 (15:20 생성, 15:15 fire 윈도우에서만 대기)
PICKS_WAIT_WINDOW = ((15, 10), (15, 23))  # [시작, deadline)
PICKS_WAIT_INTERVAL_SEC = 5

KST = ZoneInfo("Asia/Seoul")


def log(msg: str) -> None:
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"[{ts}] {msg}", flush=True)


def fetch_market_rows(token: str, mrkt_tp: str, label: str) -> list[dict]:
    """ka10099 1콜 — 시장 전 종목 행. 0건/스키마 이탈 시 RuntimeError (부분 적재 차단)."""
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10099",
    }
    last_err = "unknown"
    for attempt in range(3):
        try:
            r = requests.post(
                f"{KIWOOM_BASE}/api/dostk/stkinfo",
                json={"mrkt_tp": mrkt_tp},
                headers=headers,
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001 - 네트워크 일시 오류 재시도
            last_err = f"exception {e}"
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code == 429:
            last_err = "http 429"
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"ka10099 {label} http {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("return_code") != 0:
            raise RuntimeError(f"ka10099 {label} rc={data.get('return_code')}")
        rows = data.get("list") or []
        if not rows:
            raise RuntimeError(f"ka10099 {label} 응답 0건 — 적재 차단")
        return rows
    raise RuntimeError(f"ka10099 {label} 재시도 소진: {last_err}")


def collect_roster(token: str) -> tuple[list[str], dict[str, int]]:
    """전 시장 ka10099 → (codes_nxt 정렬 배열, 전 종목 code→listCount 맵)."""
    codes_nxt: set[str] = set()
    list_count_all: dict[str, int] = {}
    for mrkt_tp, label in MARKETS:
        rows = fetch_market_rows(token, mrkt_tp, label)
        n_y = 0
        for row in rows:
            # 신형 코드는 영문 포함 (실측: 0120G0 삼양바이오팜 등 NXT Y 4건) —
            # isdigit 필터 시 무음 탈락 = FLR-AGT-002 동형 결손. 6자리 영숫자 허용.
            code = str(row.get("code", "")).strip()
            if code.isdigit():
                code = code.zfill(6)
            if len(code) != 6 or not code.isalnum():
                continue
            if row.get("nxtEnable") == "Y":
                codes_nxt.add(code)
                n_y += 1
            lc_raw = str(row.get("listCount", "")).strip()
            if lc_raw.isdigit() and int(lc_raw) > 0:
                list_count_all[code] = int(lc_raw)
        log(f"{label}: rows={len(rows)} nxt_y={n_y}")
    if not codes_nxt:
        raise RuntimeError("nxtEnable Y 0건 — 스키마 변경 의심, 적재 차단")
    return sorted(codes_nxt), list_count_all


def load_today_picks(date_str: str) -> dict | None:
    """당일 픽 JSON. 15:10~15:23 윈도우에서만 착지 대기, 그 외 부재 = None."""
    fp = PICKS_DIR / f"{date_str}.json"
    now = datetime.now(KST)
    (w_h, w_m), (d_h, d_m) = PICKS_WAIT_WINDOW
    if (w_h, w_m) <= (now.hour, now.minute) < (d_h, d_m):
        deadline = now.replace(hour=d_h, minute=d_m, second=0, microsecond=0)
        while not fp.exists() and datetime.now(KST) < deadline:
            time.sleep(PICKS_WAIT_INTERVAL_SEC)
    if not fp.exists():
        log(f"INFO: picks 부재 ({fp.name}) — list_count {{}} (추정 적재 금지)")
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 손상 픽 = universe 없음으로 정직 처리
        log(f"WARN: picks parse 실패: {type(exc).__name__} — list_count {{}}")
        return None


def universe_codes(picks: dict) -> set[str]:
    """그날 카드 universe 코드 — picked/ranked/removed + 보유 N종(orig·new) 코드."""
    codes: set[str] = set()
    picked = picks.get("picked")
    if isinstance(picked, dict) and picked.get("code"):
        codes.add(str(picked["code"]))
    for key in ("ranked", "removed"):
        for row in picks.get(key) or []:
            if isinstance(row, dict) and row.get("code"):
                codes.add(str(row["code"]))
    for key in ("orig1_code", "orig2_code", "new1_code", "new2_code"):
        if picks.get(key):
            codes.add(str(picks[key]))
    return {c.strip().zfill(6) for c in codes if c.strip()}


def _limit_up_card_codes(date_str: str) -> set[str]:
    """상한가(--lu) 카드 universe 코드 — interpreted 일별 JSON stocks[].status_badges.

    renderer.js L2213 `_hasLimitUp = status_badges.some(b => b.label === '상한가')` verbatim
    동형. base(latest_stocks)에 없어도 union 으로 카드화되는 상한가 종목을 list_count
    universe 에 포함 (그 카드의 시총 = price × list_count). 파일 부재/손상 = 빈 set.
    """
    fp = KIWOOM_INTERPRETED_DIR / f"stock-{date_str}.json"
    if not fp.exists():
        return set()
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 손상 = universe 없음으로 정직 처리
        log(f"WARN: interpreted 일별 JSON parse 실패 ({fp.name}): {type(exc).__name__}")
        return set()
    codes: set[str] = set()
    for row in data.get("stocks") or []:
        if not isinstance(row, dict) or not row.get("code"):
            continue
        badges = row.get("status_badges") or []
        if any(isinstance(b, dict) and b.get("label") == "상한가" for b in badges):
            codes.add(str(row["code"]).strip().zfill(6))
    return codes


def card_universe_codes(date_str: str) -> set[str]:
    """전 표시 카드 universe 코드 — frontend renderer._mcapMetaHtml 소비 종목 합집합.

    (B1) 거래대금 추이 카드 — kiwoom 일별 JSON latest_stocks[].ticker (renderer base).
    (B2) 상한가(--lu) 카드 — interpreted 일별 JSON stocks[] 중 status_badges.label=='상한가'
         (renderer union append, base 미포함분). R57 픽셀 재판정 NO 본질 = B2 누락.
    일반픽(--idx)·당일픽 카드는 모두 base(latest_stocks) 또는 픽 universe 에 이미 포함되어
    별도 source 불요 (renderer 가 카드를 base+상한가union 으로만 구성, interpreted-only 비상한가
    종목은 카드 렌더 대상 아님 — L2210-2227 검증).
    파일 부재(07:50 fire·미생성) 또는 손상 = 빈 set (추정 적재 0 — FLR-AGT-002).
    """
    fp = KIWOOM_DAILY_DIR / f"{date_str}.json"
    codes: set[str] = set()
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            rows = data.get("latest_stocks") or data.get("daily_top") or []
            for row in rows:
                if isinstance(row, dict):
                    t = row.get("ticker") or row.get("code")
                    if t and str(t).strip():
                        codes.add(str(t).strip().zfill(6))
        except Exception as exc:  # noqa: BLE001 - 손상 = universe 없음으로 정직 처리
            log(f"WARN: kiwoom 일별 JSON parse 실패 ({fp.name}): {type(exc).__name__}")
    # (B2) 상한가 카드 union — interpreted JSON (base 미포함 limit-up 종목 list_count 충전)
    return codes | _limit_up_card_codes(date_str)


def _build_list_count(
    list_count_all: dict[str, int],
    pick_codes: set[str],
    card_codes: set[str],
    date_str: str,
) -> dict[str, int]:
    """pick_codes ∪ card_codes 를 list_count_all 에서 조인 — 추정 적재 0."""
    list_count: dict[str, int] = {}
    for code in sorted(pick_codes | card_codes):
        if code in list_count_all:
            list_count[code] = list_count_all[code]
        elif code in pick_codes:
            log(f"WARN: 픽 universe 코드 {code} ka10099 부재 — 누락 (fabrication 금지)")
        else:
            log(f"INFO: 카드 universe 코드 {code} ka10099 부재 — 시총 무표기")
    log(
        f"SNAPSHOT {date_str}: list_count={len(list_count)} "
        f"(pick={len(pick_codes)} card={len(card_codes)})"
    )
    return list_count


def build_day_snapshot_phase1(
    date_str: str,
) -> tuple[list[str], dict[str, int], dict[str, int]]:
    """Phase 1 (선 배포): ka10099 수집 + card_universe list_count 즉시 산출.

    픽 대기 없이 15:15:xx 에 완료 가능. 반환:
      (codes_nxt, list_count_all, list_count_phase1)
    list_count_phase1 = card_universe 기반 — 픽 universe 미포함.
    픽 종목이 card_universe 에 이미 포함된 경우(대부분) 15:20 픽 공개 즉시 시총 계산 가능.
    """
    token = get_token(
        KIWOOM_BASE,
        KIWOOM_APPKEY,
        KIWOOM_SECRETKEY,
        key_label="KIWOOM_LIVE_APPKEY/SECRETKEY",
    )
    codes_nxt, list_count_all = collect_roster(token)
    card_codes = card_universe_codes(date_str)
    list_count_p1 = _build_list_count(list_count_all, set(), card_codes, date_str)
    log(
        f"PHASE1 {date_str}: codes_nxt={len(codes_nxt)} "
        f"list_count_p1={len(list_count_p1)} card_codes={len(card_codes)}"
    )
    return codes_nxt, list_count_all, list_count_p1


def build_day_snapshot_phase2(
    date_str: str,
    codes_nxt: list[str],
    list_count_all: dict[str, int],
) -> dict:
    """Phase 2 (픽 착지 후): pick_codes 추가하여 최종 스냅샷 산출.

    픽 대기(PICKS_WAIT_WINDOW) 포함. ka10099 재호출 0 (list_count_all 재사용).
    픽 universe 에서 card_universe 에 없는 종목의 list_count 를 추가 충전.
    """
    pick_codes = (
        universe_codes(picks) if (picks := load_today_picks(date_str)) else set()
    )
    card_codes = card_universe_codes(date_str)
    list_count = _build_list_count(list_count_all, pick_codes, card_codes, date_str)
    log(
        f"PHASE2 {date_str}: codes_nxt={len(codes_nxt)} "
        f"list_count={len(list_count)} (pick={len(pick_codes)} card={len(card_codes)})"
    )
    return {"codes_nxt": codes_nxt, "list_count": list_count}


def build_day_snapshot(date_str: str) -> dict:
    """당일 스냅샷 {codes_nxt, list_count(픽 ∪ 거래대금 카드 universe 한정)}.

    백필(--date) 등 단일 호출용 래퍼 — Phase 1+2 를 순차 실행.
    운영 15:15 fire 는 main() 에서 Phase 1 즉시 배포 → Phase 2 픽 대기 재배포.
    """
    codes_nxt, list_count_all, _ = build_day_snapshot_phase1(date_str)
    return build_day_snapshot_phase2(date_str, codes_nxt, list_count_all)


def load_existing() -> dict:
    try:
        data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("snapshots"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - 손상 파일 = 신규 시작 (스냅샷 유실 로그)
        log(f"WARN: 기존 파일 parse 실패 {type(exc).__name__} — 신규 생성")
    return {"fetched_at": None, "snapshots": {}}


def write_atomic(payload: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(OUT_PATH)


def _git(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    # 고정 인자 리스트만 전달 (untrusted input 0) + PATH 는 launchd plist 가 통제
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=str(HOMEPAGE_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def sync_push(date_str: str) -> int:
    """산출 1파일 화이트리스트 add → commit → rebase+push (push_pick_preview 동형)."""
    if not (HOMEPAGE_DIR / ".git").exists():
        log(f"SYNC FAIL: not a git repo: {HOMEPAGE_DIR}")
        return 2
    rel = str(OUT_PATH.relative_to(HOMEPAGE_DIR))

    # 타 actor staged 보호 (lead-meta §11.27) — pre-staged 존재 시 본 fire SKIP
    pre = _git(["diff", "--cached", "--name-only"], timeout=30)
    if pre.returncode != 0:
        log(f"SYNC FAIL (git diff): rc={pre.returncode}")
        return 2
    if pre.stdout.strip():
        log(
            f"SYNC SKIP: pre-existing staged changes "
            f"({len(pre.stdout.splitlines())} files) — 다음 fire 재시도"
        )
        return 0

    status = _git(["status", "--porcelain", "--", rel], timeout=30)
    if status.returncode != 0:
        log(f"SYNC FAIL (git status): rc={status.returncode}")
        return 2
    if not status.stdout.strip():
        log("SYNC SKIP: no change (산출 동일)")
        return 0

    if _git(["add", rel], timeout=30).returncode != 0:
        log("SYNC FAIL (git add)")
        return 2
    msg = f"data(pm320,nxt,{date_str}): NXT roster+상장주식수 스냅샷 (collect_nxt_roster.py)"
    if _git(["commit", "-m", msg], timeout=60).returncode != 0:
        log("SYNC FAIL (git commit)")
        return 2
    log(f"SYNC: commit done ({rel})")

    # rebase + plain push, race 시 재시도 1회 (force 금지)
    # fetch + rebase 2단계 분리: cron WT upstream(cron-isolation)과 origin/main 모호성 제거
    # (pull --rebase origin main 는 upstream 추적 브랜치가 별도 설정된 경우
    #  "Cannot rebase onto multiple branches" fatal 발생 — 2026-06-29 근본 수정)
    for attempt in (1, 2):
        fetch = _git(["fetch", "origin", "main"], timeout=30)
        if fetch.returncode != 0:
            log(f"SYNC FAIL (fetch, attempt {attempt}): {fetch.stderr.strip()[:200]}")
            if attempt == 2:
                return 3
            time.sleep(3)
            continue
        pull = _git(["rebase", "--autostash", "origin/main"])
        if pull.returncode != 0:
            log(f"SYNC FAIL (rebase, attempt {attempt}): {pull.stderr.strip()[:200]}")
            if attempt == 2:
                return 3
            time.sleep(3)
            continue
        push = _git(["push", "origin", "HEAD:main"])
        if push.returncode == 0:
            log(
                f"SYNC: push done → origin main (nxt_roster {date_str}, attempt {attempt})"
            )
            return 0
        log(
            f"SYNC WARN (push rejected, attempt {attempt}): {push.stderr.strip()[:200]}"
        )
        if attempt == 2:
            return 3
        time.sleep(3)
    return 3


def main() -> int:
    ap = argparse.ArgumentParser(
        description="NXT roster + 상장주식수 일일 수집 → nxt_roster.json"
    )
    ap.add_argument("--no-sync", action="store_true", help="git add/commit/push skip")
    ap.add_argument(
        "--date",
        default=None,
        help=(
            "YYYY-MM-DD 백필 대상 날짜 override (운영 backfill 전용). 지정 시 주말 가드·"
            "picks 착지 대기 무시 — 해당 날짜 일별 JSON(kiwoom/interpreted/picks)이 이미 "
            "존재해야 함. live ka10099 의 listCount 로 과거 날짜 스냅샷 충전 (상장주식수는 "
            "준-불변 — 백필 시점 값 사용). 미지정 시 당일 (launchd 정상 경로)."
        ),
    )
    args = ap.parse_args()

    now = datetime.now(KST)
    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            log(f"FAIL: --date 형식 오류 ({args.date}) — YYYY-MM-DD 필요")
            return 1
        date_str = args.date
        log(f"BEGIN collect_nxt_roster date={date_str} (backfill override)")
    else:
        if now.weekday() >= 5:  # launchd 평일 fire — 주말 가드 (belt)
            log("SKIP: weekend")
            return 0
        date_str = now.strftime("%Y-%m-%d")
        log(f"BEGIN collect_nxt_roster date={date_str}")

    no_sync = args.no_sync or os.environ.get("M1S_NXT_ROSTER_NO_SYNC") == "1"

    # ── 백필 경로(--date): Phase 1+2 단일 실행 (픽 이미 존재 가정) ──────────────
    if args.date:
        try:
            day_snapshot = build_day_snapshot(date_str)
        except Exception as exc:  # noqa: BLE001 - 수집 실패 = 기존 스냅샷 보존 종료
            log(f"FAIL collect: {type(exc).__name__}: {exc}")
            return 1
        existing = load_existing()
        snapshots = existing["snapshots"]
        if snapshots.get(date_str) == day_snapshot:
            log("SKIP: 당일 스냅샷 동일 — write 생략 (멱등)")
        else:
            snapshots[date_str] = day_snapshot
            payload = {
                "fetched_at": now.isoformat(timespec="seconds"),
                "snapshots": snapshots,
            }
            try:
                write_atomic(payload)
                log(f"WRITE: {OUT_PATH} (days={len(snapshots)})")
            except Exception as exc:  # noqa: BLE001
                log(f"FAIL write: {type(exc).__name__}: {exc}")
                return 1
        if no_sync:
            log("SYNC SKIP: no-sync 지정")
            return 0
        return sync_push(date_str)

    # ── 운영 경로: Phase 1 선 배포 → Phase 2 픽 착지 후 재배포 ─────────────────
    # Phase 1: ka10099 수집 + card_universe list_count 즉시 산출 (픽 대기 0)
    # 목적: 15:20 픽 공개 시점에 card_universe list_count 가 이미 배포되어
    #       renderer._mcapMetaHtml 가 픽 종목 시총을 즉시 계산 가능.
    # 픽 종목이 card_universe(거래대금 상위 ~21종) 에 포함된 경우 — 대부분 해당 —
    # Phase 1 배포만으로 15:20 정각 시총 노출 충족.
    (w_h, w_m), _ = PICKS_WAIT_WINDOW
    in_picks_window = (now.hour, now.minute) >= (w_h, w_m)

    try:
        codes_nxt, list_count_all, list_count_p1 = build_day_snapshot_phase1(date_str)
    except Exception as exc:  # noqa: BLE001 - 수집 실패 = 기존 스냅샷 보존 종료
        log(f"FAIL phase1 collect: {type(exc).__name__}: {exc}")
        return 1

    snapshot_p1 = {"codes_nxt": codes_nxt, "list_count": list_count_p1}
    existing = load_existing()
    snapshots = existing["snapshots"]

    if in_picks_window and snapshots.get(date_str) != snapshot_p1:
        # Phase 1 선 배포 — 픽 착지 대기 전에 card_universe list_count 먼저 push
        snapshots[date_str] = snapshot_p1
        payload = {
            "fetched_at": now.isoformat(timespec="seconds"),
            "snapshots": snapshots,
        }
        try:
            write_atomic(payload)
            log(f"WRITE phase1: {OUT_PATH} (list_count_p1={len(list_count_p1)})")
        except Exception as exc:  # noqa: BLE001
            log(f"FAIL write phase1: {type(exc).__name__}: {exc}")
            return 1
        if not no_sync:
            rc = sync_push(date_str)
            if rc not in (0, 3):  # push 실패(rc=3)는 phase2 에서 재시도 가능 — 계속
                return rc
            log(f"PHASE1 push rc={rc} (phase2 계속)")
    elif not in_picks_window:
        # 15:10 전 (07:50 fire 등): 선 배포 없이 단일 실행
        log(f"PHASE1 SKIP: picks 윈도우 외 (now={now.hour:02d}:{now.minute:02d})")

    # Phase 2: 픽 착지 대기 → pick_codes 추가 최종 스냅샷
    try:
        day_snapshot = build_day_snapshot_phase2(date_str, codes_nxt, list_count_all)
    except Exception as exc:  # noqa: BLE001
        log(f"FAIL phase2 collect: {type(exc).__name__}: {exc}")
        return 1

    # 기존 파일 재로드 (phase1 write 이후 상태 반영)
    existing2 = load_existing()
    snapshots2 = existing2["snapshots"]
    if snapshots2.get(date_str) == day_snapshot:
        log("SKIP phase2: 당일 스냅샷 동일 — write 생략 (멱등)")
    else:
        snapshots2[date_str] = day_snapshot
        payload2 = {
            "fetched_at": now.isoformat(timespec="seconds"),
            "snapshots": snapshots2,
        }
        try:
            write_atomic(payload2)
            log(f"WRITE phase2: {OUT_PATH} (days={len(snapshots2)})")
        except Exception as exc:  # noqa: BLE001
            log(f"FAIL write phase2: {type(exc).__name__}: {exc}")
            return 1

    if no_sync:
        log("SYNC SKIP: no-sync 지정")
        return 0
    return sync_push(date_str)


if __name__ == "__main__":
    sys.exit(main())
