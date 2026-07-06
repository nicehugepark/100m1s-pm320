"""키움 REST 공통 클라이언트 — token 발급 + 얇은 POST wrapper (SSOT).

배경 (DSN-arch-pipeline):
  7개 collector (indices·credit·dailybars·intraday·limit_up 등) 가 각자
  `_get_token()` 를 거의 byte-identical 하게 복제하고 있었다 (5개 동일 본문,
  차이는 base_url/appkey/secretkey 변수값 + session 인자 유무 + 에러 메시지뿐).
  retry/페이징/rc5 분기/parsing 은 collector 마다 6변종으로 전부 다르므로
  **공통화 대상이 아니다** (통일 시 동작 변경 = 회귀). 따라서 본 모듈은
  **token 발급 + 얇은 POST wrapper 만** SSOT 로 흡수하고,
  retry/paging/_RUN_CACHE/frozen-skip/Session/rc5 분기는 collector 에 100% 유지한다.
  → 구조적 동작 보존 (보존 "증명" 부담 없이 미접촉으로 보장).

key-agnostic 설계:
  collector 가 자기 base_url/appkey/secretkey (live 또는 mock) 를 인자로 전달.
  본 모듈은 어떤 키 집합도 강제하지 않는다 (intraday=mock / dailybars=live 그대로).

flexible:
  session 인자 (intraday 의 requests.Session keep-alive 보존) 옵션 지원.
  본문 로직은 collector 별 기존 `_get_token()` 와 byte-identical (FLR-20260428-TEC-001
  한쪽 수정·다른 쪽 누락 동형 회귀 회피 — 동일 RuntimeError 문구·동일 timeout·동일 키).

관련 FLR:
  - FLR-20260526-JDG-001 (프로덕션 파이프라인 고착 + lead 추측 단정) — read-only
    token/POST 만 위임, 동작 추측 0.
  - FLR-20260406-TEC-001 (SSOT 비대칭 — 한쪽만 fix) — token helper SSOT 화로
    재발 면적 축소.
"""

from __future__ import annotations

import requests

TOKEN_TIMEOUT = 15


def get_token(
    base_url: str,
    appkey: str | None,
    secretkey: str | None,
    *,
    session: requests.Session | None = None,
    key_label: str = "KIWOOM_APPKEY/SECRETKEY",
    env_note: str = "",
) -> str:
    """키움 OAuth2 client_credentials 토큰 발급 (key-agnostic).

    Args:
        base_url: 키움 base URL (live=https://api.kiwoom.com / mock=https://mockapi.kiwoom.com).
                  collector 가 자기 env 로 결정한 값을 그대로 전달.
        appkey:    collector 의 appkey (live 또는 mock).
        secretkey: collector 의 secretkey.
        session:   requests.Session (keep-alive). None 이면 requests.post 직접 사용
                   (intraday 의 Session 보존용 옵션, 그 외 collector 는 None).
        key_label: 누락 시 RuntimeError 문구 (collector 별 기존 문구 보존,
                   예 credit="LIVE_APPKEY/SECRETKEY").
        env_note:  RuntimeError 문구에 덧붙일 부가 정보 (limit_up 의 "env={KIWOOM_ENV}" 보존).

    Returns:
        발급된 토큰 문자열.

    Raises:
        RuntimeError: 키 누락 / HTTP non-200 / token 부재 (기존 collector 와 동일 분기).
    """
    if not appkey or not secretkey:
        suffix = (
            f" ({env_note}, pm320/poc/.env 확인)"
            if env_note
            else " (pm320/poc/.env 확인)"
        )
        raise RuntimeError(f"{key_label} 누락{suffix}")
    poster = session.post if session is not None else requests.post
    r = poster(
        f"{base_url}/oauth2/token",
        json={
            "grant_type": "client_credentials",
            "appkey": appkey,
            "secretkey": secretkey,
        },
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=TOKEN_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"token http {r.status_code}: {r.text[:200]}")
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"token 발급 실패: {data}")
    return token
