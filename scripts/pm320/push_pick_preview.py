#!/usr/bin/env python3
"""PM320 15:20 경량 픽 선공개(preview) push — 대표 결정 2026-06-12 15:40 집행.

대표 verbatim: "나는 3시 20분에 딱 추천을 하고 싶은거야."
  본 데이터(카드) 자연 착지 = 15:35~40 (build_card_history cascade) → PM320 이름과 모순.
  본 스크립트가 15:20:50 에 STEP1 산출 picks JSON 만으로 경량 레코드를 만들어
  서빙 레포(data/pm320_history/preview/{date}.json)로 push → 15:21 내 화면 노출.
  전략·백테스트 무영향 (judge window 불변) — 상세 카드는 기존 15:35 경로가 자동 보강.

입력: projects/pm320/data/daily/picks/{date}.json (select_daily_pick STEP1 산출)
출력: {HOMEPAGE}/data/pm320_history/preview/{date}.json + commit/push (자기 산출물 단독 add)

가격 SSOT: dailybars close (15:20 시점 스냅샷 = select_daily_pick 판정과 동일 DB row).
  익절/물타기 비율 = 비가변 코드 상수 (+3.2% / -6.4%) — strategy_profiles/profiles.json
  _doc verbatim "비가변 공통 룰(... 익절 +3.2% / 물타기 -6.4%)은 코드 상수".
  build_card_history.py L163-164 동일 값. profiles.json 은 active profile id 정합
  확인용으로만 read (실패 시 무시, non-fatal).

가드 (FLR-AGT-002 거짓 충실성 차단):
  - picks 파일 부재 (주말·휴장·미산출) → 무동작 종료 exit 0
  - picked 부재 (보류일) → preview 미발행 exit 0 (추정 픽 금지)
  - dailybars close 부재 → preview 미발행 exit 0 (가격 fabrication 금지)

git 안전 (FLR-20260519-TEC-001 / lead-meta §11.27 — cron 자기 산출물 화이트리스트):
  - pre-staged change 존재 시 add/commit SKIP (타 actor staged 보호)
  - add 는 본 스크립트 산출 파일 1개 경로만 (무차별 glob 금지)
  - push: pull --rebase --autostash origin main → push HEAD:main (plain, force 금지)
    race(push 거절) 시 재시도 1회 (build_card_history.sync_to_homepage_main 동형)

env:
  M1S_HOMEPAGE       서빙 레포 (기본 ~/company/100m1s-homepage-cron)
  M1S_PREVIEW_NO_SYNC=1   git add/commit/push 전체 skip (dry-run)
  M1S_PREVIEW_NO_WAIT=1   15:20:50 대기 + picks 대기 루프 skip (dry-run/수동)

exit: 0=PASS/no-op, 1=write 실패, 2=git 실패, 3=push 실패
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parents[2]
# S5 자립화 (DOC-20260707-REQ-001): pm320 레포에는 projects/ 부재 → 레포 로컬 data/daily/picks.
PICKS_DIR = REPO_ROOT / "data" / "daily" / "picks"
HOMEPAGE_DIR = Path(
    os.environ.get(
        "M1S_HOMEPAGE", str(Path.home() / "company" / "100m1s-homepage-cron")
    )
)
STOCKS_DB = HOMEPAGE_DIR / "data" / "stocks.db"
PROFILES_JSON = REPO_ROOT / "scripts" / "pm320" / "strategy_profiles" / "profiles.json"

# 비가변 공통 상수 — build_card_history.py L163-164 verbatim (익절 +3.2% / 물타기 -6.4%)
WATERING_RATIO = 0.936
TAKE_PROFIT_RATIO = 1.032

FIRE_HMS = (15, 20, 50)  # launchd 15:20 fire 후 본 시각까지 자기 대기
PICKS_WAIT_DEADLINE_HMS = (
    15,
    23,
    0,
)  # picks 착지 대기 상한 (이후 부재 = 휴장/미산출 간주)
PICKS_WAIT_INTERVAL_SEC = 5


def log(msg: str) -> None:
    ts = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"[{ts}] {msg}", flush=True)


def _today_kst() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d")


def _wait_until(hms: tuple[int, int, int]) -> None:
    """오늘 hms(KST) 까지 대기. 이미 지났으면 즉시 반환 (launchd 지연 fire 안전)."""
    now = dt.datetime.now(KST)
    target = now.replace(hour=hms[0], minute=hms[1], second=hms[2], microsecond=0)
    diff = (target - now).total_seconds()
    if diff > 0:
        log(f"WAIT: {diff:.1f}s → {target.strftime('%H:%M:%S')} KST")
        time.sleep(diff)


def load_picks_with_wait(date_str: str, no_wait: bool) -> dict[str, Any] | None:
    """picks JSON read. fire 윈도우에서는 착지 대기 (상한 15:23), 부재 시 None."""
    fp = PICKS_DIR / f"{date_str}.json"
    if no_wait:
        if not fp.exists():
            log(f"INFO: picks not found: {fp.name} (no-wait, graceful exit)")
            return None
    else:
        deadline = dt.datetime.now(KST).replace(
            hour=PICKS_WAIT_DEADLINE_HMS[0],
            minute=PICKS_WAIT_DEADLINE_HMS[1],
            second=PICKS_WAIT_DEADLINE_HMS[2],
            microsecond=0,
        )
        while not fp.exists():
            if dt.datetime.now(KST) >= deadline:
                log(
                    f"INFO: picks not found by deadline: {fp.name} (휴장/주말/미산출 — 무동작 종료)"
                )
                return None
            time.sleep(PICKS_WAIT_INTERVAL_SEC)
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"FAIL: picks parse: {type(exc).__name__}")
        return None


def load_close_price(code: str, date_str: str) -> int | None:
    """dailybars close (read-only URI) — send_kakao_message.load_close_price 동형."""
    if not STOCKS_DB.exists():
        log(f"WARN: stocks.db not found: {STOCKS_DB}")
        return None
    try:
        conn = sqlite3.connect(f"file:{STOCKS_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT close FROM dailybars WHERE code=? AND date=?",
                (code, date_str),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        log(f"FAIL: stocks.db query: {type(exc).__name__}")
        return None
    if row is None or row[0] is None:
        log(f"WARN: dailybars row missing: code={code} date={date_str}")
        return None
    return int(row[0])


def check_profile_consistency(picks_profile_id: str | None) -> None:
    """profiles.json active vs picks profile_id 정합 로그 (non-fatal, 비율은 코드 상수)."""
    try:
        profiles = json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
        active = profiles.get("active")
        if picks_profile_id and active and picks_profile_id != active:
            log(
                f"WARN: profile mismatch picks={picks_profile_id} active={active} (비율은 비가변 상수, 영향 0)"
            )
        else:
            log(f"OK: profile={active or picks_profile_id or 'unknown'}")
    except Exception as exc:
        log(f"INFO: profiles.json read skip: {type(exc).__name__} (비가변 상수 사용)")


def build_preview_record(picks: dict[str, Any], date_str: str) -> dict[str, Any] | None:
    """picks → 경량 preview 레코드. 보류/가격 부재 시 None (발행 금지)."""
    picked = picks.get("picked")
    if not isinstance(picked, dict) or not picked.get("code"):
        log(
            f"INFO: no picked (보류일, branch={picks.get('branch')!r}) — preview 미발행"
        )
        return None
    code = str(picked["code"])
    name = str(picked.get("name") or "")
    entry = load_close_price(code, date_str)
    if entry is None or entry <= 0:
        log("INFO: entry price unavailable — preview 미발행 (fabrication 금지)")
        return None
    return {
        "schema": "pm320-pick-preview/v1",
        "preview": True,
        "date": date_str,
        "code": code,
        "name": name,
        "entry_price": entry,
        "take_profit_target_price": round(entry * TAKE_PROFIT_RATIO),
        "watering_target_price": round(entry * WATERING_RATIO),
        "profile_id": picks.get("profile_id"),
        "note": "15:20 선공개 — 상세 분석 카드는 15:35경 본 데이터로 자동 갱신",
        "generated_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
    }


def write_preview(record: dict[str, Any], date_str: str) -> Path | None:
    """preview JSON 원자 write (tmp → replace)."""
    target_dir = HOMEPAGE_DIR / "data" / "pm320_history" / "preview"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{date_str}.json"
        tmp = target_dir / f".{date_str}.json.tmp"
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, target)
        log(f"WRITE: {target}")
        return target
    except Exception as exc:
        log(f"FAIL: write: {type(exc).__name__}: {exc}")
        return None


def _git(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(HOMEPAGE_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def sync_push(target: Path, date_str: str) -> int:
    """자기 산출물 1파일 add → commit → rebase+push (race 재시도 1회)."""
    if os.environ.get("M1S_PREVIEW_NO_SYNC") == "1":
        log("SYNC SKIP: M1S_PREVIEW_NO_SYNC=1 (dry-run)")
        return 0
    if not (HOMEPAGE_DIR / ".git").exists():
        log(f"SYNC SKIP: homepage repo absent ({HOMEPAGE_DIR})")
        return 0

    rel = str(target.relative_to(HOMEPAGE_DIR))

    # 타 actor staged 보호 (lead-meta §11.27) — pre-staged 존재 시 본 fire SKIP
    pre = _git(["diff", "--cached", "--name-only"], timeout=30)
    if pre.returncode != 0:
        log(f"SYNC FAIL (git diff): rc={pre.returncode}")
        return 2
    if pre.stdout.strip():
        log(
            f"SYNC SKIP: pre-existing staged changes ({len(pre.stdout.splitlines())} files) — 다음 fire 재시도"
        )
        return 2

    status = _git(["status", "--porcelain", "--", rel], timeout=30)
    if status.returncode != 0:
        log(f"SYNC FAIL (git status): rc={status.returncode}")
        return 2
    if not status.stdout.strip():
        log(f"SYNC SKIP: no diff (idempotent, {rel})")
        return 0

    if _git(["add", rel], timeout=30).returncode != 0:
        log("SYNC FAIL (git add)")
        return 2
    msg = f"data(pm320,preview,{date_str}): 15:20 경량 픽 선공개 (push_pick_preview.py)"
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
                f"SYNC: push done → origin main (preview {date_str}, attempt {attempt})"
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
    no_wait = os.environ.get("M1S_PREVIEW_NO_WAIT") == "1"
    if not no_wait:
        _wait_until(FIRE_HMS)
    date_str = _today_kst()
    log(f"BEGIN push_pick_preview date={date_str}")

    picks = load_picks_with_wait(date_str, no_wait)
    if picks is None:
        return 0

    check_profile_consistency(picks.get("profile_id"))
    record = build_preview_record(picks, date_str)
    if record is None:
        return 0
    log(
        f"PREVIEW: {record['name']}({record['code']}) entry={record['entry_price']} "
        f"tp={record['take_profit_target_price']} water={record['watering_target_price']}"
    )

    target = write_preview(record, date_str)
    if target is None:
        return 1
    rc = sync_push(target, date_str)

    # S2 dual-write (DOC-20260614-DSN-002): 홈 push 성공 후 독립 repo 로도 동기화.
    # 🔴 failure isolated — 독립 repo 실패해도 rc(홈 픽 exit) 불변 (무중단 최우선).
    #    M1S_PM320_INDEPENDENT 미설정 시 무동작 (S2 비활성, 현행 0 변화).
    if rc == 0:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dual_sync import dual_write

        dual_write(
            source_path=target,
            rel_data_path=f"pm320_history/preview/{date_str}.json",
            date_str=date_str,
            log=log,
            commit_label="preview",
        )

    log(f"END rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
