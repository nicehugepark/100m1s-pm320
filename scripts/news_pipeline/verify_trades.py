"""토구사(Togusa) -- 이시카와 결과 매매 관점 1차 검증.

규칙 기반으로 종목별 호재/악재 판정.
trading_principles_v1.md 기반 3가지 규칙:
  1. 거래대금 대장급 여부 (3000억 기준)
  2. 차트(등락률) vs 뉴스(causal_chain) 방향 동행 여부
  3. 테마 내 거래대금 순위 (대장 여부)
"""

import json
from datetime import datetime

from .config import pipeline_date
from .db import connect


def verify_stock(conn, code: str, target_date: str, all_amounts: list = None):
    """개별 종목 호재/악재 판정 (규칙 기반)."""
    rules = []

    # 1. 거래대금 기준 (백분위 기반 — 상위 10% pass / 30% neutral / else fail)
    dp = conn.execute(
        "SELECT trade_amount, change_pct FROM daily_picks WHERE stock_code=? AND date=? AND source='kiwoom'",
        (code, target_date),
    ).fetchone()
    if not dp:
        return None

    ta = dp["trade_amount"] or 0

    # 백분위 계산 (상위 N%)
    if all_amounts and ta > 0:
        rank = sum(1 for a in all_amounts if a >= ta)
        percentile = (rank / len(all_amounts)) * 100
    else:
        percentile = 100  # 데이터 없으면 최하위

    if percentile <= 10:  # 상위 10%
        rules.append(
            {
                "rule": "거래대금_대장급",
                "result": "pass",
                "detail": f"상위 {percentile:.0f}% ({ta / 1e8:.0f}억)",
            }
        )
    elif percentile <= 30:  # 상위 30%
        rules.append(
            {
                "rule": "거래대금_대장급",
                "result": "neutral",
                "detail": f"상위 {percentile:.0f}% ({ta / 1e8:.0f}억)",
            }
        )
    else:
        rules.append(
            {
                "rule": "거래대금_대장급",
                "result": "fail",
                "detail": f"상위 {percentile:.0f}% ({ta / 1e8:.0f}억)",
            }
        )

    # 2. 등락률 방향 vs causal_chain 방향
    pct = dp["change_pct"] or 0
    news = conn.execute(
        "SELECT causal_chain FROM news WHERE stock_code=? AND published_at>=? AND causal_chain IS NOT NULL AND causal_chain != '' LIMIT 1",
        (code, target_date),
    ).fetchone()
    if news and news["causal_chain"]:
        chain = news["causal_chain"]
        bull_words = ["성장", "증가", "확대", "수주", "흑자", "상향", "돌파", "급등"]
        bear_words = ["감소", "축소", "적자", "하향", "하락", "급감"]
        is_bull_news = any(w in chain for w in bull_words)
        is_bear_news = any(w in chain for w in bear_words)

        if pct > 0 and is_bull_news:
            rules.append(
                {"rule": "차트_뉴스_동행", "result": "pass", "detail": "호재+상승"}
            )
        elif pct < 0 and is_bear_news:
            rules.append(
                {"rule": "차트_뉴스_동행", "result": "pass", "detail": "악재+하락"}
            )
        elif pct > 0 and is_bear_news:
            rules.append(
                {
                    "rule": "차트_뉴스_동행",
                    "result": "warn",
                    "detail": "악재인데 상승 — 주의",
                }
            )
        elif pct < 0 and is_bull_news:
            rules.append(
                {
                    "rule": "차트_뉴스_동행",
                    "result": "warn",
                    "detail": "호재인데 하락 — 주의",
                }
            )

    # 3. 테마 내 대장 여부
    themes = conn.execute(
        "SELECT t.name, t.id FROM stock_themes st JOIN themes t ON st.theme_id=t.id WHERE st.stock_code=? AND t.is_active=1",
        (code,),
    ).fetchall()

    is_leader = False
    leader_rank = 99
    for theme in themes:
        rank_row = conn.execute(
            """SELECT stock_code, trade_amount FROM daily_picks
               WHERE date=? AND source='kiwoom' AND stock_code IN
               (SELECT stock_code FROM stock_themes WHERE theme_id=?)
               ORDER BY trade_amount DESC""",
            (target_date, theme["id"]),
        ).fetchall()
        for i, r in enumerate(rank_row):
            if r["stock_code"] == code:
                if i == 0:
                    is_leader = True
                leader_rank = min(leader_rank, i + 1)
                break

    if is_leader:
        rules.append({"rule": "테마_대장", "result": "pass", "detail": "거래대금 1위"})
    elif leader_rank <= 3:
        rules.append(
            {
                "rule": "테마_대장",
                "result": "neutral",
                "detail": f"거래대금 {leader_rank}위",
            }
        )
    else:
        rules.append(
            {
                "rule": "테마_대장",
                "result": "warn",
                "detail": f"거래대금 {leader_rank}위 — 대장 아님",
            }
        )

    # 종합 verdict
    fails = sum(1 for r in rules if r["result"] == "fail")
    warns = sum(1 for r in rules if r["result"] == "warn")
    passes = sum(1 for r in rules if r["result"] == "pass")
    neutrals = sum(1 for r in rules if r["result"] == "neutral")

    if fails >= 2:
        verdict = "strong_bear"
    elif fails >= 1 and warns >= 1:
        verdict = "weak_bear"
    elif passes >= 2:
        verdict = "strong_bull"
    elif passes >= 1 and neutrals >= 1:
        verdict = "weak_bull"
    else:
        verdict = "neutral"

    return {
        "code": code,
        "verdict": verdict,
        "rules": rules,
        "is_theme_leader": is_leader,
        "theme_leader_rank": leader_rank,
    }


def verify_daily(target_date=None):
    """일일 전체 검증."""
    if not target_date:
        target_date = pipeline_date()

    with connect() as conn:
        # 테이블 생성 (안전)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS togusa_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL, stock_code TEXT NOT NULL,
                verdict TEXT NOT NULL, rules_json TEXT NOT NULL,
                is_theme_leader INTEGER DEFAULT 0, theme_leader_rank INTEGER,
                source TEXT DEFAULT 'togusa', created_at TEXT NOT NULL,
                UNIQUE(date, stock_code)
            );
            """
        )

        codes = [
            r["stock_code"]
            for r in conn.execute(
                "SELECT DISTINCT stock_code FROM daily_picks WHERE date=? AND source='kiwoom'",
                (target_date,),
            ).fetchall()
        ]

        # 전체 거래대금 목록 (백분위 계산용, 내림차순)
        amounts = sorted(
            [
                r["trade_amount"]
                for r in conn.execute(
                    "SELECT trade_amount FROM daily_picks WHERE date=? AND source='kiwoom'",
                    (target_date,),
                ).fetchall()
                if r["trade_amount"]
            ],
            reverse=True,
        )

        results = []
        for code in codes:
            r = verify_stock(conn, code, target_date, amounts)
            if r:
                results.append(r)
                conn.execute(
                    """INSERT OR REPLACE INTO togusa_verdicts
                       (date, stock_code, verdict, rules_json, is_theme_leader, theme_leader_rank, source, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'togusa', ?)""",
                    (
                        target_date,
                        r["code"],
                        r["verdict"],
                        json.dumps(r["rules"], ensure_ascii=False),
                        1 if r["is_theme_leader"] else 0,
                        r["theme_leader_rank"],
                        datetime.now().isoformat(),
                    ),
                )
        conn.commit()

    verdicts = {}
    for r in results:
        v = r["verdict"]
        verdicts[v] = verdicts.get(v, 0) + 1
    print(f"[togusa] {target_date} {len(results)}종목 검증: {verdicts}")
    return results


if __name__ == "__main__":
    import sys

    td = sys.argv[1] if len(sys.argv) > 1 else None
    verify_daily(td)
