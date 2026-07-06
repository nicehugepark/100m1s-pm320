#!/usr/bin/env python3
"""PM320 전략 프로파일 배치 생성기 — 프로파일 N개 → 버전별 산출 일괄 생성.

각 프로파일을 M1S_PM320_PROFILE env 로 강제하여 build_card_history.build_history(date)
를 재실행, 버전별 카드 history + 카톡 샘플을 별도 디렉토리에 저장한다. 라이브
picks/history 디렉토리는 무touch (read-only 입력만 사용, sync hook skip).

"백필 기다림 없이 즉시 비교" 목적 — 여러 프로파일의 카드/카톡 결과를 한 번에 만들어
대조한다. active 변경은 하지 않는다 (switch.py 책임 분리).

산출 위치: projects/pm320/research/profile_matrix/{profile_id}/
  - cards/{date}.json       — build_history 결과 (라이브 history 와 동일 schema)
  - kakao/{date}.txt        — 카톡 메시지 본문 (picked 종목)
  - SUMMARY.json            — 프로파일별 집계 (pick 수 / 익절 / 만기 / 보류)

데이터 정합 (DOC-20260609-FLR-001 대응): fresh dailybars DB 강제. M1S_PM320_DAILYBARS_DB
미지정 시 메인 레포 stocks.db(백테스트 +11.80% SoT) 우선 — stale cron DB false-fidelity 차단.

usage:
  python3 -m scripts.pm320.strategy_profiles.batch_generate \
      [--profiles p1,p2] [--dates D1,D2] [--from YYYY-MM-DD --to YYYY-MM-DD]
  --profiles 미지정 시 profiles.json 전체. --dates/--from/--to 미지정 시 picks 디렉토리 전체.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_PM320 = REPO_ROOT / "scripts" / "pm320"
PICKS_DIR = REPO_ROOT / "projects" / "pm320" / "data" / "daily" / "picks"
MATRIX_DIR = REPO_ROOT / "projects" / "pm320" / "research" / "profile_matrix"

PROFILES_JSON = Path(__file__).resolve().parent / "profiles.json"


def _load_script(name: str, path: Path) -> Any:
    """worktree-안전: build_card_history / send_kakao_message 를 fresh import."""
    for m in list(sys.modules):
        if m == name or m.startswith("strategy_profiles"):
            del sys.modules[m]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _all_profiles() -> list[str]:
    doc = json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    return list((doc.get("profiles") or {}).keys())


def _pick_dates() -> list[str]:
    return sorted(
        p.stem
        for p in PICKS_DIR.glob("*.json")
        if len(p.stem) == 10 and p.stem[4] == "-"
    )


def _date_range(d_from: str, d_to: str) -> list[str]:
    s = datetime.strptime(d_from, "%Y-%m-%d").date()
    e = datetime.strptime(d_to, "%Y-%m-%d").date()
    out, cur = [], s
    while cur <= e:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def generate_profile(profile_id: str, dates: list[str]) -> dict[str, Any]:
    """단일 프로파일 배치 생성. cards + kakao 샘플 write + 집계 반환."""
    os.environ["M1S_PM320_PROFILE"] = profile_id
    os.environ["M1S_HISTORY_NO_SYNC"] = "1"  # push 0건 (라이브 무touch)
    sys.path.insert(0, str(SCRIPTS_PM320))

    bch = _load_script("build_card_history", SCRIPTS_PM320 / "build_card_history.py")
    skm = _load_script("send_kakao_message", SCRIPTS_PM320 / "send_kakao_message.py")
    bch.PICKS_DIR = PICKS_DIR
    skm.PICKS_DIR = PICKS_DIR

    out_dir = MATRIX_DIR / profile_id
    (out_dir / "cards").mkdir(parents=True, exist_ok=True)
    (out_dir / "kakao").mkdir(parents=True, exist_ok=True)

    agg = {
        "profile_id": profile_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": 0,
        "pick_days": 0,
        "hold_days": 0,
        "taken_profit": 0,
        "expired_gain": 0,
        "expired_loss": 0,
        "running": 0,
    }

    for d in dates:
        history = bch.build_history(d)
        if history is None:
            continue
        agg["days"] += 1
        (out_dir / "cards" / f"{d}.json").write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        picked = history.get("picked_code")
        if picked:
            agg["pick_days"] += 1
            # picked 종목 카드의 current_state 집계 + 카톡 샘플
            for s in history.get("stocks", []):
                if s.get("code") != picked:
                    continue
                state = s.get("pm320_pick", {}).get("current_state")
                if state in agg:
                    agg[state] += 1
                break
            picks = json.loads((PICKS_DIR / f"{d}.json").read_text(encoding="utf-8"))
            p0 = skm.load_close_price(picked, d)
            text, _ = skm.build_message(picks, p0)
            (out_dir / "kakao" / f"{d}.txt").write_text(text, encoding="utf-8")
        else:
            agg["hold_days"] += 1

    (out_dir / "SUMMARY.json").write_text(
        json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sys.path.pop(0)
    return agg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PM320 전략 프로파일 배치 생성기")
    parser.add_argument("--profiles", help="콤마 구분 profile_id (미지정=전체)")
    parser.add_argument("--dates", help="콤마 구분 YYYY-MM-DD")
    parser.add_argument("--from", dest="d_from", help="범위 시작 YYYY-MM-DD")
    parser.add_argument("--to", dest="d_to", help="범위 끝 YYYY-MM-DD")
    args = parser.parse_args(argv)

    profiles = args.profiles.split(",") if args.profiles else _all_profiles()
    if args.dates:
        dates = args.dates.split(",")
    elif args.d_from and args.d_to:
        dates = _date_range(args.d_from, args.d_to)
    else:
        dates = _pick_dates()

    print(f"[batch_generate] profiles={profiles} dates={len(dates)}")
    for pid in profiles:
        agg = generate_profile(pid, dates)
        print(
            f"  {pid}: days={agg['days']} pick={agg['pick_days']} hold={agg['hold_days']} "
            f"익절={agg['taken_profit']} 만기상승={agg['expired_gain']} "
            f"만기하락={agg['expired_loss']} 보유중={agg['running']}"
        )
    print(f"[batch_generate] done → {MATRIX_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
