#!/usr/bin/env python3
"""카카오 토큰 공용 모듈 — refresh·.env atomic 갱신·만료 대장 갱신 단일 SSOT.

send_kakao_message.py 의 검증된 로직(read_env / write_env_refresh_token /
refresh_access_token)을 verbatim 추출·공용화 (FLR-20260406-TEC-001 재발 방지 —
한쪽 fix·다른 쪽 누락 봉쇄). 신규 발명 0.

소비처:
  - scripts/pm320/send_kakao_message.py (15:20 카톡 push — thin wrapper 경유)
  - scripts/automation/kakao_token_rotate.py (일일 선제 rotate, 08:10 launchd)
  - scripts/automation/kakao_reauth_helper.py (사슬 단절 시 반자동 재인가)

rules:
  - 키·토큰 실물 값은 로그·예외 메시지·stdout 에 절대 포함 금지 (rules/security.md §2)
  - .env rewrite 는 tempfile + os.replace atomic (race 봉쇄)
  - 만료 대장 SSOT: records/ops/credentials-expiry.json (값 등재 금지 — 날짜·status 만)

doc_id: feat(infra,P0,kakao-token-rotate,DSN-20260507-003-arch-infra,FLR-20260706-PRC-001)
generated: 2026-07-06 (대표 지시 "앞으로 스스로 카카오 토큰을 갱신해줘")
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
LEDGER_KAKAO_ID = "kakao-refresh-token"
REFRESH_TOKEN_LIFETIME_DAYS = 60  # 카카오 refresh_token 유효 2개월 (발급일+60일)

REQUIRED_ENV_KEYS = (
    "KAKAO_REST_API_KEY",
    "KAKAO_CLIENT_SECRET",
    "KAKAO_REFRESH_TOKEN",
)


class KakaoTokenError(Exception):
    """토큰 처리 일반 오류 (네트워크·파일·파싱). 메시지에 토큰 값 포함 금지."""


class KakaoAuthError(KakaoTokenError):
    """카카오 4xx 인증 오류 — 재인가 필요 신호 (KOE322 등)."""

    def __init__(self, status: int, error_code: str, description: str) -> None:
        self.status = status
        self.error_code = error_code
        self.description = description
        super().__init__(f"HTTP {status} {error_code}: {description}")


def read_kakao_env(
    env_path: Path, required: tuple[str, ...] = REQUIRED_ENV_KEYS
) -> dict[str, str]:
    """.env 파싱 — KAKAO_* 만 추출 (send_kakao_message.read_env verbatim).

    raises KakaoTokenError: .env 부재 또는 required 키 누락 (키 이름만 노출).
    """
    if not env_path.exists():
        raise KakaoTokenError(f".env not found: {env_path}")
    env: dict[str, str] = {}
    with env_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k.startswith("KAKAO_"):
                env[k] = v
    missing = [k for k in required if k not in env]
    if missing:
        raise KakaoTokenError(f"missing env vars: {missing}")
    return env


def write_env_refresh_token(env_path: Path, new_refresh_token: str) -> None:
    """새 refresh_token .env atomic rewrite (send_kakao_message verbatim).

    raises KakaoTokenError: .env 부재 또는 쓰기 실패 (예외 타입명만 노출).
    """
    if not env_path.exists():
        raise KakaoTokenError(f".env not found for rewrite: {env_path}")
    with env_path.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith("KAKAO_REFRESH_TOKEN="):
            lines[i] = f"KAKAO_REFRESH_TOKEN={new_refresh_token}\n"
            found = True
            break
    if not found:
        lines.append(f"KAKAO_REFRESH_TOKEN={new_refresh_token}\n")
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".env.tmp.", dir=str(env_path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(tmp_path, env_path)
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise KakaoTokenError(f".env rewrite failed: {type(exc).__name__}") from exc


def _post_token(form: dict[str, str], timeout: int = 15) -> dict[str, Any]:
    """kauth.kakao.com/oauth/token POST 공통 (grant_type 별 form 주입).

    raises KakaoAuthError(4xx) / KakaoTokenError(그 외).
    응답·예외에 토큰 실물 값 미포함 (에러 body 는 error_code/description 만 추출).
    """
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(KAKAO_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_code, description = "unknown", ""
        try:
            err = json.loads(exc.read().decode("utf-8"))
            error_code = str(err.get("error_code") or err.get("error") or "unknown")
            description = str(err.get("error_description") or "")[:200]
        except Exception:
            pass
        if 400 <= exc.code < 500:
            raise KakaoAuthError(exc.code, error_code, description) from exc
        raise KakaoTokenError(f"HTTP {exc.code} {error_code}") from exc
    except KakaoTokenError:
        raise
    except Exception as exc:
        raise KakaoTokenError(f"token request failed: {type(exc).__name__}") from exc


def request_token_refresh(env: dict[str, str], timeout: int = 15) -> dict[str, Any]:
    """refresh_token → access_token 갱신 (send_kakao_message spec verbatim).

    kakao spec (developers.kakao.com/docs/latest/ko/kakaologin/rest-api#refresh-token):
      grant_type=refresh_token & client_id & refresh_token & client_secret
    응답 schema:
      { "access_token": str, "token_type": "bearer", "refresh_token"?: str,
        "expires_in": int, "refresh_token_expires_in"?: int }
    refresh_token 응답 부재 = 기존 refresh_token 유효 유지 (만료 1개월 전부터 재발급).
    """
    body = _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": env["KAKAO_REST_API_KEY"],
            "client_secret": env["KAKAO_CLIENT_SECRET"],
            "refresh_token": env["KAKAO_REFRESH_TOKEN"],
        },
        timeout=timeout,
    )
    if not body.get("access_token"):
        raise KakaoTokenError("token refresh response missing access_token")
    return body


def exchange_authorization_code(
    env: dict[str, str], code: str, redirect_uri: str, timeout: int = 15
) -> dict[str, Any]:
    """인가 code → 토큰 교환 (재인가 헬퍼 전용, grant_type=authorization_code)."""
    body = _post_token(
        {
            "grant_type": "authorization_code",
            "client_id": env["KAKAO_REST_API_KEY"],
            "client_secret": env["KAKAO_CLIENT_SECRET"],
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=timeout,
    )
    if not body.get("refresh_token"):
        raise KakaoTokenError("authorization_code response missing refresh_token")
    return body


def update_ledger_kakao(
    ledger_path: Path,
    *,
    status: str | None = None,
    issued: str | None = None,
    expires: str | None = None,
    note: str | None = None,
) -> bool:
    """만료 대장(credentials-expiry.json) 카카오 항목 갱신 — atomic write.

    None 필드는 보존. 성공 True / 대장·항목 부재 False (호출부 graceful 처리).
    값(토큰 실물) 등재 절대 금지 — 날짜·status·note 만.
    """
    if not ledger_path.exists():
        return False
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise KakaoTokenError(f"ledger parse failed: {type(exc).__name__}") from exc
    entry = next(
        (c for c in ledger.get("credentials", []) if c.get("id") == LEDGER_KAKAO_ID),
        None,
    )
    if entry is None:
        return False
    if status is not None:
        entry["status"] = status
    if issued is not None:
        entry["issued"] = issued
    if expires is not None:
        entry["expires"] = expires
    if note is not None:
        entry["note"] = note
    ledger["updated"] = date.today().isoformat()
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".ledger.tmp.", dir=str(ledger_path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, ledger_path)
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise KakaoTokenError(f"ledger write failed: {type(exc).__name__}") from exc
    return True


def refresh_expiry_dates(
    issued_on: date | None = None, expires_in_sec: int | None = None
) -> tuple[str, str]:
    """신규 refresh_token 발급 기준 (issued, expires) ISO 문자열 산출.

    expires_in_sec (응답 refresh_token_expires_in) 우선, 부재 시 발급일+60일.
    """
    d0 = issued_on or date.today()
    if expires_in_sec and expires_in_sec > 0:
        d1 = d0 + timedelta(days=expires_in_sec // 86400)
    else:
        d1 = d0 + timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS)
    return d0.isoformat(), d1.isoformat()
