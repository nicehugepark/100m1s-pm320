"""PM320 전략 프로파일 loader — profiles.json SSOT 단일 출처 (open-for-extension).

라이브 엔진(select_daily_pick.py / build_card_history.py / send_kakao_message.py)이
하드코딩 상수 대신 active 프로파일을 읽어 가변 룰 축을 결정한다.

가변 축 (현 4축, 미래 무한 확장 — 대표 2026-06-09 "축도 버전도 무한히"):
  ① min_trade_amount_eok  — 매수 게이트 (거래대금 하한, 억 단위)
  ② bear_filter           — 거래대금터진음봉 핸들러 키 (registry.BEAR_FILTERS 등록 핸들러명)
  ③ watering_weight       — 물타기 추가매수 비중 (1.0=1배, 2.0=2배)
  ④ expiry_mode           — 만기 연장 핸들러 키 (registry.EXPIRY_MODES 등록 핸들러명)

확장성 설계 (open schema, 닫힌 enum 금지):
  - schema_version: 프로파일 스키마 버전. 미래 필드 추가 시 마이그레이션 경로.
  - 누락 필드 = SCHEMA_DEFAULTS 안전 default (기존 프로파일 안 깨짐).
  - unknown 필드 = graceful 보존 (무시·폭주 금지, 미래 축 선반영 가능).
  - enum(bear_filter/expiry_mode) 유효성은 닫힌 tuple 이 아니라 **registry 등록 핸들러
    존재 여부**로 dispatch 시점 판정 (새 핸들러 등록만으로 새 enum 허용 — loader 무수정).

검증 (FLR-AGT-002 거짓 충실성 차단): 구조 필수 키 결측 시 즉시 ValueError. 미등록
핸들러 키는 dispatch 시점(registry.get)에 ValueError — silent fallback 금지.

active 지정: profiles.json["active"] = profile_id. switch.py 가 이 값만 변경한다.
env M1S_PM320_PROFILE 지정 시 그 profile_id 를 강제 (배치 생성기/회귀 테스트용,
active 무시). 라이브 cron 은 env 미지정 → active 사용.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROFILES_JSON = Path(__file__).resolve().parent / "profiles.json"

# 현재 스키마 버전. 미래 필드 추가/의미 변경 시 +1 + _migrate() 분기 추가.
CURRENT_SCHEMA_VERSION = 1

# 구조 필수 키 (핸들러 키 + 가변축 — 모든 프로파일 의무). 미래 신규 축은
# SCHEMA_DEFAULTS 로 graceful default 부여하여 기존 프로파일 무손상 추가.
_REQUIRED_KEYS = (
    "min_trade_amount_eok",
    "bear_filter",
    "watering_weight",
    "expiry_mode",
    "forward_d_base",
    "forward_d_water",
)

# 누락 필드 안전 default (기존/구버전 프로파일이 새 필드 없이도 안 깨지게).
# 미래 신규 축 추가 시 여기에 보수적 default 1줄 추가 = 구 프로파일 자동 호환.
SCHEMA_DEFAULTS: dict[str, Any] = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "bear_amount_min_eok": None,  # abs3000 외 핸들러는 미사용
    "bear_recent_n": 10,  # rank5_10d 윈도우
    "bear_rank_max": 5,  # rank5_10d 순위
    "watering_weight_label": None,  # None 시 "첫 매수의 {w}배" 자동
}

_EOK = 10**8  # 1억원 = 100,000,000 원


class Profile(dict):
    """active 프로파일 dict wrapper. 키 접근 + 파생 단위 변환 헬퍼."""

    @property
    def profile_id(self) -> str:
        return self["_profile_id"]

    @property
    def min_trade_amount_won(self) -> int:
        return int(self["min_trade_amount_eok"]) * _EOK

    @property
    def bear_amount_min_won(self) -> int | None:
        """bear_filter == 'abs3000' 일 때만 의미. 그 외 None."""
        v = self.get("bear_amount_min_eok")
        return int(v) * _EOK if v is not None else None


def _migrate(p: dict[str, Any]) -> dict[str, Any]:
    """구버전 스키마 → 현재 스키마 마이그레이션 (미래 필드 변경 호환).

    schema_version 별 변환 분기. 현재 v1 단일 — 미래 v2+ 추가 시 여기 분기 1개 추가.
    원본 profiles.json 은 무수정 (in-memory 변환만).
    """
    ver = int(p.get("schema_version", 1))
    # 예: if ver < 2: p["new_field"] = derive_from_old(p); ver = 2
    p["schema_version"] = ver
    return p


def _fill_defaults(p: dict[str, Any]) -> dict[str, Any]:
    """누락 필드 안전 default 채움 (기존/구 프로파일 무손상). unknown 필드는 보존."""
    for k, v in SCHEMA_DEFAULTS.items():
        p.setdefault(k, v)
    return p


def _validate(profile_id: str, p: dict[str, Any]) -> None:
    """구조 검증 — 필수 키 결측 시 ValueError (silent fallback 금지).

    enum(bear_filter/expiry_mode) 유효성은 닫힌 tuple 이 아니라 dispatch 시점
    registry.get() 가 판정 (새 핸들러 등록만으로 새 enum 허용 — open-for-extension).
    여기선 구조 필수 키 존재 + 핸들러별 의존 필드만 본다.
    """
    missing = [k for k in _REQUIRED_KEYS if k not in p]
    if missing:
        raise ValueError(f"profile '{profile_id}' missing keys: {missing}")
    # 핸들러별 의존 필드 (abs3000 계열은 절대값 임계 필요). 핸들러 자체 유효성은 registry 가 판정.
    if p["bear_filter"] in {"abs3000", "abs3000_or_rank5_10d"} and p.get("bear_amount_min_eok") is None:
        raise ValueError(
            f"profile '{profile_id}' bear_filter=abs3000 requires bear_amount_min_eok"
        )


def _load_doc() -> dict[str, Any]:
    if not PROFILES_JSON.exists():
        raise FileNotFoundError(f"profiles.json not found: {PROFILES_JSON}")
    return json.loads(PROFILES_JSON.read_text(encoding="utf-8"))


def load_profile(profile_id: str) -> Profile:
    """지정 profile_id 로드 + 마이그레이션 + default 채움 + 구조 검증. 미존재 시 ValueError."""
    doc = _load_doc()
    profiles = doc.get("profiles") or {}
    if profile_id not in profiles:
        raise ValueError(
            f"profile '{profile_id}' not in profiles.json "
            f"(available: {sorted(profiles)})"
        )
    p = dict(profiles[profile_id])  # unknown 필드 보존 (graceful)
    p = _fill_defaults(_migrate(p))
    _validate(profile_id, p)
    p["_profile_id"] = profile_id
    return Profile(p)


def load_active_profile() -> Profile:
    """active 프로파일 로드. env M1S_PM320_PROFILE 지정 시 그 값을 강제 (active 무시).

    라이브 cron: env 미지정 → profiles.json["active"]. 회귀 테스트/배치 생성기:
    env 지정 → 특정 프로파일 강제 (active 변경 없이 비교).
    """
    forced = os.environ.get("M1S_PM320_PROFILE")
    if forced:
        return load_profile(forced)
    doc = _load_doc()
    active = doc.get("active")
    if not active:
        raise ValueError("profiles.json missing 'active' key")
    return load_profile(active)


def min_trade_amount_won(profile: Profile | None = None) -> int:
    """active(또는 지정) 프로파일 매수 게이트(원)."""
    p = profile or load_active_profile()
    return p.min_trade_amount_won


def bear_amount_min_won(profile: Profile | None = None) -> int | None:
    """active(또는 지정) 프로파일 거래대금터진음봉 절대값 임계(원). abs3000 외 None."""
    p = profile or load_active_profile()
    return p.bear_amount_min_won
