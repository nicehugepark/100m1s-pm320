"""REQ-028 W4 — Nightly recovery batch (23:00 KST).

latest.json ∪ 상한가 union 종목 중 stock_themes 0건 종목을 강제 재해석.
W2 industry_seeds + W3 structural + W5 togusa_missed 흐름을 순차 적용.

SPEC-001 §VII.3 (REQ-028 추가):
- 23:00 KST nightly recovery batch — 빈 themes 종목 강제 재해석.

Q-20260511-FIX-A (P0, dev-audit-v2 root cause A):
- 기존: latest.json (조건검색 30종목)만 fan-out
- 결함: build_daily 상한가 union (+N건) 미해석 → themes=[] 잔존
- 정정: interpret_loop.load_fanout_codes() 재사용 (latest.json ∪ 상한가 union)

비판 가드 (개발팀):
- N종목 전수 LLM 호출 회피 — stock_themes count == 0 필터로 진입 제한
- W3는 W2 결과 0건 시에만 실행 (interpret() 내부 가드 정합)
- W5는 W2+W3 결과 모두 0건 시에만 실행
- LLM 콜 실측 + cost 로그 출력 (cost 누적 추적)
"""

from __future__ import annotations

from datetime import datetime

from .db import connect
from .interpret_loop import load_fanout_codes
from .interpret_stocks import (
    _apply_industry_seeds,
    _interpret_structural,
    togusa_check_missed,
)


def _load_latest_codes(target_date: str | None = None) -> list[str]:
    """fan-out 종목 코드 = latest.json ∪ 상한가 union.

    Q-20260511-FIX-A: interpret_loop.load_fanout_codes() 단일 출처 재사용.
    """
    return load_fanout_codes(target_date)


def _stock_themes_count(code: str) -> int:
    """종목별 stock_themes 누적 건수 조회."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM stock_themes WHERE stock_code=?",
            (code,),
        ).fetchone()
        return row["n"] if row else 0


def _stock_meta(code: str) -> tuple[str, str]:
    """종목 industry/sector 조회. 없으면 ('미분류', '')."""
    with connect() as conn:
        row = conn.execute(
            "SELECT industry, sector FROM stocks WHERE code=?", (code,)
        ).fetchone()
    if not row:
        return "미분류", ""
    return (row["industry"] or "미분류", row["sector"] or "")


def nightly_recovery(target_date: str | None = None) -> dict:
    """REQ-028 W4 — 빈 themes 종목 강제 재해석.

    Returns:
        {
            'total': 50,
            'empty': N,         # stock_themes 0건이었던 종목 수
            'recovered_w2': N,  # industry_seeds 부착 종목 수
            'recovered_w3': N,  # structural LLM 부착 종목 수
            'recovered_w5': N,  # togusa_missed 부착 종목 수
            'still_empty': N,   # 모든 패스 후에도 빈 종목 수
        }
    """
    snap_date = target_date or datetime.now().strftime("%Y-%m-%d")
    codes = _load_latest_codes(snap_date)
    if not codes:
        return {"total": 0, "empty": 0}

    stats = {
        "total": len(codes),
        "empty": 0,
        "recovered_w2": 0,
        "recovered_w3": 0,
        "recovered_w5": 0,
        "still_empty": 0,
    }

    for code in codes:
        before = _stock_themes_count(code)
        if before > 0:
            continue  # 이미 테마 부착된 종목 스킵
        stats["empty"] += 1
        industry, sector = _stock_meta(code)

        # W2 — industry_seeds 직매핑 (LLM 미경유)
        seed = _apply_industry_seeds(code, industry, snap_date)
        if seed:
            stats["recovered_w2"] += 1
            continue  # W3 스킵 (LLM 콜 절감)

        # W3 — structural LLM 폴백
        struct = _interpret_structural(code, industry, sector, snap_date)
        if struct:
            stats["recovered_w3"] += 1
            continue

        # W5 — togusa missed 패스 (W2+W3 모두 빈 경우)
        missed = togusa_check_missed(code, industry, sector, snap_date)
        if missed:
            stats["recovered_w5"] += 1
            continue

        stats["still_empty"] += 1

    print(
        f"[recovery] total={stats['total']} empty={stats['empty']} "
        f"W2={stats['recovered_w2']} W3={stats['recovered_w3']} "
        f"W5={stats['recovered_w5']} still_empty={stats['still_empty']}"
    )
    return stats


if __name__ == "__main__":
    nightly_recovery()
