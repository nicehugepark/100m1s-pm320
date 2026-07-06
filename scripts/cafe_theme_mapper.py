#!/usr/bin/env python3
"""카페 테마 매핑 엔진 (2단계 a — staging 산출·읽기 전용).

입력(전부 읽기 전용):
  - cafe.db (1단계 산출, board_menu=994 테마맵)
  - stocks.db `stocks`(code·name) — 종목명→티커 마스터
  - theme_dictionary.json `canonical_themes[]` — canonical 테마 사전

산출:
  - data/cafe-staging/theme-overrides.json (신규)

서빙 DB·build_daily·theme_dictionary 절대 write 금지. 네트워크 0.
"""

import json
import os
import re
import sqlite3
from pathlib import Path

# S5 자립화 (DOC-20260707-REQ-001): 옛 homepage/메인 레포 절대경로 → pm320 레포 로컬 기반.
# REPO_ROOT = 이 파일(scripts/cafe_theme_mapper.py)의 조상 = pm320 레포 루트(parents[1]).
# DB·데이터 위치는 M1S_HOMEPAGE(config.py 정합, cron/serving worktree)를 우선 존중하되,
# 미설정 시 pm320 레포 루트로 자립 fallback (메인 레포 무의존).
REPO_ROOT = Path(__file__).resolve().parents[1]
_DATA_HOME = Path(os.environ.get("M1S_HOMEPAGE", str(REPO_ROOT)))

# 카페 스크레이퍼 작업 DB — pm320 레포 로컬(gitignore: scripts/cafe-scraper/cafe.db).
CAFE_DB = str(
    Path(
        os.environ.get(
            "M1S_CAFE_DB", str(REPO_ROOT / "scripts" / "cafe-scraper" / "cafe.db")
        )
    )
)
# 종목 마스터 DB — 데이터 홈(M1S_HOMEPAGE)/data/stocks.db.
STOCKS_DB = str(_DATA_HOME / "data" / "stocks.db")
# canonical 테마 사전 — pm320 레포에 이관된 로컬 사본.
THEME_DICT = str(REPO_ROOT / "scripts" / "news_pipeline" / "theme_dictionary.json")

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "cafe-staging"
OUT_FILE = OUT_DIR / "theme-overrides.json"
SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    # time 모듈 기반 — datetime.UTC(3.10+) 포맷터 강제 회피, 3.9 호환.
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize(s: str) -> str:
    """정규화: 공백·중점(·/ㆍ)·구분자 제거 + 소문자화. 종목명·테마명 매칭 공통."""
    if s is None:
        return ""
    s = s.strip().lower()
    # 공백, 가운뎃점, 슬래시, 하이픈, 앰퍼샌드 등 구분자 제거
    s = re.sub(r"[\s·ㆍ・\-/&\.,]+", "", s)
    return s


def build_stock_index(conn):
    """정규화 종목명 -> code. 동명이인 종목은 첫 매칭 우선(수집순)."""
    idx = {}
    for code, name in conn.execute("SELECT code, name FROM stocks"):
        key = normalize(name)
        if key and key not in idx:
            idx[key] = (code, name)
    return idx


def build_theme_index(canon):
    """정규화된 name/alias -> canonical name. name 우선, alias 후행."""
    idx = {}
    for t in canon:
        cname = t["name"]
        idx.setdefault(normalize(cname), cname)
    for t in canon:
        cname = t["name"]
        for alias in t.get("aliases", []):
            idx.setdefault(normalize(alias), cname)
    return idx


# 종목명으로 보기 어려운 파싱 노이즈 판별: 종목 마스터 미해석 + 길이/문장부호 휴리스틱은
# 리포트에만 쓰고, staging에는 원문 그대로 남긴다(버리지 말 것 원칙).
def looks_like_sentence(name: str) -> bool:
    n = name.strip()
    if len(n) >= 12:
        return True
    if re.search(r"[.?!~]$", n):
        return True
    if " " in n and len(n) >= 7:
        return True
    return False


def main():
    cafe = sqlite3.connect(f"file:{CAFE_DB}?mode=ro", uri=True)
    stocks = sqlite3.connect(f"file:{STOCKS_DB}?mode=ro", uri=True)
    canon = json.load(open(THEME_DICT))["canonical_themes"]

    stock_idx = build_stock_index(stocks)
    theme_idx = build_theme_index(canon)

    rows = cafe.execute(
        """
        SELECT p.post_date, ts.stock_name, ts.ticker, tm.theme_name,
               tm.parent_theme, ts.reason
        FROM cafe_theme_stock ts
        JOIN cafe_theme_mapping tm ON ts.mapping_id = tm.id
        JOIN cafe_post p ON tm.post_id = p.post_id
        WHERE p.board_menu = 994
        ORDER BY p.post_date, tm.seq, ts.id
        """
    ).fetchall()

    items = []
    resolved = 0
    theme_matched = 0
    new_theme_candidates = {}  # raw -> count
    resolved_theme_names = set()
    all_theme_names = set()
    fail_samples = []
    sentence_noise = 0

    for post_date, stock_name, ticker_in, theme_name, parent_theme, reason in rows:
        # 티커 해석: 정확 정규화 일치
        key = normalize(stock_name)
        hit = stock_idx.get(key)
        resolved_ticker = hit[0] if hit else (ticker_in or None)
        if hit:
            resolved += 1
        else:
            if looks_like_sentence(stock_name):
                sentence_noise += 1
            if len(fail_samples) < 20:
                fail_samples.append(stock_name)

        # 테마 canonical 매핑
        all_theme_names.add(theme_name)
        canon_hit = theme_idx.get(normalize(theme_name))
        is_new = canon_hit is None
        if canon_hit:
            resolved_theme_names.add(theme_name)
        else:
            new_theme_candidates[theme_name] = (
                new_theme_candidates.get(theme_name, 0) + 1
            )

        items.append(
            {
                "post_date": post_date,
                "stock_name": stock_name,
                "ticker": resolved_ticker,
                "cafe_theme_raw": theme_name,
                "canonical_theme": canon_hit,
                "is_new_theme_candidate": is_new,
                "reason": reason,
                "parent_theme": parent_theme,
            }
        )

    theme_matched = len(resolved_theme_names)

    payload = {
        "generated_at": _utc_now_iso(),
        "source": "cafe",
        "schema_version": SCHEMA_VERSION,
        "items": items,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    # 품질 리포트 (stdout)
    N = len(items)
    K = len(all_theme_names)
    print("=== 품질 리포트 ===")
    print(f"stock master: {STOCKS_DB} :: stocks(name->code)")
    print(f"종목 총 N={N} / 티커 해석 성공 M={resolved} ({resolved / N * 100:.1f}%)")
    print(f"  실패 중 문장 노이즈 추정: {sentence_noise}건 (1단계 파서 이슈)")
    print(f"  실패 샘플: {fail_samples[:5]}")
    print(
        f"테마 총 K={K} / canonical 매칭 P={theme_matched} / 신규 후보 Q={len(new_theme_candidates)}"
    )
    print(f"  신규 후보 전량: {sorted(new_theme_candidates.keys())}")
    print(f"staging JSON 건수: {N}  ->  {OUT_FILE}")


if __name__ == "__main__":
    main()
