#!/usr/bin/env python3
"""
PM320 카드 universe pm320_history 전체 재백필 — 현 active 검증 백테스트
(게이트1천억 + 음봉 AND (3천억 OR 10영업일 top5) + 잔존1 단독픽 +
물타기 1배 + 물타기 D+6) 적용. 대표 2026-06-11 확정.

본 드라이버는 검증 백테스트 결과 JSON 의 selection_trace/timeline/summary 를 SSOT 로 삼아
전 거래일(4/8~6/11) 카드 history 와 summary.json 을 재산출한다. 카드 universe 생성은
build_card_history.py 를 재사용하지만, PICK 종목의 청산 상태와 summary/detail 전수표는
백테스트 결과를 그대로 overlay 한다.

flow:
  1. 신 SoT selection_trace read → date별 pick_code (보류=None).
  2. 각 거래일별 picks 합성 (in-memory monkeypatch) → build_card_history.build_history().
  3. 백테스트 timeline 의 PICK 결과를 해당 카드에 overlay.
  4. 메인 worktree write (projects/pm320/data/history/{date}.json).
  5. summary.json 은 백테스트 summary/timeline/balance_history 로 직접 생성.
  6. M1S_HISTORY_NO_SYNC=1 (per-day push 억제) — 루프 종료 후 일괄 배포는 별도.

검증 정합 (백테스트 +9.5431%):
  - 6/2 보류 / 6/8 보류 / 6/11 원익IPS 단독픽 D0 장외 익절.

usage:
  python3 scripts/pm320/backfill_card_history_water1x_d6.py [--start] [--end] [--dry-run]
  --start default 2026-04-08 / --end default 2026-06-11.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "pm320"))

import build_card_history as BCH  # noqa: E402
import build_judgement_minutes as BJM  # noqa: E402

NEW_SOT = (
    REPO_ROOT
    / "projects"
    / "pm320"
    / "research"
    / "backtest-3d-3.2pct"
    / "adhoc_20260611_bear3000_or_rank5_singleton"
    / "unified_select_gate1000_bear3000_or_rank5_singleton_water1x_d6_to_0611_current_results.json"
)

DEFAULT_START = "2026-04-08"
DEFAULT_END = "2026-06-11"
DISPLAY_START_BALANCE = 10_000_000

KST = timezone(timedelta(hours=9))


def log(msg: str) -> None:
    print(f"[backfill_w1x_d6] {msg}", file=sys.stderr, flush=True)


def load_results() -> dict[str, Any]:
    if not NEW_SOT.exists():
        raise FileNotFoundError(f"new SoT not found: {NEW_SOT}")
    d = json.loads(NEW_SOT.read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise ValueError("result root not object")
    return d


def load_trace(d: dict[str, Any]) -> list[dict[str, Any]]:
    trace = d.get("selection_trace", [])
    if not isinstance(trace, list):
        raise ValueError("selection_trace not list")
    return trace


def _timeline_by_date(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in d.get("timeline", []) or []:
        if row.get("row_kind") == "진입" and row.get("pick_code"):
            out[row["date"]] = row
    return out


def synthesize_picks(item: dict[str, Any]) -> dict[str, Any]:
    """selection_trace entry → picks JSON 합성 (보류는 picked=null)."""
    branch = item.get("branch")
    pick_code = item.get("pick_code") if branch != "보류" else None
    picked = (
        {
            "code": pick_code,
            "name": item.get("pick_name", ""),
            "branch": branch,
            "amount": item.get("pick_amount"),
        }
        if pick_code
        else None
    )
    return {
        "date": item["date"],
        "picked": picked,
        "source": "backtest_water1x_d6_reconstruction",
        "backtest_meta": {
            "T1": item.get("T1"),
            "T2": item.get("T2"),
            "gap_ratio": item.get("gap_ratio"),
            "branch": branch,
            "reason": item.get("reason", ""),
        },
    }


def build_one(
    date_str: str,
    item: dict[str, Any],
    bt_row: dict[str, Any] | None,
    dry_run: bool,
) -> dict[str, Any] | None:
    synthesized = synthesize_picks(item)
    history_path = BCH.HISTORY_OUT_DIR / f"{date_str}.json"

    orig_load_picks = BCH.load_picks
    BCH.load_picks = lambda d, _s=synthesized: (
        _s if d == date_str else orig_load_picks(d)
    )  # noqa: E731
    try:
        history = BCH.build_history(date_str)
    finally:
        BCH.load_picks = orig_load_picks

    if history is None:
        log(f"FAIL: build_history None for {date_str}")
        return None

    if bt_row is not None:
        overlay_backtest_pick(history, bt_row)

    if dry_run:
        log(f"DRY-RUN: would write {history_path}")
    else:
        BCH.atomic_write_json(history_path, history)

    return {
        "date": date_str,
        "picked_code": history.get("picked_code"),
        "_meta": history.get("_meta", {}),
    }


def overlay_backtest_pick(history: dict[str, Any], row: dict[str, Any]) -> None:
    """백테스트 timeline 의 정확한 PICK 결과를 history 의 PICK 카드에 overlay."""
    code = row.get("pick_code")
    if not code:
        return
    stock = None
    for s in history.get("stocks", []):
        if s.get("code") == code:
            stock = s
            break
    if stock is None:
        stock = {"code": code, "name": row.get("pick_name", ""), "pm320_pick": {}}
        history.setdefault("stocks", []).append(stock)

    pk = stock.setdefault("pm320_pick", {})
    ret_pct = row.get("ret_pct")
    state = "taken_profit" if row.get("exit_type") == "익절" else (
        "expired_gain" if isinstance(ret_pct, (int, float)) and ret_pct > 0 else "expired_loss"
    )
    pk.update(
        {
            "is_pick": True,
            "pick_date": row.get("date"),
            "entry_price": row.get("entry_price_p0") or pk.get("entry_price"),
            "expiry_date": (
                row.get("exit_date")
                if row.get("exit_type") != "익절" and row.get("exit_date")
                else pk.get("expiry_date")
            ),
            "current_state": state,
            "current_pnl_pct": float(ret_pct) if isinstance(ret_pct, (int, float)) else 0.0,
            "result": {
                "final_price": int(round(row.get("exit_price") or 0)),
                "final_pnl_pct": round(float(ret_pct), 2) if isinstance(ret_pct, (int, float)) else 0.0,
                "watered": bool(row.get("martingaled")),
                "result_date": row.get("exit_date"),
                "same_day_afterhours": bool(row.get("same_day_afterhours")),
                "current_partial": bool(row.get("current_partial")),
            },
        }
    )
    history["picked_code"] = code
    pick_count = 0
    running_count = 0
    expired_count = 0
    for s in history.get("stocks", []):
        p = s.get("pm320_pick") or {}
        if s.get("code") == code:
            p["is_pick"] = True
        elif p.get("pick_date") == row.get("date"):
            p["is_pick"] = False
        if p.get("is_pick"):
            pick_count += 1
        if p.get("current_state") == "running":
            running_count += 1
        elif p:
            expired_count += 1
    meta = history.setdefault("_meta", {})
    meta["pick_count"] = pick_count
    meta["running_count"] = running_count
    meta["expired_count"] = expired_count


def _money(v: Any, scale: float) -> float | None:
    if not isinstance(v, (int, float)):
        return None
    return round(float(v) * scale, 2)


def _source_start_balance(s: dict[str, Any]) -> float:
    if isinstance(s.get("start_balance"), (int, float)) and s["start_balance"] > 0:
        return float(s["start_balance"])
    if isinstance(s.get("final_balance"), (int, float)) and isinstance(s.get("total_pnl"), (int, float)):
        inferred = float(s["final_balance"]) - float(s["total_pnl"])
        if inferred > 0:
            return inferred
    return float(DISPLAY_START_BALANCE)


def _collapse_equity_curve(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = row.get("date")
        if not date:
            continue
        date_key = str(date)
        if date_key not in by_date:
            order.append(date_key)
        by_date[date_key] = {**row, "date": date_key}
    return [by_date[date_key] for date_key in order]


def _account_mdd_pct(curve: list[dict[str, Any]]) -> float | None:
    peak: float | None = None
    worst = 0.0
    for row in curve:
        bal = row.get("balance")
        if not isinstance(bal, (int, float)):
            continue
        bal_f = float(bal)
        peak = bal_f if peak is None else max(peak, bal_f)
        if peak:
            worst = min(worst, (bal_f / peak - 1.0) * 100.0)
    return round(worst, 4) if peak is not None else None


def _settlement_order_by_key(d: dict[str, Any]) -> dict[tuple[str, str, str, str], list[int]]:
    order: dict[tuple[str, str, str, str], list[int]] = {}
    for idx, row in enumerate(d.get("balance_history", []) or []):
        if row.get("event") != "청산":
            continue
        key = (
            str(row.get("date") or ""),
            str(row.get("code") or ""),
            str(row.get("name") or ""),
            str(row.get("exit_class") or ""),
        )
        order.setdefault(key, []).append(idx)
    return order


def _settlement_seq(
    order: dict[tuple[str, str, str, str], list[int]],
    row: dict[str, Any],
    fallback: int,
) -> int:
    key = (
        str(row.get("exit_date") or ""),
        str(row.get("pick_code") or ""),
        str(row.get("pick_name") or ""),
        str(row.get("exit_class") or row.get("result") or ""),
    )
    seqs = order.get(key)
    if seqs:
        return seqs.pop(0)
    return 1_000_000 + fallback


def _sort_table_by_settlement(table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        table,
        key=lambda row: (
            str(row.get("exit_date") or row.get("date") or ""),
            row.get("_settle_seq") if isinstance(row.get("_settle_seq"), int) else 1_000_000,
        ),
    )


def build_summary_from_backtest(d: dict[str, Any]) -> dict[str, Any]:
    """승률카드 summary.json 을 검증 백테스트 결과에서 직접 생성."""
    s = d.get("summary") or {}
    source_start = _source_start_balance(s)
    balance_scale = DISPLAY_START_BALANCE / source_start if source_start > 0 else 1.0
    final_balance = _money(s.get("final_balance"), balance_scale)
    total_pnl = _money(s.get("total_pnl"), balance_scale)
    if final_balance is None and total_pnl is not None:
        final_balance = round(DISPLAY_START_BALANCE + total_pnl, 2)
    if total_pnl is None and final_balance is not None:
        total_pnl = round(final_balance - DISPLAY_START_BALANCE, 2)
    total_return_pct = (
        round((final_balance / DISPLAY_START_BALANCE - 1.0) * 100.0, 4)
        if final_balance is not None
        else s.get("total_return_pct")
    )
    timeline = [
        r for r in (d.get("timeline") or [])
        if r.get("row_kind") == "진입" and r.get("pick_code")
    ]
    settled = int(s.get("n_closed") or len(timeline))
    take_profit = int(s.get("reach_count") or sum(1 for r in timeline if r.get("exit_type") == "익절"))
    expired_loss = int(s.get("expiry_loss_count") or sum(
        1 for r in timeline
        if r.get("exit_type") != "익절" and isinstance(r.get("ret_pct"), (int, float)) and r["ret_pct"] <= 0
    ))
    expired_gain = int(s.get("expiry_count") or 0) - expired_loss
    expired_gain = max(0, expired_gain)
    total_picks = int(s.get("n_entered") or len(timeline))
    win_rate = round(100.0 * take_profit / settled, 1) if settled else None
    settle_order = _settlement_order_by_key(d)
    table = []
    for idx, r in enumerate(timeline):
        table.append(
            {
                "date": r.get("date"),
                "code": r.get("pick_code"),
                "name": r.get("pick_name"),
                "entry_price": r.get("entry_price_p0"),
                "exit_date": r.get("exit_date"),
                "exit_class": r.get("exit_class") or r.get("result"),
                "ret_pct": r.get("ret_pct"),
                "pnl": _money(r.get("pnl"), balance_scale),
                "balance_after": _money(r.get("balance_after"), balance_scale),
                "watered": bool(r.get("martingaled")),
                "same_day_afterhours": bool(r.get("same_day_afterhours")),
                "_settle_seq": _settlement_seq(settle_order, r, idx),
            }
        )

    equity_curve_raw = []
    for b in d.get("balance_history", []) or []:
        if b.get("event") == "청산":
            equity_curve_raw.append(
                {
                    "date": b.get("date"),
                    "balance": _money(b.get("balance"), balance_scale),
                    "name": b.get("name"),
                    "exit_class": b.get("exit_class"),
                }
            )
    equity_curve = _collapse_equity_curve(equity_curve_raw)
    source_mdd = s.get("mdd_pct")
    account_mdd_pct = round(float(source_mdd), 4) if isinstance(source_mdd, (int, float)) else _account_mdd_pct(equity_curve)
    table = _sort_table_by_settlement(table)

    generated_at = datetime.now(KST).isoformat(timespec="seconds")
    return {
        "generated_at": generated_at,
        "since": "2026-04-08",
        "first_pick_date": timeline[0]["date"] if timeline else None,
        "last_settled_date": timeline[-1].get("exit_date") if timeline else None,
        "total_picks": total_picks,
        "settled": settled,
        "running": int(s.get("n_held_open") or 0),
        "take_profit": take_profit,
        "expired_loss": expired_loss,
        "expired_gain": expired_gain,
        "win_rate": win_rate,
        "_basis": "검증 백테스트 timeline 기준 (보유중 제외). 승률 = 익절 / 청산완료. 원화 잔고·손익은 초기 시드 1천만원 기준.",
        "_source": "backtest_ssot",
        "start_balance": DISPLAY_START_BALANCE,
        "source_start_balance": round(source_start, 2),
        "balance_scale": round(balance_scale, 8),
        "take_profit_target_pct": 3.2,
        "account_mdd_pct": account_mdd_pct,
        "worst_mdd_pct": account_mdd_pct,
        "avg_mdd_pct": None,
        "_mdd_basis": "계좌 MDD = 백테스트 balance_history 기준 누적 잔고 최대낙폭.",
        "total_return_pct": total_return_pct,
        "total_pnl": total_pnl,
        "final_balance": final_balance,
        "branch_counts": s.get("branch_counts"),
        "same_day_afterhours_exits": s.get("same_day_afterhours_exits"),
        "profile_id": "gate1000_bear3000_or_rank5_water1x_d6",
        "condition": s.get("bear_filter_rule"),
        "backtest_detail": {
            "source": str(NEW_SOT.relative_to(REPO_ROOT)),
            "as_of": s.get("as_of"),
            "table": table,
            "equity_curve": equity_curve,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="PM320 history 재백필 (물타기1배+D6)")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # per-day push 억제 (40일 루프 동안 매 일자 push 폭발 방지). 일괄 배포는 루프 후 별도.
    os.environ.setdefault("M1S_HISTORY_NO_SYNC", "1")

    log(f"START: {args.start} ~ {args.end} dry_run={args.dry_run}")
    snapshot = BJM.build_snapshot(args.start, args.end)
    log(
        "JUDGE_SNAPSHOT: "
        f"regular={snapshot['regular_rows']} nxt={snapshot['nxt_rows']} "
        f"total={snapshot['total_rows']} ext={snapshot['extended_rows']} "
        f"db={snapshot['out_db']}"
    )
    result_doc = load_results()
    trace = load_trace(result_doc)
    timeline = _timeline_by_date(result_doc)
    log(f"trace loaded: {len(trace)} entries")

    summary: list[dict[str, Any]] = []
    for item in trace:
        date_str = item.get("date")
        if not isinstance(date_str, str):
            continue
        if not (args.start <= date_str <= args.end):
            continue
        if item.get("is_holiday"):
            continue
        try:
            res = build_one(date_str, item, timeline.get(date_str), args.dry_run)
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

    log(f"DONE: built {len(summary)} days")
    if summary:
        agg_pick = sum(s["_meta"].get("pick_count", 0) for s in summary)
        agg_expired = sum(s["_meta"].get("expired_count", 0) for s in summary)
        agg_running = sum(s["_meta"].get("running_count", 0) for s in summary)
        log(f"AGG: pick={agg_pick} expired={agg_expired} running={agg_running}")
    summary_doc = build_summary_from_backtest(result_doc)
    if args.dry_run:
        log("DRY-RUN: would write summary.json")
    else:
        BCH.atomic_write_json(BCH.HISTORY_OUT_DIR / "summary.json", summary_doc)
        log(
            f"SUMMARY: picks={summary_doc['total_picks']} settled={summary_doc['settled']} "
            f"win={summary_doc['take_profit']} loss={summary_doc['expired_loss']} "
            f"rate={summary_doc['win_rate']}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
