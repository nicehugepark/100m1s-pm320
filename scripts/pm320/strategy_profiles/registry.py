"""PM320 전략 룰 핸들러 레지스트리 — open-for-extension dispatch.

bear_filter / expiry_mode 같은 룰 축의 분기를 거대한 if/else 로 박지 않고 **핸들러
레지스트리**(dict)로 등록형 관리한다. 새 필터·새 만기 모드·미래의 신규 축이 와도
핸들러 1개를 register 하면 엔진은 무수정으로 동작한다.

설계 (대표 2026-06-09 "버전도 축도 무한히 늘릴 수 있는 프레임워크"):
  - 핸들러는 엔진 모듈(select_daily_pick / build_card_history)이 import 시 자기 로직을
    register 한다 (로직 co-location + dispatch open 양립, 순환 import 회피).
  - 레지스트리는 dict 보관 + 조회만. enum 유효성 = 핸들러 등록 여부로 판정 (loader 가 사용).
  - 새 룰 축 추가 시 = 새 레지스트리 인스턴스 1개 + 엔진 lookup 1줄 (스키마·loader 무수정).

사용 (엔진 모듈에서):
  from strategy_profiles.registry import BEAR_FILTERS

  @BEAR_FILTERS.register("abs3000")
  def _abs3000(ctx): ...

  fn = BEAR_FILTERS.get(profile["bear_filter"])
  result = fn(ctx)

레지스트리 인스턴스:
  BEAR_FILTERS   — 거래대금터진음봉 판정 핸들러 (축②)
  EXPIRY_MODES   — 만기 연장 판정 핸들러 (축④)
신규 축 = 본 파일에 Registry() 인스턴스 1개 추가.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T", bound=Callable[..., Any])


class Registry:
    """이름→핸들러 레지스트리. register 데코레이터 + get 조회 + 등록 키 열람.

    같은 이름 재등록 시 ValueError (silent override 방지 — 충돌은 명시적 실패).
    """

    def __init__(self, axis_name: str) -> None:
        self.axis_name = axis_name
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register(self, name: str) -> Callable[[T], T]:
        """핸들러 등록 데코레이터. @REGISTRY.register("name")."""

        def deco(fn: T) -> T:
            if name in self._handlers:
                raise ValueError(
                    f"{self.axis_name} 핸들러 '{name}' 중복 등록 "
                    f"(기존: {self._handlers[name].__name__})"
                )
            self._handlers[name] = fn
            return fn

        return deco

    def get(self, name: str) -> Callable[..., Any]:
        """등록 핸들러 조회. 미등록 시 ValueError (silent fallback 금지 — FLR-AGT-002)."""
        if name not in self._handlers:
            raise ValueError(
                f"{self.axis_name} 핸들러 '{name}' 미등록 "
                f"(등록됨: {sorted(self._handlers)})"
            )
        return self._handlers[name]

    def has(self, name: str) -> bool:
        return name in self._handlers

    def names(self) -> list[str]:
        return sorted(self._handlers)


# ── 룰 축별 레지스트리 인스턴스 (신규 축 = 여기 1개 추가) ─────────────────────
BEAR_FILTERS = Registry("bear_filter")  # 축② 거래대금터진음봉
EXPIRY_MODES = Registry("expiry_mode")  # 축④ 만기 연장
