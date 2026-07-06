#!/usr/bin/env python3
"""PM320 전략 프로파일 스위처 — profiles.json["active"] 만 변경.

라이브 배포 프로파일을 즉시 전환 (재백필 불요 — 선정 룰만 바뀌면 당일 picks 만
재생성, 과거 카드 history 는 그대로). 만기/물타기 축(③④) 변경 시는 batch_generate
또는 backfill 로 카드 history 재산출 필요 (switch 는 active 지정만 담당).

멀티세션 안전: active Edit 단일 진입점. 변경 후 60초 내 commit 의무(§11.29) — 본
스크립트는 active 만 변경하고 commit 은 호출자(lead/DevOps)가 수행 (publish 사전 확인 §6).

usage:
  python3 -m scripts.pm320.strategy_profiles.switch --to <profile_id>
  python3 -m scripts.pm320.strategy_profiles.switch --list
  python3 -m scripts.pm320.strategy_profiles.switch            # 현재 active 출력
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from .loader import PROFILES_JSON, load_profile


def _load_doc() -> dict:
    return json.loads(PROFILES_JSON.read_text(encoding="utf-8"))


def list_profiles() -> int:
    doc = _load_doc()
    active = doc.get("active")
    print(f"active: {active}")
    for pid, p in (doc.get("profiles") or {}).items():
        mark = " *" if pid == active else "  "
        ret = p.get("verified_return_pct")
        ret_s = f"{ret:+.2f}%" if ret is not None else "?"
        print(f"{mark} {pid}: {p.get('label', '')} (검증 {ret_s})")
    return 0


def switch_to(profile_id: str) -> int:
    # 존재·schema 검증 (미지원 프로파일로 전환 차단 — silent 깨짐 방지)
    try:
        load_profile(profile_id)
    except (ValueError, FileNotFoundError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    prev = _load_doc().get("active")
    if prev == profile_id:
        print(f"no-op: active already '{profile_id}'")
        return 0

    # active 값만 surgical 치환 (json.dumps 전체 재직렬화 시 11.80→11.8 등 cosmetic diff +
    # 키 순서 변동 방지). "active": "<prev>" 단일 라인만 교체 → 나머지 byte 무손상.
    raw = PROFILES_JSON.read_text(encoding="utf-8")
    pattern = re.compile(r'("active"\s*:\s*)"[^"]*"')
    new_raw, n = pattern.subn(rf'\g<1>"{profile_id}"', raw, count=1)
    if n != 1:
        print(
            "FAIL: 'active' 키 단일 치환 실패 (profiles.json 형식 점검)",
            file=sys.stderr,
        )
        return 2
    PROFILES_JSON.write_text(new_raw, encoding="utf-8")
    print(f"switched: active '{prev}' → '{profile_id}'")
    print("  ⚠ 60초 내 git commit 의무 (§11.29). 라이브 반영 = 다음 cron fire 부터.")
    print("  ⚠ 만기/물타기 축 변경 시 카드 history 재산출 필요 (backfill).")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PM320 전략 프로파일 스위처")
    parser.add_argument("--to", help="전환할 profile_id")
    parser.add_argument("--list", action="store_true", help="프로파일 목록 + active")
    args = parser.parse_args(argv)

    if args.list:
        return list_profiles()
    if args.to:
        return switch_to(args.to)
    # 인자 없으면 현재 active 출력
    print(_load_doc().get("active"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
