#!/usr/bin/env python3
"""PM320 running 픽 SSOT — 현재 보유중(미청산·만기 미도래) 픽 단일 도출 경로.

요청 시각: 2026-06-15 (월) / 대표 직접 지시 — "익절 준실시간 반영. 청산시간 전이라도
익절 구간 도달 시 즉시 노출(매수자 만족)."

블로커 (직전 진단 a17b60a)
--------------------------
장중 익절 판정의 진짜 블로커 = **현재 보유중 픽의 단일 출처(SSOT) 부재**. 익절 판정
locus(`build_card_history.simulate_minute_touch`)는 forward 분봉(judge_minutes)을 보지만,
장중에 그 분봉이 적재되는 대상이 "당일 신규 카드 종목"(daily_picks)뿐이라 과거에 매수해
아직 보유중인 픽(running)은 장중 데이터 0 → 익절 판정 0 → 청산시간(15:31~) 이후에야 첫 판정.

본 모듈
-------
card history 산출물(`projects/pm320/data/history/<date>.json`)을 **파생**해 running 픽
집합을 도출한다 (신규 DB 신설 0, 기존 산출 단일 파생 — DSN-001 §1 schema 정합).

running 정의 (build_card_history.compute_pm320_pick 산출 schema verbatim):
  pm320_pick.current_state == "running"  AND  pm320_pick.expiry_date >= today
  (expiry < today = 만기 도래 → 더는 보유중 아님, 마감 사이클이 최종 청산 확정)

raw 파일이 곧 SSOT 이므로 본 모듈은 read-only. 추정·fallback 0 (FLR-AGT-002 거짓 충실성
차단 — 파일에 running 으로 적힌 것만 running).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HISTORY_DIR = REPO_ROOT / "projects" / "pm320" / "data" / "history"
KST = timezone(timedelta(hours=9))


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def running_picks(today: str | None = None) -> list[dict[str, object]]:
    """보유중(running·만기 미도래) 픽 목록.

    return: [{"code","name","pick_date","expiry_date","entry_price",
              "take_profit_target_price"}], pick_date asc · code asc 정렬.
    history 디렉토리 부재 / 파일 0건 시 빈 list (graceful).
    """
    day = today or _today_kst()
    if not HISTORY_DIR.exists():
        return []

    seen: set[str] = set()
    out: list[dict[str, object]] = []
    # 각 <date>.json = 그 pick_date 픽 1건의 forward 추적 스냅샷. 한 종목이 여러 날
    # 등장할 수 있으나 (code, pick_date) 가 유일하므로 dedup 키로 사용.
    for fp in sorted(HISTORY_DIR.glob("*.json")):
        if fp.name == "summary.json":
            continue
        try:
            doc = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for stock in doc.get("stocks", []) or []:
            pick = (stock or {}).get("pm320_pick")
            if not isinstance(pick, dict):
                continue
            if pick.get("current_state") != "running":
                continue
            expiry = pick.get("expiry_date")
            pick_date = pick.get("pick_date")
            # 만기 도래분 제외 (보유중 아님). expiry 결측은 보수적으로 포함.
            if expiry and str(expiry) < day:
                continue
            code = str(stock.get("code") or "")
            if not (len(code) == 6 and code.isdigit()):
                continue
            key = f"{code}@{pick_date}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "code": code,
                    "name": stock.get("name"),
                    "pick_date": pick_date,
                    "expiry_date": expiry,
                    "entry_price": pick.get("entry_price"),
                    "take_profit_target_price": pick.get("take_profit_target_price"),
                }
            )
    out.sort(key=lambda r: (str(r["pick_date"]), str(r["code"])))
    return out


def running_codes(today: str | None = None) -> list[str]:
    """보유중 픽의 distinct 종목코드 (분봉 수집 대상). pick_date 무관 종목 단위."""
    codes: list[str] = []
    seen: set[str] = set()
    for r in running_picks(today):
        c = str(r["code"])
        if c not in seen:
            seen.add(c)
            codes.append(c)
    return sorted(codes)


def running_pick_dates(today: str | None = None) -> list[str]:
    """보유중 픽이 존재하는 distinct pick_date (build_card_history --date 대상).

    카드 재판정은 pick_date 단위로 돈다 (build_card_history 가 --date 1개씩 처리).
    """
    dates = {str(r["pick_date"]) for r in running_picks(today) if r.get("pick_date")}
    return sorted(dates)


if __name__ == "__main__":
    import sys

    arg_today = sys.argv[1] if len(sys.argv) > 1 else None
    picks = running_picks(arg_today)
    print(f"[running_picks] today={arg_today or _today_kst()} → {len(picks)} 보유중 픽")
    for p in picks:
        print(
            f"  {p['pick_date']} {p['code']} {p['name']} "
            f"expiry={p['expiry_date']} entry={p['entry_price']} "
            f"tp={p['take_profit_target_price']}"
        )
    print(
        f"[running_picks] distinct 종목 {len(running_codes(arg_today))} / "
        f"pick_date {len(running_pick_dates(arg_today))}"
    )
