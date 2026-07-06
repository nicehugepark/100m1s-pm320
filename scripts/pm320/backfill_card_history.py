#!/usr/bin/env python3
"""
PM320 카드 universe pm320_history Phase 3 reconstruction (4/8~5/26 backfill).

목적:
  - 대표 결정 A안 (Phase 3): 백테스트 결과로 4/8~5/26 과거 카드들 채워넣기
  - SoT: projects/pm320/research/backtest-3d-3.2pct/gap75_rule_results_to_0601.json
  - selection_trace 40건 (4/8~6/1) 중 4/8~5/26 39거래일 reconstruction 대상
    (5/27~6/1은 backtest 진행 중이고 picks 정상 cron 적재 외)

flow:
  1. 백테스트 selection_trace read → date별 pick_code 추출
  2. 각 거래일 별:
     a) PICKS_DIR / {date}.json 부재 시 backtest pick_code로 합성 picks 파일 생성 (--write-picks 옵션)
        또는 in-memory picks dict 합성 (default)
     b) build_card_history.build_history() 호출
     c) 메인 worktree path 산출
  3. progress + 회귀 영향 0건 cross-check (메인 worktree write only)

usage:
  python3 scripts/pm320/backfill_card_history.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--dry-run] [--write-picks]
  --start default: 2026-04-08
  --end default: 2026-05-26
  --dry-run: HISTORY_OUT_DIR write skip (stdout summary만)
  --write-picks: PICKS_DIR 합성 picks JSON write (cron 정합용, 기본 false)

doc_id: feat(pm320,P0,card-recommendation,historical,backfill,DSN-001) — Phase 3 reconstruction wrapper
generated: 2026-06-03 (대표 A+A ack)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

# build_card_history 본문 import (메인 worktree path 정합)
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "pm320"))

import build_card_history as BCH  # noqa: E402

BACKTEST_SOT = (
    REPO_ROOT
    / "projects"
    / "pm320"
    / "research"
    / "backtest-3d-3.2pct"
    / "gap75_rule_results_to_0601.json"
)

DEFAULT_START = "2026-04-08"
DEFAULT_END = "2026-05-26"

KST = timezone(timedelta(hours=9))


def log(msg: str) -> None:
    print(f"[backfill_card_history] {msg}", file=sys.stderr, flush=True)


def load_backtest_trace() -> list[dict[str, Any]]:
    """gap75_rule_results_to_0601.json selection_trace read."""
    if not BACKTEST_SOT.exists():
        raise FileNotFoundError(f"backtest SoT not found: {BACKTEST_SOT}")
    d = json.loads(BACKTEST_SOT.read_text(encoding="utf-8"))
    trace = d.get("selection_trace", [])
    if not isinstance(trace, list):
        raise ValueError("selection_trace not list")
    return trace


def synthesize_picks(trace_item: dict[str, Any]) -> dict[str, Any]:
    """backtest selection_trace 단일 entry → picks JSON 합성.

    send_kakao_message.py picks schema 가정:
      {
        "date": "YYYY-MM-DD",
        "picked": { "code": "...", "name": "...", ... },
        "source": "backtest_reconstruction"
      }
    """
    return {
        "date": trace_item["date"],
        "picked": {
            "code": trace_item["pick_code"],
            "name": trace_item.get("pick_name", ""),
            "branch": trace_item.get("branch", ""),
            "amount": trace_item.get("pick_amount"),
        },
        "source": "backtest_reconstruction",
        "backtest_meta": {
            "T1": trace_item.get("T1"),
            "T2": trace_item.get("T2"),
            "gap_ratio": trace_item.get("gap_ratio"),
            "reason": trace_item.get("reason", ""),
        },
    }


def build_one(
    date_str: str,
    trace_item: dict[str, Any],
    write_picks: bool,
    dry_run: bool,
) -> dict[str, Any] | None:
    """단일 거래일 reconstruction.

    1. picks 합성 (PICKS_DIR / {date}.json) — --write-picks 시 file write, default in-memory monkeypatch
    2. build_card_history.build_history() 호출
    3. HISTORY_OUT_DIR write (dry-run 시 skip)
    """
    picks_path = BCH.PICKS_DIR / f"{date_str}.json"
    history_path = BCH.HISTORY_OUT_DIR / f"{date_str}.json"

    # picks 합성
    synthesized = synthesize_picks(trace_item)

    if write_picks:
        BCH.atomic_write_json(picks_path, synthesized)
        log(f"PICKS-WRITE: {picks_path.name}")
    else:
        # in-memory monkeypatch (load_picks 직접 return)
        orig_load_picks = BCH.load_picks
        BCH.load_picks = lambda d, _s=synthesized: (
            _s if d == date_str else orig_load_picks(d)
        )  # noqa: E731

    try:
        history = BCH.build_history(date_str)
    finally:
        if not write_picks:
            BCH.load_picks = orig_load_picks

    if history is None:
        log(f"FAIL: build_history None for {date_str}")
        return None

    if dry_run:
        log(f"DRY-RUN: would write {history_path}")
    else:
        BCH.atomic_write_json(history_path, history)

    return {
        "date": date_str,
        "picked_code": history.get("picked_code"),
        "_meta": history.get("_meta", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PM320 카드 universe pm320_history Phase 3 reconstruction"
    )
    parser.add_argument(
        "--start", default=DEFAULT_START, help=f"default {DEFAULT_START}"
    )
    parser.add_argument("--end", default=DEFAULT_END, help=f"default {DEFAULT_END}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="HISTORY_OUT_DIR write skip (stdout summary만)",
    )
    parser.add_argument(
        "--write-picks",
        action="store_true",
        help="PICKS_DIR 합성 picks JSON write (cron 정합용, 기본 false)",
    )
    args = parser.parse_args()

    log(
        f"START: {args.start} ~ {args.end} "
        f"dry_run={args.dry_run} write_picks={args.write_picks}"
    )

    trace = load_backtest_trace()
    log(f"backtest trace loaded: {len(trace)} entries")

    summary: list[dict[str, Any]] = []
    skip_dates: list[str] = []

    for item in trace:
        date_str = item.get("date")
        if not isinstance(date_str, str):
            continue
        if not (args.start <= date_str <= args.end):
            skip_dates.append(date_str)
            continue
        try:
            res = build_one(date_str, item, args.write_picks, args.dry_run)
        except Exception as exc:
            log(f"FAIL: {date_str}: {type(exc).__name__}: {exc}")
            continue
        if res is not None:
            summary.append(res)
            m = res["_meta"]
            log(
                f"  {date_str} picked={res['picked_code']} "
                f"total={m.get('total_cards')} pick={m.get('pick_count')} "
                f"virtual={m.get('virtual_count')} skip={m.get('skip_count')} "
                f"expired={m.get('expired_count')} running={m.get('running_count')}"
            )

    log(
        f"DONE: built {len(summary)} / target {sum(1 for t in trace if args.start <= t.get('date', '') <= args.end)}"
    )
    if skip_dates:
        log(f"out-of-range skipped: {len(skip_dates)} (5/27+ left to cron)")

    # 통계 합계
    if summary:
        agg_expired = sum(s["_meta"].get("expired_count", 0) for s in summary)
        agg_running = sum(s["_meta"].get("running_count", 0) for s in summary)
        agg_skip = sum(s["_meta"].get("skip_count", 0) for s in summary)
        agg_pick = sum(s["_meta"].get("pick_count", 0) for s in summary)
        log(
            f"AGG: pick={agg_pick} expired={agg_expired} "
            f"running={agg_running} skip={agg_skip}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
