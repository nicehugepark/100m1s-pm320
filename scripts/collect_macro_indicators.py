#!/usr/bin/env python3
"""매크로 지표 수집 → macro_indicators.json (글로벌 지표 구획 데이터 레이어).

대상 3종 — 원/달러(KRW=X, Yahoo CCY 24h) + WTI 선물(CL=F, NYMEX Globex)
  + 미10년물 금리(^TNX, CBOE 10-Year Treasury Yield Index).
  ※ 미10년물은 종전 국채선물(ZN=F, 역방향 가격)을 조니 단정(2026-06-12 17:06)으로
    제외했으나, 대표 12:50(2026-06-13) 승인으로 **선물 대신 금리(yield) 직접** 되살림
    (Q-20260613-165). ^TNX 는 가격이 아닌 yield(%) 그 자체 → "가격↑=금리↓" 역방향
    결함 소멸 (값 = 4.23 = 4.23%, 직관적). 선물(ZN=F) 은 여전히 미수집.
    delta 는 % 아닌 **bp(베이시스포인트)** = (yield - prev) × 100. 금리↑=악재(빨강).

스케줄 = NXT 장시간 체제 08:00~20:00 (2026-06-12 19:00 대표 지시): launchd 평일
08:07~19:52 15분 간격 + 20:07 final. KRW=X(FX 24h)·CL=F(Globex)는 시간대 무관 갱신
— probe 실측 17:03(장외) + 19:1x(애프터마켓) bar_asof 수분 내 재확인.

probe 실측 (dev-probe-macro3 2026-06-12 17:03 채택): 두 심볼 모두 한국 장중 실갱신
(마지막 봉 0~10.6분 전). 🔴 Yahoo 429 실측 (burst 5호출 전건 429 — 기존
us-intraday cron 과 IP rate limit 공유) → 봉쇄 3축:
  ① Yahoo 호출 코드 신작 금지 — collect_us_futures.fetch_chart(429 백오프 2/4/8s)
     import 재사용 (FLR-20260406-TEC-001 공통화 누락 recurring 차단, 원본 수정 0줄)
  ② 심볼 간 sleep 10s (SYMBOL_GAP_SECONDS)
  ③ launchd offset 분 = 7,22,37,52 — us-intraday(0,10,..,50) 와 kr-index-intraday
     (5,15,..,55) 양 grid 모두 비충돌. lead 예시(5,20,35,50)는 us-intraday 가
     15분→10분 전환(Q-20260608-139) 전 가정이라 :20/:50 충돌 → 의도(동시 burst
     회피) 우선 적용.

산출: {M1S_HOMEPAGE}/pm320/data/macro_indicators.json — 원자 덮어쓰기(tmp→rename).
  스키마: {"as_of": KST ISO, "indicators": {
    "usdkrw": {"label": "원/달러", "value", "prev_close"(meta.chartPreviousClose),
               "change_pct", "bar_asof"(KST ISO — frontend 60분 stale 가드용)},
    "wti":    {"label": "WTI 선물", ...동일 필드...},
    "ust10y": {"label": "미10년물 금리", "value"(yield %), "prev_close",
               "change_bp"((value-prev)×100), "bar_asof"}}}
  ※ ust10y 만 change_pct 대신 **change_bp** (금리 항목 — bp 가 관습 단위). usdkrw·wti 는
    change_pct 유지. (frontend 가 키 존재로 분기 — 한 항목에 둘 다 싣지 않음, 모호성 0).
  항목별 독립 — 1심볼 실패 시 해당 항목 생략(부분 산출). 전 심볼 실패 시에만
  write 생략 + exit 1 (기존 serving json 보존, FLR-AGT-002 거짓 충실성 차단).

git push (collect_kr_index_intraday.sync_push 동형 — lead 사전 명시 승인 범위):
  - add 는 macro_indicators.json **한 파일 화이트리스트만** (무차별 glob 금지)
  - pre-staged change 존재 시 add/commit SKIP (타 actor 보호, lead-meta §11.27)
  - pull --rebase --autostash → push HEAD:main (plain, force/--no-verify 금지)

env: M1S_HOMEPAGE 필수 (fallback 폐기 — 경로 divergence 봉쇄).
사용:
  M1S_HOMEPAGE=~/company/100m1s-homepage-cron \
    python3 scripts/collect_macro_indicators.py [--final] [--no-sync]
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

# 패키지 import 경로 보정 (단독 실행 시) — collect_kr_index_intraday.py:72 동형
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 🔴 read-only import 만 — collect_us_futures.py 수정 0줄 (모듈 레벨 부작용 0 확인:
# 상수 + 함수 정의만, __main__ 가드 존재)
from scripts.news_pipeline.collect_us_futures import fetch_chart  # noqa: E402

# M1S_HOMEPAGE 필수 — collect_kr_index_intraday.py:85 동형
_HOMEPAGE_ENV = os.environ.get("M1S_HOMEPAGE")
if not _HOMEPAGE_ENV:
    raise RuntimeError(
        "M1S_HOMEPAGE 환경변수 필수 — cron: ~/company/100m1s-homepage-cron, "
        "ad-hoc: 대상 worktree 명시"
    )
HOMEPAGE_DIR = Path(_HOMEPAGE_ENV)
OUT_PATH = HOMEPAGE_DIR / "pm320" / "data" / "macro_indicators.json"

# (json key, 표시 label, Yahoo 심볼, sanity range, delta_kind) — 스케일/심볼 오인 탐지
# (collect_us_futures.SANITY_RANGE 정책 동형, FLR-20260406-TEC-001).
# delta_kind: "pct" = change_pct(% 등락), "bp" = change_bp((value-prev)×100, 금리 항목).
#   ust10y 만 bp — ^TNX value 는 yield(%) 그 자체라 일간 변화 = bp 가 관습(스케일 가드도
#   42.3(잘못된 ×10 스케일) vs 4.23(정상) 탐지 위해 sanity (0.5, 8.0) 좁게: 2026-06 금리 ~4.5%).
MACRO_TARGETS = [
    ("usdkrw", "원/달러", "KRW=X", (800.0, 2500.0), "pct"),
    ("wti", "WTI 선물", "CL=F", (20.0, 250.0), "pct"),
    ("ust10y", "미10년물 금리", "^TNX", (0.5, 8.0), "bp"),
]

# 심볼 간 호출 간격 (초) — Yahoo IP rate limit 공유 (probe burst 5호출 전건 429 실측)
SYMBOL_GAP_SECONDS = 10

KST = ZoneInfo("Asia/Seoul")


def log(msg: str) -> None:
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"[{ts}] {msg}", flush=True)


def build_indicator(
    label: str, symbol: str, sanity: tuple, delta_kind: str = "pct"
) -> dict | None:
    """단일 심볼 → 지표 entry. 실패 시 None (항목별 독립 — 호출측이 생략).

    delta_kind="pct" → change_pct((value/prev-1)×100), "bp" → change_bp((value-prev)×100,
    금리 항목). value/prev/sanity/bar_asof 산출 로직은 공통 (yield 도 동일 fetch_chart
    재사용 — Yahoo 호출 신작 0줄, FLR-20260406-TEC-001 공통화).
    """
    raw = fetch_chart(symbol)  # 429 백오프 2/4/8s 내장 (재시도 소진 시 None)
    if raw is None:
        log(f"FAIL {symbol}: fetch_chart None (재시도 소진)")
        return None
    try:
        result = raw["chart"]["result"][0]
        meta = result["meta"]
    except (KeyError, IndexError, TypeError):
        log(f"FAIL {symbol}: chart.result 구조 이상")
        return None
    value = meta.get("regularMarketPrice")
    prev_close = meta.get("chartPreviousClose")
    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote") or [{}]
    closes = quotes[0].get("close") or []
    # 마지막 비-null 봉 → bar_asof (frontend 60분 stale 가드 기준점)
    bar_ts = None
    for i in range(min(len(timestamps), len(closes)) - 1, -1, -1):
        if closes[i] is not None:
            bar_ts = timestamps[i]
            if value is None:
                value = closes[i]  # meta 가격 부재 시 마지막 봉 종가 fallback
            break
    if value is None or bar_ts is None:
        log(f"FAIL {symbol}: 가격/봉 부재 (value={value}, bar_ts={bar_ts})")
        return None
    value = round(float(value), 2)
    lo, hi = sanity
    if not (lo <= value <= hi):
        log(f"FAIL {symbol}: sanity 위반 value={value} not in [{lo},{hi}]")
        return None
    prev = round(float(prev_close), 2) if prev_close is not None else None
    entry = {
        "label": label,
        "value": value,
        "prev_close": prev,
        "bar_asof": datetime.fromtimestamp(bar_ts, KST).isoformat(timespec="seconds"),
    }
    if delta_kind == "bp":
        # 금리 항목 — 일간 변화는 bp(베이시스포인트) = (yield - prev) × 100. round 1자리
        # (예 +2.4bp). prev 부재 시 None (frontend graceful — 빈 칸 색칠 금지).
        entry["change_bp"] = round((value - prev) * 100.0, 1) if prev else None
    else:
        entry["change_pct"] = round((value / prev - 1.0) * 100.0, 2) if prev else None
    return entry


def payload_unchanged(new: dict) -> bool:
    """기존 파일 대비 indicators 동일 여부 — 휴장 공회전 commit 방지."""
    try:
        old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 최초 실행/손상 → 변경으로 간주
        return False
    return old.get("indicators") == new["indicators"]


def write_atomic(payload: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    tmp.replace(OUT_PATH)


def _git(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    # 고정 인자 리스트만 전달 (untrusted input 0) + PATH 는 launchd plist 가 통제
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=HOMEPAGE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def sync_push(date_iso: str) -> int:
    """산출 1파일 화이트리스트 add → commit → rebase+push (kr_index sync_push 동형)."""
    rel = str(OUT_PATH.relative_to(HOMEPAGE_DIR))
    if not (HOMEPAGE_DIR / ".git").exists():
        log(f"SYNC FAIL: not a git repo: {HOMEPAGE_DIR}")
        return 2

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
    msg = f"data(macro,{date_iso}): 매크로 지표 갱신 (collect_macro_indicators.py)"
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
            return 3
        pull = _git(["rebase", "--autostash", "origin/main"])
        if pull.returncode != 0:
            log(f"SYNC FAIL (rebase, attempt {attempt}): {pull.stderr.strip()[:200]}")
            return 3
        push = _git(["push", "origin", "HEAD:main"])
        if push.returncode == 0:
            log(f"SYNC: push done → origin main (macro {date_iso}, attempt {attempt})")
            return 0
        log(
            f"SYNC WARN (push rejected, attempt {attempt}): {push.stderr.strip()[:200]}"
        )
    return 3


def main() -> int:
    ap = argparse.ArgumentParser(
        description="매크로 지표 2종 수집 → macro_indicators.json"
    )
    ap.add_argument(
        "--final", action="store_true", help="20:07 final fire 라벨 (동작 동일)"
    )
    ap.add_argument("--no-sync", action="store_true", help="git add/commit/push skip")
    args = ap.parse_args()

    now = datetime.now(KST)
    if now.weekday() >= 5:  # launchd 매일 fire — 주말 가드 (kr_index 동형)
        log("SKIP: weekend")
        return 0
    mode = "final" if args.final else "intraday"
    log(f"BEGIN collect_macro_indicators mode={mode} targets={len(MACRO_TARGETS)}")

    indicators: dict[str, dict] = {}
    for i, (key, label, symbol, sanity, delta_kind) in enumerate(MACRO_TARGETS):
        if i > 0:
            time.sleep(SYMBOL_GAP_SECONDS)  # Yahoo IP 한도 공유 — 심볼 간 간격 의무
        entry = build_indicator(label, symbol, sanity, delta_kind)
        if entry is None:
            log(f"OMIT {key} ({symbol}): 수집 실패 — 항목 생략 (부분 산출)")
            continue
        indicators[key] = entry
        delta = (
            f"chg={entry['change_pct']}%"
            if "change_pct" in entry
            else f"chg={entry['change_bp']}bp"
        )
        log(
            f"{key}: value={entry['value']} prev={entry['prev_close']} "
            f"{delta} bar_asof={entry['bar_asof']}"
        )

    if not indicators:
        log("FAIL: 전 심볼 수집 실패 — 기존 serving json 보존, write 생략")
        return 1

    payload = {
        "as_of": datetime.now(KST).isoformat(timespec="seconds"),
        "indicators": indicators,
    }
    if payload_unchanged(payload):
        log("SKIP: indicators unchanged (휴장 등) — write·push 생략")
        return 0
    try:
        write_atomic(payload)
        log(f"WRITE: {OUT_PATH} ({len(indicators)}/{len(MACRO_TARGETS)} 심볼)")
    except Exception as exc:  # noqa: BLE001
        log(f"FAIL write: {type(exc).__name__}: {exc}")
        return 1

    if args.no_sync or os.environ.get("M1S_MACRO_NO_SYNC") == "1":
        log("SYNC SKIP: no-sync 지정")
        return 0
    return sync_push(payload["as_of"][:10])


if __name__ == "__main__":
    sys.exit(main())
