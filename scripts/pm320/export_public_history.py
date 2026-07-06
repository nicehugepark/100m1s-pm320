#!/usr/bin/env python3
"""
PM320 live deploy history exporter.

`projects/pm320/data/history` is a private/generated work area and may change when
backtests are rerun. This exporter writes the public, sanitized, deploy-only copy
that the homepage is allowed to serve.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DIR = REPO_ROOT / "projects" / "pm320" / "data" / "history"
PUBLIC_DIR = REPO_ROOT / "projects" / "pm320" / "data" / "deploy_history"
START_BALANCE = 10_000_000
KST = timezone(timedelta(hours=9))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def _copy_day(date_str: str) -> Path | None:
    src = PRIVATE_DIR / f"{date_str}.json"
    if not src.exists():
        return None
    dst = PUBLIC_DIR / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = _read_json(src)
    meta = data.get("_meta")
    if isinstance(meta, dict):
        meta.pop("backtest_meta", None)
    _write_json(dst, data)
    return dst


def _clean_table(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = summary.get("backtest_detail", {}).get("table", [])
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = row.get("date")
        if not date:
            continue
        date_key = str(date)
        if date_key in seen_dates:
            raise ValueError(
                f"duplicate backtest entry date in public table: {date_key}"
            )
        seen_dates.add(date_key)
        out.append(
            {
                "date": date,
                "code": row.get("code"),
                "name": row.get("name"),
                "entry_price": row.get("entry_price"),
                "exit_date": row.get("exit_date"),
                "exit_class": row.get("exit_class"),
                "ret_pct": row.get("ret_pct"),
                "pnl": row.get("pnl"),
                "balance_after": row.get("balance_after"),
                "watered": bool(row.get("watered")),
                "_settle_seq": row.get("_settle_seq"),
                "settlement_order": row.get("settlement_order")
                or row.get("_settle_seq"),
            }
        )
    return out


def _dedupe_curve_rows(rows: list[Any]) -> list[dict[str, Any]]:
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
        by_date[date_key] = {
            "date": date_key,
            "balance": row.get("balance"),
            "name": row.get("name"),
        }
    return [by_date[date_key] for date_key in order]


def _clean_curve(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = summary.get("backtest_detail", {}).get("equity_curve", [])
    if not isinstance(rows, list):
        return []
    return _dedupe_curve_rows(rows)


def _is_take_profit(row: dict[str, Any]) -> bool:
    text = str(row.get("exit_class") or "")
    return "익절" in text or "목표" in text


def _account_mdd(curve: list[dict[str, Any]]) -> float | None:
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


def _money_key(value: Any) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) else None


def _sort_table_by_settlement_order(
    table: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        table,
        key=lambda row: (
            str(row.get("exit_date") or row.get("date") or ""),
            row.get("settlement_order")
            if isinstance(row.get("settlement_order"), int)
            else (
                row.get("_settle_seq")
                if isinstance(row.get("_settle_seq"), int)
                else 1_000_000
            ),
            str(row.get("code") or ""),
            str(row.get("date") or ""),
        ),
    )


def _strip_private_table_fields(table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in row.items() if not str(k).startswith("_")} for row in table
    ]


def _recompute(summary: dict[str, Any]) -> dict[str, Any]:
    detail = summary.setdefault("backtest_detail", {})
    table = detail.get("table") if isinstance(detail.get("table"), list) else []
    curve = (
        detail.get("equity_curve")
        if isinstance(detail.get("equity_curve"), list)
        else []
    )
    table = _sort_table_by_settlement_order(table)
    curve = _dedupe_curve_rows(curve)
    source_mdd = summary.get("account_mdd_pct")
    curve_mdd = _account_mdd(curve)
    account_mdd = (
        round(float(source_mdd), 4)
        if isinstance(source_mdd, (int, float))
        else curve_mdd
    )
    detail["table"] = _strip_private_table_fields(table)
    detail["equity_curve"] = curve

    settled = len(table)
    take_profit = sum(1 for r in table if _is_take_profit(r))
    expired_loss = sum(
        1
        for r in table
        if not _is_take_profit(r)
        and isinstance(r.get("ret_pct"), (int, float))
        and r["ret_pct"] <= 0
    )
    expired_gain = max(0, settled - take_profit - expired_loss)
    final_balance = next(
        (
            float(r["balance"])
            for r in reversed(curve)
            if isinstance(r.get("balance"), (int, float))
        ),
        None,
    )
    if final_balance is None:
        final_balance = next(
            (
                float(r["balance_after"])
                for r in reversed(table)
                if isinstance(r.get("balance_after"), (int, float))
            ),
            None,
        )

    summary.update(
        {
            "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "since": min(
                (str(r.get("date") or "") for r in table if r.get("date")),
                default=summary.get("since"),
            ),
            "first_pick_date": min(
                (str(r.get("date") or "") for r in table if r.get("date")), default=None
            ),
            "last_settled_date": max(
                (str(r.get("exit_date") or r.get("date") or "") for r in table),
                default=None,
            ),
            "total_picks": settled,
            "settled": settled,
            "running": 0,
            "take_profit": take_profit,
            "expired_loss": expired_loss,
            "expired_gain": expired_gain,
            "win_rate": round(100.0 * take_profit / settled, 1) if settled else None,
            "start_balance": START_BALANCE,
            "account_mdd_pct": account_mdd,
            "worst_mdd_pct": account_mdd,
            "avg_mdd_pct": None,
            "total_return_pct": round((final_balance / START_BALANCE - 1.0) * 100.0, 4)
            if final_balance is not None
            else None,
            "total_pnl": round(final_balance - START_BALANCE, 2)
            if final_balance is not None
            else None,
            "final_balance": round(final_balance, 2)
            if final_balance is not None
            else None,
        }
    )
    return summary


def _full_summary(private_summary: dict[str, Any]) -> dict[str, Any]:
    return _recompute(
        {
            "account_mdd_pct": private_summary.get("account_mdd_pct"),
            "worst_mdd_pct": private_summary.get("worst_mdd_pct")
            or private_summary.get("account_mdd_pct"),
            "backtest_detail": {
                "as_of": private_summary.get("last_settled_date"),
                "table": _clean_table(private_summary),
                "equity_curve": _clean_curve(private_summary),
            },
        }
    )


def _merge_summary(private_summary: dict[str, Any], dates: set[str]) -> dict[str, Any]:
    public_path = PUBLIC_DIR / "summary.json"
    if not public_path.exists():
        return _full_summary(private_summary)

    current = _read_json(public_path)
    if isinstance(private_summary.get("account_mdd_pct"), (int, float)):
        current["account_mdd_pct"] = private_summary.get("account_mdd_pct")
        current["worst_mdd_pct"] = private_summary.get(
            "worst_mdd_pct"
        ) or private_summary.get("account_mdd_pct")
    old_detail = current.setdefault("backtest_detail", {})
    old_table = (
        old_detail.get("table") if isinstance(old_detail.get("table"), list) else []
    )
    old_curve = (
        old_detail.get("equity_curve")
        if isinstance(old_detail.get("equity_curve"), list)
        else []
    )

    private_table = _clean_table(private_summary)
    update_rows = [
        r
        for r in private_table
        if str(r.get("date") or "") in dates or str(r.get("exit_date") or "") in dates
    ]
    if update_rows:
        by_date = {
            str(r.get("date")): r
            for r in old_table
            if isinstance(r, dict) and r.get("date")
        }
        for row in update_rows:
            by_date[str(row.get("date"))] = row
        old_detail["table"] = list(by_date.values())

    private_curve = _clean_curve(private_summary)
    update_curve = [r for r in private_curve if str(r.get("date") or "") in dates]
    if update_curve:
        old_detail["equity_curve"] = [
            r
            for r in old_curve
            if isinstance(r, dict) and str(r.get("date") or "") not in dates
        ] + update_curve

    current["backtest_detail"] = old_detail
    return _recompute(current)


def export_public(
    dates: list[str] | None = None, all_files: bool = False
) -> list[Path]:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if all_files:
        for old in PUBLIC_DIR.glob("*.json"):
            old.unlink()
        for src in sorted(PRIVATE_DIR.glob("20*.json")):
            copied = _copy_day(src.stem)
            if copied is not None:
                written.append(copied)
    else:
        for date_str in dates or []:
            copied = _copy_day(date_str)
            if copied is not None:
                written.append(copied)

    private_summary_path = PRIVATE_DIR / "summary.json"
    if private_summary_path.exists():
        private_summary = _read_json(private_summary_path)
        public_summary = (
            _full_summary(private_summary)
            if all_files or not dates
            else _merge_summary(private_summary, set(dates))
        )
        summary_path = PUBLIC_DIR / "summary.json"
        _write_json(summary_path, public_summary)
        written.append(summary_path)

    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export sanitized PM320 deploy history"
    )
    parser.add_argument(
        "--date",
        action="append",
        help="YYYY-MM-DD. Repeatable. Omit with --all for full init.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Initialize/refresh the full public deploy copy.",
    )
    parser.add_argument("--print", action="store_true", help="Print written paths.")
    args = parser.parse_args()

    written = export_public(dates=args.date, all_files=args.all)
    if args.print:
        for path in written:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
