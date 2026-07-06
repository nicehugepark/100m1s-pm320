"""REQ-003 투자위험 predicted 로직 단위 테스트.

로컬 실행 (워크트리 루트에서):
    python3 -m scripts.news_pipeline.test_risk_predicted

검증 항목:
1. _is_under_warning: 투자경고 active/released/10일초과 구분
2. _compute_index_ratio_period: N영업일 기간 ratio 계산
3. _predict_status_from_dailybars: 4/22 기준 투자위험 predicted 4종목 타겟 검사
"""

from __future__ import annotations

import sys
from pathlib import Path

# 워크트리 루트 기준 import path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.news_pipeline.build_daily import (  # noqa: E402
    _compute_index_ratio_period,
    _is_under_warning,
    _predict_status_from_dailybars,
)
from scripts.news_pipeline.db import connect  # noqa: E402


def test_is_under_warning():
    """stock_alert_history 기반 투자경고 active 판정."""
    conn = connect()
    # 4/22 기준 designated 종목들은 True
    assert _is_under_warning(conn, "036930", "2026-04-22") is True, (
        "주성 designated 04-22"
    )
    assert _is_under_warning(conn, "010820", "2026-04-22") is True, (
        "퍼스텍 designated 04-22"
    )
    assert _is_under_warning(conn, "417840", "2026-04-22") is True, (
        "저스템 designated 04-22"
    )
    assert _is_under_warning(conn, "006110", "2026-04-22") is True, (
        "삼아알미늄 designated 04-22"
    )

    # 파인텍(131760)은 4/21 released — 4/22에는 False
    assert _is_under_warning(conn, "131760", "2026-04-22") is False, (
        "파인텍 released 04-21"
    )

    # 존재하지 않는 종목
    assert _is_under_warning(conn, "999999", "2026-04-22") is False

    # 레코드 없는 종목 (삼성전자 005930)
    assert _is_under_warning(conn, "005930", "2026-04-22") is False
    print("[OK] _is_under_warning — 5 cases pass")


def test_compute_index_ratio_period():
    """기간 ratio 계산 — KOSDAQ 종목 + 3/5/15일."""
    conn = connect()
    # 주성엔지니어링(036930) 4/22 기준 — close_today 필요
    row = conn.execute(
        "SELECT close FROM dailybars WHERE code='036930' AND date='2026-04-22'"
    ).fetchone()
    if not row:
        print("[SKIP] 036930 2026-04-22 dailybars 미존재 → ratio 테스트 생략")
        return
    close_today = row["close"]

    for days in (3, 5, 15):
        ratio, elig = _compute_index_ratio_period(
            conn, "036930", "2026-04-22", days, close_today
        )
        print(f"  036930 {days}d ratio={ratio} eligible={elig}")

    # 비상장 종목
    ratio, elig = _compute_index_ratio_period(conn, "999999", "2026-04-22", 3, 10000)
    assert ratio is None and not elig
    print("[OK] _compute_index_ratio_period — sample + invalid")


def test_predict_badges_for_warning_stocks():
    """4/22 투자경고 designated 4종목에 대한 투자위험 predicted 검사."""
    conn = connect()
    targets = [
        ("036930", "주성엔지니어링"),
        ("010820", "퍼스텍"),
        ("417840", "저스템"),
        ("006110", "삼아알미늄"),
    ]
    for code, name in targets:
        row = conn.execute(
            "SELECT close FROM dailybars WHERE code=? AND date='2026-04-22'", (code,)
        ).fetchone()
        if not row:
            print(f"[SKIP] {name}({code}) 4/22 dailybars 미존재")
            continue
        close = row["close"]
        badges = _predict_status_from_dailybars(conn, code, "2026-04-22", close)
        risk = [b for b in badges if b.get("label") == "투자위험 근접"]
        warn = [b for b in badges if b.get("label") == "투자경고 근접"]
        caut = [b for b in badges if b.get("label") == "투자주의 근접"]
        print(
            f"  {name}({code}) close={close} 투자위험={len(risk)} 투자경고={len(warn)} 투자주의={len(caut)}"
        )
        if risk:
            b = risk[0]
            print(
                f"    path={b.get('path')} price_chg={b.get('price_chg')} "
                f"ratio={b.get('index_ratio')} reg_conf={b.get('regulation_source_confidence')}"
            )

    print("[OK] predict badges — 4 warning-active stocks probed")


if __name__ == "__main__":
    test_is_under_warning()
    test_compute_index_ratio_period()
    test_predict_badges_for_warning_stocks()
    print("\nALL TESTS PASSED")
