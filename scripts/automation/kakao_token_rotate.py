#!/usr/bin/env python3
"""카카오 refresh_token 일일 선제 rotate — 발송과 무관하게 토큰 자가 연장.

배경 (FLR-20260706-PRC-001 계열): 카카오 refresh_token rotate 는 발송 스크립트
(send_kakao_message.py) 의 refresh 호출에 편승해 왔음 → 발송이 장기 실패하면
rotate 사슬이 끊겨 기한 전 무효화(KOE322) → 15:20 카톡 알림 실패.
본 스크립트는 매일 1회 (launchd 08:10) refresh 를 선제 호출해 사슬을 유지한다.
(대표 직접 지시 2026-07-06 18:45 "앞으로 스스로 카카오 토큰을 갱신해줘")

flow:
  1. .env KAKAO_* 3종 read (kakao_token 공용 모듈 — 새 발명 0)
  2. POST kauth.kakao.com/oauth/token (grant_type=refresh_token)
  3-a. 응답에 새 refresh_token 포함 (만료 1개월 전부터 카카오가 재발급)
       → .env atomic rewrite + 만료 대장 issued/expires(발급일+60일)/status=active 갱신
  3-b. 새 refresh_token 없음 → "rotate 불필요, 유효 확인" (대장 status 만 active 보정)
  3-c. 4xx (KOE322 등) → stdout "카카오 재인가 필요" + 대장 status=invalid
       (credential_expiry_watch.sh 가 invalid 을 🔴 알람 → SessionStart 브리핑 노출)
  3-d. 5xx/네트워크 → 일시 장애로 간주, 대장 불변 (invalid 오판 금지) + exit 2

rules:
  - 키·토큰 실물 값 stdout/stderr/log 절대 0건 (rules/security.md §2)
  - .env / 만료 대장 rewrite = atomic (kakao_token 공용 모듈)
  - 테스트는 --env-file/--ledger 복사본 주입으로 (진짜 .env 훼손 금지)

exit: 0=정상(rotate 완료 또는 유효 확인) / 1=재인가 필요(4xx) /
      2=설정·네트워크 오류 / 3=신규 토큰 .env 반영 실패 (critical)

usage:
  python3 scripts/automation/kakao_token_rotate.py [--env-file PATH] [--ledger PATH]

doc_id: feat(infra,P0,kakao-token-rotate,DSN-20260507-003-arch-infra,FLR-20260706-PRC-001)
generated: 2026-07-06
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_LEDGER = REPO_ROOT / "records" / "ops" / "credentials-expiry.json"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "pm320"))
import kakao_token as kt  # noqa: E402


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[kakao_token_rotate] {ts} {msg}", flush=True)


def _ledger_update(ledger_path: Path, **kwargs: str) -> None:
    """대장 갱신 — 실패해도 rotate 본류는 유지 (graceful, 로그만)."""
    try:
        if kt.update_ledger_kakao(ledger_path, **kwargs):
            log(f"OK: 만료 대장 갱신 ({ledger_path.name}: {', '.join(kwargs)})")
        else:
            log(f"WARN: 만료 대장 항목 부재 — 갱신 skip ({ledger_path})")
    except kt.KakaoTokenError as exc:
        log(f"WARN: 만료 대장 갱신 실패: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="카카오 refresh_token 일일 선제 rotate"
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args()

    log(f"START: env={args.env_file} ledger={args.ledger.name}")

    try:
        env = kt.read_kakao_env(args.env_file)
    except kt.KakaoTokenError as exc:
        log(f"FAIL: env read: {exc}")
        return 2

    try:
        body = kt.request_token_refresh(env)
    except kt.KakaoAuthError as exc:
        # 4xx = refresh_token 무효 (KOE322 등) — 재인가 필요
        log(f"FAIL: refresh 거부 ({exc.error_code}, HTTP {exc.status})")
        print(
            "🔴 카카오 재인가 필요 — refresh_token 무효 "
            f"({exc.error_code}). 대표 실행: "
            "python3 scripts/automation/kakao_reauth_helper.py",
            flush=True,
        )
        _ledger_update(
            args.ledger,
            status="invalid",
            note=(
                f"{date.today().isoformat()} kakao_token_rotate 4xx "
                f"({exc.error_code}) — 재인가 필요 (kakao_reauth_helper.py)"
            ),
        )
        return 1
    except kt.KakaoTokenError as exc:
        # 5xx/네트워크 = 일시 장애 — 대장 불변 (invalid 오판 금지)
        log(f"WARN: 일시 장애 추정, 대장 불변 — {exc}")
        return 2

    new_rt = body.get("refresh_token")
    if not new_rt:
        # 만료 1개월 이상 남음 — 카카오가 재발급 안 함 = 정상
        log("OK: access_token 수신 — rotate 불필요, refresh_token 유효 확인")
        _ledger_update(args.ledger, status="active")
        return 0

    # 새 refresh_token 수신 (만료 1개월 전 구간) — .env 반영이 최우선 critical
    try:
        kt.write_env_refresh_token(args.env_file, new_rt)
    except kt.KakaoTokenError as exc:
        log(f"FAIL: 신규 refresh_token .env 반영 실패 (critical): {exc}")
        print(
            "🔴 카카오 신규 refresh_token .env 반영 실패 — 즉시 수동 확인 필요",
            flush=True,
        )
        return 3
    issued, expires = kt.refresh_expiry_dates(
        expires_in_sec=body.get("refresh_token_expires_in")
    )
    log(f"OK: 신규 refresh_token 수신 → .env atomic 반영 (만료 {expires})")
    _ledger_update(
        args.ledger,
        status="active",
        issued=issued,
        expires=expires,
        note=f"{issued} kakao_token_rotate 자동 rotate (선제 갱신)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
