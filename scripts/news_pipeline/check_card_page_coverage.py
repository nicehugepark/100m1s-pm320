"""카드 universe ↔ 종목페이지/OG 완결성 검사 (FLR-20260605-TEC-001 P1-1/P1-3 공통 모듈).

"라이브 부분상태 노출 클래스" 봉쇄용 불변식(invariant) 단언:
  카드 N종목(`data/interpreted/stock-{date}.json` stocks[].code)이 가리키는
  종목페이지(`news/stock/{date}/{code}.html`) + OG(`og/news/stock/{date}/{code}.png`)
  가 worktree 에 **전부 존재** 해야 한다.

공통 모듈로 분리한 이유 (FLR-20260406-TEC-001 recurring — 한쪽-fix 누락 회피):
  - P1-3 (생성단, pipeline.sh): generate_stock_og 직후 누락 catch → 즉시 재생성.
  - P1-1 (배포단, kiwoom_cron.sh): 끝 push commit 직전 단언 → 부족 시 재생성/차단.
  두 게이트가 동일 판정 로직을 공유 → 한쪽만 고쳐서 갈라지는 사고 봉쇄.

추정 금지 (FLR-AGT-002): 실파일 존재만 검사. 가짜 통과 없음.

CLI:
  python -m scripts.news_pipeline.check_card_page_coverage [YYYY-MM-DD]
출력 (stdout, 후속 shell 파싱용):
  MISSING=<comma codes>   (카드에 있으나 페이지/OG 누락 — 빈 문자열이면 완결)
  CARD_N=<int> PAGE_OK_N=<int>
exit code:
  0 = 완결 (누락 0), 1 = 누락 존재, 2 = 카드 universe 부재 (검사 불가/스킵).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from .config import HOMEPAGE as HOMEPAGE_DIR

INTERPRETED_DIR = HOMEPAGE_DIR / "data" / "interpreted"
OG_BASE_DIR = HOMEPAGE_DIR / "og" / "news" / "stock"
STOCK_HTML_BASE_DIR = HOMEPAGE_DIR / "news" / "stock"


def card_codes_for_date(date_str: str) -> list[str]:
    """카드 universe = interpreted stock-{date}.json stocks[].code (순서/중복 보존 X)."""
    jf = INTERPRETED_DIR / f"stock-{date_str}.json"
    if not jf.exists():
        return []
    try:
        data = json.loads(jf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    seen: set[str] = set()
    codes: list[str] = []
    for s in data.get("stocks") or []:
        c = s.get("code")
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    return codes


def missing_codes(date_str: str) -> tuple[list[str], int, int]:
    """카드 코드 중 OG PNG 또는 종목 HTML 페이지가 누락된 코드 목록.

    returns (missing_codes, card_n, page_ok_n).
    card_n == 0 이면 카드 universe 부재 (검사 대상 없음).
    """
    codes = card_codes_for_date(date_str)
    if not codes:
        return [], 0, 0
    og_dir = OG_BASE_DIR / date_str
    html_dir = STOCK_HTML_BASE_DIR / date_str
    missing: list[str] = []
    ok = 0
    for c in codes:
        png = og_dir / f"{c}.png"
        html = html_dir / f"{c}.html"
        # 둘 다 존재 + PNG 비어있지 않음(0바이트 방지) 이어야 완결.
        if html.exists() and png.exists() and png.stat().st_size > 0:
            ok += 1
        else:
            missing.append(c)
    return missing, len(codes), ok


def main() -> int:
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    missing, card_n, ok_n = missing_codes(date_str)
    print(f"MISSING={','.join(missing)}")
    print(f"CARD_N={card_n} PAGE_OK_N={ok_n}")
    if card_n == 0:
        return 2
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
