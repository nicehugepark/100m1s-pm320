"""우선주 ↔ 본주 매핑 + 우선주 관점 재해석 컨텍스트.

[배경] 우선주(예: LG전자우 066575)는 자체 뉴스가 거의 없거나(또는 전부 로봇 기사)
본주(LG전자 066570) 뉴스가 종목명 정확 매칭에서 누락돼 뉴스 섹션이 빈다.
삼성전자우(005935)처럼 이미 우선주 고유 해석("괴리율 축소·배당 매력")이 있는 케이스는
덮지 않는다.

[핵심 원칙 — 대표 통찰 2026-05-28]
본주 뉴스를 *단순 복사*하지 않는다. 우선주는 본주 뉴스를 **참고**하되
**우선주 관점(괴리율 축소·배당 매력·의결권 없음·본주 대비 할인)으로 재해석**한다.

[매핑 규칙 — 한국 우선주 코드 관습]
- 구형 우선주(1우): 본주 코드 끝자리 0 → 5  (066570 → 066575).
- 신형 우선주(2우B/3우B 등): 본주 코드 끝자리 0 → 7  (005380 → 005387).
- 따라서 우선주 코드 끝자리(5 또는 7)를 0으로 치환 = 본주 코드 후보.
- 종목명에 '우' 글자 포함(우/우B/2우B 등).
- 코드 규칙(끝자리 5/7) + 이름 규칙('우' 포함) + 본주 실재 **3중 교차검증**으로
  오매핑 방지. 예: 성우(458650), 이오플로우(294090), 에코글로우(159910)는 이름이
  '우'/'로우'로 끝나지만 끝자리가 0이라 코드 규칙에서 배제 → 우선주 아님.

[안전]
- 가격·등락률·거래대금은 우선주 자체값 유지. 뉴스 해석만 우선주 관점.
- DB에 본주 뉴스를 우선주 코드로 무차별 복사하지 않는다. interpret 시점에 본주 뉴스를
  후보로 메모리에서 합치고, 우선주 관점으로 재해석된 결과만 우선주 행으로 적재한다.

[FLR 참조]
- FLR-AGT-002 (거짓 충실성): 본주 해석을 우선주 해석인 척 복사 금지 — 재해석 강제.
- FLR-20260409-TEC-001 (사명 부분문자열 오탐): 코드+이름+본주실재 3중 교차로 오매핑 방지.
"""

from __future__ import annotations

from .db import connect

# 우선주 코드 끝자리 → 본주 끝자리(0). 5=구형 1우, 7=신형 2우B/3우B.
_PREF_LAST_DIGITS = frozenset(("5", "7"))


def base_stock_code(code: str, conn=None) -> str | None:
    """우선주이면 본주 코드 반환, 아니면 None (3중 교차검증).

    교차검증:
      (1) 코드 규칙: 6자리 숫자 + 끝자리 5 또는 7 → 끝자리 0 치환 = 본주 후보.
      (2) 이름 규칙: 종목명에 '우' 글자 포함 (우/우B/2우B 등).
      (3) 본주 실재: 본주 후보가 stocks 테이블에 존재.
    셋 다 충족해야 우선주로 인정.

    Args:
        code: 종목 코드.
        conn: sqlite connection (테스트/재사용 주입). None이면 새 connect().

    Returns:
        본주 코드 (str) | None (우선주 아님 또는 본주 부재).
    """
    # (1) 코드 규칙
    if not code or not code.isdigit() or len(code) != 6:
        return None
    if code[-1] not in _PREF_LAST_DIGITS:
        return None
    base_candidate = code[:-1] + "0"

    own = conn is None
    if own:
        conn = connect()
    try:
        # (2) 이름 규칙 — '우' 글자 포함
        name_row = conn.execute(
            "SELECT name FROM stocks WHERE code=?", (code,)
        ).fetchone()
        if not name_row:
            return None
        name = name_row["name"] if hasattr(name_row, "keys") else name_row[0]
        if not name or "우" not in name:
            return None
        # (3) 본주 실재
        base_row = conn.execute(
            "SELECT code FROM stocks WHERE code=?", (base_candidate,)
        ).fetchone()
        if not base_row:
            return None
        return base_candidate
    finally:
        if own:
            conn.close()


def get_base_code(code: str, name: str | None = None) -> str | None:
    """우선주이면 본주 코드 반환, 아니면 None.

    base_stock_code의 얇은 래퍼 (호출부 호환). name 인자는 시그니처 호환용 (미사용 —
    base_stock_code가 DB에서 직접 이름 검증).
    """
    return base_stock_code(code)


def is_preferred(code: str) -> bool:
    """우선주 여부 (본주 매핑 가능 = 우선주)."""
    return base_stock_code(code) is not None


# 우선주 관점 재해석 컨텍스트 — interpret 프롬프트 prepend 블록.
# 본주 뉴스를 우선주에 그대로 옮기지 말고 우선주에 미치는 영향으로 변환하도록 강제.
PREFERRED_CONTEXT_TEMPLATE = """# [우선주 컨텍스트 — 매우 중요]
이 종목은 **우선주**({pref_name} {pref_code})다. 본주는 {base_name}({base_code}).
아래 뉴스에는 본주({base_name}) 뉴스가 후보로 포함될 수 있다.

본주 이슈를 **그대로 옮기지 말고**, 반드시 **우선주 관점으로 재해석**하라:
- 의결권이 없는 대신 배당 우선권이 있다.
- 통상 본주 대비 할인(괴리) 거래된다. 본주 재평가·강세는 괴리율 축소 기대로 이어진다.
- causal_chain은 "본주 이슈 → 우선주에 미치는 영향(괴리율 축소·배당 매력·할인 메리트)"
  형태로 작성한다. 본주 해석을 복사하면 안 된다.
- 본주 호재가 명확하면 우선주도 동반 강세이나, 그 경로(괴리·배당)를 명시하라.
예: "{base_name} 보통주 재평가 기대 → 의결권 없는 우선주의 보통주 대비 할인 괴리율
축소 기대 → 시세차익·배당 양면 매력."

"""


def build_preferred_context(
    pref_code: str, pref_name: str, base_code: str, base_name: str
) -> str:
    """우선주 프롬프트 컨텍스트 블록 생성."""
    return PREFERRED_CONTEXT_TEMPLATE.format(
        pref_code=pref_code,
        pref_name=pref_name,
        base_code=base_code,
        base_name=base_name,
    )
