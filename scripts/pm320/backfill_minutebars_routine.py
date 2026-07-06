#!/usr/bin/env python3
"""PM320 1분봉 자동 backfill routine — 정규장/NXT 장외 통합 launchd 호출.

요청 시각: 2026-06-03 (수) 10:48 KST / 타치코마 → dev-minute-routine-plist (대표 trigger)

목적
----
launchd `com.100m1s.pm320-minute-backfill.plist` 가 매시 1/11/21/31/35/41/51분
호출하고, 본 wrapper 가 시간대를 판정해 실제 작업만 수행한다.

- 09:00~15:20 (장중 tick: 1/11/21/31분): **보유중(running) 픽만** NXT 통합(_AL)
  1분봉을 수집해 익절 준실시간 판정 (2026-06-15 대표 직접 지시 — "청산시간 전이라도
  익절 구간 도달 시 즉시 노출"). 보유중 픽 pick_date 별 build_card_history 재판정 →
  high ≥ 익절선 도달 즉시 taken_profit. 상세: scripts.pm320.running_picks SSOT.
- 15:31, 15:35: 당일 정규 1분봉(09:00~15:30)을
  scripts.news_pipeline.collect_minutebars 백필 모드로 적재.
- 15:41~20:11: 당일 통합/NXT 1분봉(장전·정규·장후, 특히 15:20 이후 장외 판정)을
  scripts.news_pipeline.collect_minutebars_nxt 백필 모드로 적재.
- 수집을 실행한 모든 tick 뒤 processed snapshot 생성 → build_card_history 재판정
  순서로 cascade 한다.

휴장일(주말·공휴일) 자동 skip — collect_minutebars 의 _load_target_codes 가
한 번이라도 호출되기 전에 본 wrapper 가 is_market_holiday 로 차단 (불필요한
키움 token 발급·rate-limit 소비 회피).

대상 종목
---------
collect_minutebars._load_target_codes() 위임 (research SUMMARY universe 합집합,
현재 337 codes). `--codes` 미지정 → collect_minutebars 가 자동 산출. wrapper 가
universe 를 새로 정의하지 않는다 (SSOT 위반 방지).

장중 가드 정합
--------------
collect_minutebars 본문 `guard_intraday_research` 가 09:00~15:30 KST 차단
(_INTRADAY_BLOCK_END = (15, 30) 포함, 대표 지시 2026-06-05 마진 5분→1분 단축).
따라서 정규 백필 tick 은 **15:31/15:35 KST**, NXT 장외 백필 tick 은
**15:41~20:11 KST** 로 제한한다. 마감 후 백필은 ALLOW_INTRADAY_RESEARCH 미사용.
- 장중 익절 tick (09:00~15:20) 만 예외적으로 ALLOW_INTRADAY_RESEARCH=1 명시(가드
  우회) + `--codes` 로 **보유중 픽(~30종)만** 좁혀 수집. 무차별 universe(337종)
  장중 폴링은 금지 — 프로덕션 폴링과 경합 최소화 (대표 2026-05-26 취지 보존).
  throttle 0.5s = 초당 2회 (키움 한도 5회/초의 40%).

카드 재생성 cascade (2026-06-05 신설)
-------------------------------------
백필 완료 후 build_judgement_minutes --start today --end today 로 raw 정규/NXT를
판정용 processed DB 에 병합한 뒤 build_card_history --date today 를 cascade 로 호출한다. 직전에는
history-build plist 가 15:25 (백필 15:31 보다 먼저) 독립 fire → 당일 만기종목
카드가 분봉(minutes.db) 부재 시점에 일봉 fallback 으로 부정확하게 생성되는 사고.
backfill → card 순서를 wrapper 내 cascade 로 묶어 시각 race 를 원천 제거한다.
processed snapshot 생성 실패 시 build_card_history 는 실행하지 않는다. raw 수집 DB를
직접 보거나 stale processed 를 쓰는 fallback 은 금지한다.

종료 코드
---------
  0 — 정상 적재 또는 휴장일 정상 skip
  1 — collect_minutebars / collect_minutebars_nxt / processed snapshot / card cascade 실패
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# S5 자립화 (DOC-20260707-REQ-001): 메인 레포 절대경로 → env(M1S_COMPANY) 우선 + pm320 레포 루트 fallback.
REPO_ROOT = Path(
    os.environ.get("M1S_COMPANY", str(Path(__file__).resolve().parents[2]))
)

REGULAR_BACKFILL_TICKS = {(15, 31), (15, 35)}
NXT_AFTERHOURS_START = (15, 41)
NXT_AFTERHOURS_END = (20, 11)

# 장중 익절 준실시간 tick (2026-06-15, 대표 직접 지시 — "청산시간 전이라도 익절 구간
# 도달 시 즉시 노출"). 09:00~15:20(픽 확정시각) 구간의 plist tick(1/11/21/31분)에서
# **보유중(running) 픽만** NXT 통합(_AL) 분봉을 수집해 judge_minutes 병합 → 보유중 픽
# pick_date 별 build_card_history 재판정. high ≥ 익절선 도달 즉시 taken_profit 전이.
#   - 대상 한정(running 30종 내외) + throttle 0.5s = 초당 2회 (키움 한도 5회의 40%).
#   - 장중 수집이므로 ALLOW_INTRADAY_RESEARCH=1 명시(가드 우회) + --codes 로 좁힌다.
#     무차별 universe(337종) 장중 폴링 금지 — 프로덕션 경합 최소화 (대표 2026-05-26 정합).
#   - 15:21 이후(애프터마켓·마감)는 기존 REGULAR/NXT_AFTERHOURS tick 이 커버(이중 안전).
INTRADAY_RUNNING_START = (9, 0)
INTRADAY_RUNNING_END = (15, 20)


def _hm(now: datetime) -> tuple[int, int]:
    return now.hour, now.minute


def _between(
    cur: tuple[int, int], start: tuple[int, int], end: tuple[int, int]
) -> bool:
    return start <= cur <= end


def _run(cmd: list[str], env: dict[str, str], label: str) -> int:
    print(f"[backfill_minutebars_routine] {label} 시작 cmd={' '.join(cmd)}")
    try:
        rc = subprocess.call(cmd, cwd=str(REPO_ROOT), env=env)
    except Exception as e:
        print(f"[backfill_minutebars_routine] {label} subprocess exception: {e}")
        return 1
    print(f"[backfill_minutebars_routine] {label} exit={rc}")
    return int(rc)


def _run_intraday_running(today: str, stamp: str, env: dict[str, str]) -> int:
    """장중 보유중(running) 픽 익절 준실시간 판정 cascade.

    1) running_picks 로 보유중 픽 SSOT 도출 (없으면 graceful 0 — 폴링 자체 skip).
    2) NXT 통합(_AL) 분봉을 **running 종목만** 수집 (ALLOW_INTRADAY_RESEARCH=1 + --codes).
       NXT _AL = 08~20시 통합시세(실전키) 단일 소스. 정규 collect_minutebars(모의키·
       정규장만)는 장중 가드 대상이라 호출 안 한다.
    3) build_judgement_minutes 로 raw → 판정용 processed DB 병합 (실패 시 카드 skip).
    4) 보유중 픽 pick_date 별 build_card_history 재판정 → high ≥ 익절선 도달분 즉시
       taken_profit. forward 분봉 미커버 종목은 running 유지 (실데이터만, 추정 0).

    실 분봉이 없으면(수집 0·미반환) 카드는 running 그대로 — 가짜 익절 0 (FLR-AGT-002).
    """
    from scripts.pm320.running_picks import running_codes, running_pick_dates

    codes = running_codes(today)
    if not codes:
        print(f"[backfill_minutebars_routine] {stamp} {today} 보유중 픽 0 — 장중 skip")
        return 0

    pick_dates = running_pick_dates(today)
    print(
        f"[backfill_minutebars_routine] {stamp} {today} 장중 익절 tick — "
        f"보유중 {len(codes)}종목 / pick_date {len(pick_dates)}개"
    )

    # NXT 통합(_AL) 분봉 — 장중 수집이므로 가드 우회 명시. running 종목만 좁힌다.
    intraday_env = dict(env)
    intraday_env["ALLOW_INTRADAY_RESEARCH"] = "1"
    nxt_cmd = [
        sys.executable,
        "-m",
        "scripts.news_pipeline.collect_minutebars_nxt",
        "--backfill",
        "--start",
        today,
        "--end",
        today,
        "--codes",
        *codes,
    ]
    collect_rc = _run(
        nxt_cmd, intraday_env, f"{stamp} {today} 장중 보유중 NXT 분봉({len(codes)}종)"
    )
    # 수집 실패(token/네트워크)면 stale 판정 위험 → 카드 재판정 skip (graceful).
    # 수집 0건(거래 없음)은 collect 자체가 0 exit 가능하나, 그 경우 judge_minutes 가
    # 신규 분봉 0 → 카드 running 유지 (정상). 따라서 rc != 0 일 때만 차단한다.
    if collect_rc != 0:
        print(
            f"[backfill_minutebars_routine] {stamp} {today} 장중 수집 rc={collect_rc} "
            "— 카드 재판정 skip (stale 방지, 다음 tick 재시도)"
        )
        return 1

    # processed snapshot 범위 = [가장 이른 보유중 pick_date ~ 오늘].
    # build_card_history.require_fresh_judge_minutes 가 last_range 에 --date(과거 pick_date)
    # 포함을 요구하므로(L356-361), 오늘만 빌드하면 6/12 등 과거 pick_date 카드가 게이트에서
    # 막힌다. forward 윈도우(pick_date+1 ~ 오늘) 분봉도 같은 범위에 들어와 익절 판정에 필요.
    snapshot_start = pick_dates[0] if pick_dates else today
    process_cmd = [
        sys.executable,
        "-m",
        "scripts.pm320.build_judgement_minutes",
        "--start",
        snapshot_start,
        "--end",
        today,
    ]
    process_rc = _run(
        process_cmd,
        env,
        f"{stamp} {today} 장중 판정용 processed minute snapshot ({snapshot_start}~{today})",
    )
    if process_rc != 0:
        return 1

    # 보유중 픽 pick_date 별 카드 재판정 (build_card_history 는 --date 1개씩 처리).
    # 오늘(today) 분봉이 forward 윈도우에 들어가는 pick_date 만 재판정하면 충분하나,
    # 보유중 픽이 걸린 모든 pick_date 를 도는 게 누락 0 (소수 pick_date 라 비용 무시).
    card_rcs: list[int] = []
    for pd in pick_dates:
        card_cmd = [
            sys.executable,
            "-m",
            "scripts.pm320.build_card_history",
            "--date",
            pd,
        ]
        card_rcs.append(
            _run(card_cmd, env, f"{stamp} {today} 보유중 카드 재판정 (pick_date={pd})")
        )

    return (
        0
        if collect_rc == 0 and process_rc == 0 and all(rc == 0 for rc in card_rcs)
        else 1
    )


def main() -> int:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M:%S KST")

    # --- 휴장일 사전 차단 (token 발급 + rate-limit 절약) ---
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.news_pipeline.config import is_market_holiday  # noqa: E402

    if is_market_holiday(today):
        print(f"[backfill_minutebars_routine] {stamp} {today} 휴장일 — SKIP")
        return 0

    env = dict(os.environ)
    # ALLOW_INTRADAY_RESEARCH 미설정 (15:31 이후 trigger 가 가드 경계 15:30 외).

    cur = _hm(now)
    run_regular = cur in REGULAR_BACKFILL_TICKS
    run_nxt = _between(cur, NXT_AFTERHOURS_START, NXT_AFTERHOURS_END)
    run_intraday = _between(cur, INTRADAY_RUNNING_START, INTRADAY_RUNNING_END)

    # 장중(09:00~15:20) tick = 보유중 픽 익절 준실시간 경로 (대표 직접 지시).
    # 마감 후 정규/NXT 백필과 상호 배타 (시간대가 안 겹친다) — 별 핸들러로 분리한다.
    if run_intraday and not run_regular and not run_nxt:
        return _run_intraday_running(today, stamp, env)

    if not run_regular and not run_nxt:
        print(f"[backfill_minutebars_routine] {stamp} {today} 실행 시간대 아님 — SKIP")
        return 0

    rcs: list[int] = []
    if run_regular:
        regular_cmd = [
            sys.executable,
            "-m",
            "scripts.news_pipeline.collect_minutebars",
            "--backfill",
            "--start",
            today,
            "--end",
            today,
        ]
        rcs.append(_run(regular_cmd, env, f"{stamp} {today} 정규분봉 backfill"))

    if run_nxt:
        nxt_cmd = [
            sys.executable,
            "-m",
            "scripts.news_pipeline.collect_minutebars_nxt",
            "--backfill",
            "--start",
            today,
            "--end",
            today,
        ]
        rcs.append(_run(nxt_cmd, env, f"{stamp} {today} NXT/장외분봉 backfill"))

    # --- processed snapshot + 카드 재생성 cascade ---
    # raw 수집 DB는 판정에 직접 사용하지 않는다. processed snapshot 생성 성공 뒤에만 카드 생성.
    process_cmd = [
        sys.executable,
        "-m",
        "scripts.pm320.build_judgement_minutes",
        "--start",
        today,
        "--end",
        today,
    ]
    process_rc = _run(
        process_cmd, env, f"{stamp} {today} 판정용 processed minute snapshot"
    )
    if process_rc != 0:
        return 1

    card_cmd = [
        sys.executable,
        "-m",
        "scripts.pm320.build_card_history",
        "--date",
        today,
    ]
    card_rc = _run(card_cmd, env, f"{stamp} {today} 카드 재생성 cascade")
    if card_rc != 0:
        return 1

    return 0 if all(rc == 0 for rc in rcs) else 1


if __name__ == "__main__":
    sys.exit(main())
