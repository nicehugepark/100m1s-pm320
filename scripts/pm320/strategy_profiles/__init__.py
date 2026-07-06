"""PM320 전략 프로파일 패키지 — profiles.json SSOT + loader + 핸들러 레지스트리.

open-for-extension: 새 프로파일 = profiles.json 항목 추가 / 새 룰 핸들러 = registry 등록.
"""

from .loader import (
    Profile,
    bear_amount_min_won,
    load_active_profile,
    load_profile,
    min_trade_amount_won,
)
from .registry import BEAR_FILTERS, EXPIRY_MODES, Registry

__all__ = [
    "BEAR_FILTERS",
    "EXPIRY_MODES",
    "Profile",
    "Registry",
    "bear_amount_min_won",
    "load_active_profile",
    "load_profile",
    "min_trade_amount_won",
]
