#!/usr/bin/env python3
"""카카오 OAuth 재인가 반자동 헬퍼 — rotate 사슬 단절(KOE322) 시 복구 도구.

⚠ 반자동 도구: 사람(대표)의 브라우저 동의 클릭을 전제로 한다.
   동의 클릭 자동화(브라우저 조작)는 포함하지 않는다 (2026-07-06 대표 지시 범위).

flow:
  (a) localhost:{port}/callback 에 일회성 HTTP 리스너 기동
  (b) 인가 URL stdout 출력 + `open` 으로 기본 브라우저에 오픈
  (c) 대표가 브라우저에서 동의 클릭 → redirect 로 code 자동 회수
  (d) code → 토큰 교환 (grant_type=authorization_code, kakao_token 공용 모듈)
  (e) .env KAKAO_REFRESH_TOKEN atomic 갱신 + 만료 대장(issued/expires/status) 갱신

redirect_uri = http://localhost:3000/callback (2026-07-06 18:42 재인가 실사용 검증 값 —
카카오 developers 앱에 등록되어 있어야 함).

rules:
  - 키·토큰 실물 값 stdout/stderr 절대 0건 (인가 URL 의 client_id 는 인가 절차상 필수 노출)
  - .env / 만료 대장 rewrite = atomic (kakao_token 공용 모듈)

exit: 0=재인가 완료 / 1=사용자 거부·code 미회수(timeout) / 2=설정 오류 / 3=교환·반영 실패

usage:
  python3 scripts/automation/kakao_reauth_helper.py
      [--env-file PATH] [--ledger PATH] [--port 3000] [--timeout 300] [--no-open]

doc_id: feat(infra,P0,kakao-token-rotate,DSN-20260507-003-arch-infra,FLR-20260706-PRC-001)
generated: 2026-07-06
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_LEDGER = REPO_ROOT / "records" / "ops" / "credentials-expiry.json"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "pm320"))
import kakao_token as kt  # noqa: E402


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[kakao_reauth] {ts} {msg}", flush=True)


class _CallbackHandler(BaseHTTPRequestHandler):
    """GET /callback?code=... 일회성 수신 핸들러 — code/error 를 서버 객체에 저장."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 규약)
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        srv = self.server
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        srv.oauth_code = (qs.get("code") or [None])[0]  # type: ignore[attr-defined]
        srv.oauth_error = (qs.get("error") or [None])[0]  # type: ignore[attr-defined]
        ok = srv.oauth_code is not None  # type: ignore[attr-defined]
        html = (
            "<html><body style='font-family:sans-serif'><h2>"
            + (
                "✅ 인가 완료 — 터미널로 돌아가세요."
                if ok
                else "❌ 인가 실패/거부 — 터미널 확인."
            )
            + "</h2></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, fmt: str, *args: object) -> None:
        """기본 액세스 로그 억제 — query(code) 가 stderr 에 찍히는 것 차단."""


def wait_for_code(port: int, timeout: int) -> tuple[str | None, str | None]:
    """일회성 리스너 기동 → (code, error) 회수. timeout 시 (None, None)."""
    server = HTTPServer(("localhost", port), _CallbackHandler)
    server.oauth_code = None  # type: ignore[attr-defined]
    server.oauth_error = None  # type: ignore[attr-defined]
    server.timeout = timeout
    log(f"리스너 대기 중: http://localhost:{port}/callback (timeout {timeout}s)")
    try:
        # handle_request = 단일 요청 처리 후 반환 (일회성) — timeout 시 그냥 반환
        server.handle_request()
    finally:
        server.server_close()
    return server.oauth_code, server.oauth_error  # type: ignore[attr-defined]


def main() -> int:
    parser = argparse.ArgumentParser(description="카카오 OAuth 재인가 반자동 헬퍼")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--no-open", action="store_true", help="브라우저 자동 오픈 skip (URL 출력만)"
    )
    args = parser.parse_args()

    redirect_uri = f"http://localhost:{args.port}/callback"

    try:
        # 재인가에는 REST key + secret 만 필요 (기존 refresh_token 무효 상태 허용)
        env = kt.read_kakao_env(
            args.env_file, required=("KAKAO_REST_API_KEY", "KAKAO_CLIENT_SECRET")
        )
    except kt.KakaoTokenError as exc:
        log(f"FAIL: env read: {exc}")
        return 2

    auth_url = (
        kt.KAKAO_AUTHORIZE_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": env["KAKAO_REST_API_KEY"],
                "redirect_uri": redirect_uri,
            }
        )
    )
    print("\n브라우저에서 카카오 동의를 눌러주세요. 인가 URL:", flush=True)
    print(f"  {auth_url}\n", flush=True)
    if not args.no_open:
        try:
            subprocess.run(["open", auth_url], check=False, timeout=10)
        except Exception as exc:
            log(
                f"WARN: 브라우저 자동 오픈 실패 ({type(exc).__name__}) — 위 URL 수동 접속"
            )

    code, error = wait_for_code(args.port, args.timeout)
    if error:
        log(f"FAIL: 인가 거부/오류 (error={error})")
        return 1
    if not code:
        log(f"FAIL: {args.timeout}s 내 code 미회수 (timeout) — 재실행 필요")
        return 1
    log("OK: 인가 code 회수 — 토큰 교환 중")

    try:
        body = kt.exchange_authorization_code(env, code, redirect_uri)
    except kt.KakaoTokenError as exc:
        log(f"FAIL: 토큰 교환 실패: {exc}")
        return 3

    try:
        kt.write_env_refresh_token(args.env_file, body["refresh_token"])
    except kt.KakaoTokenError as exc:
        log(f"FAIL: .env 반영 실패 (critical): {exc}")
        return 3
    issued, expires = kt.refresh_expiry_dates(
        expires_in_sec=body.get("refresh_token_expires_in")
    )
    try:
        kt.update_ledger_kakao(
            args.ledger,
            status="active",
            issued=issued,
            expires=expires,
            note=f"{issued} kakao_reauth_helper 재인가 (대표 동의 클릭)",
        )
    except kt.KakaoTokenError as exc:
        log(f"WARN: 만료 대장 갱신 실패: {exc}")
    log(f"DONE: 재인가 완료 — .env + 만료 대장 갱신 (만료 {expires})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
