"""cafe.db 백필 CLI — 네트워크 없이 raw_body 재파싱.

설계 원칙 (백필 오류 탈출):
  파서 버그 발견 → parser_version 올림 → 아래 실행:
      python cafe_backfill.py --reparse-only --parser-version-below <신버전>
  cafe_post.raw_body(원문)만으로 파생 테이블 재생성. 네이버 재접속 불필요.

파서 연동:
  main.py 의 parse_post()/extract_stock_news_blocks() 를 import 하되,
  import 실패(BeautifulSoup 미설치·리팩터 등) 시 graceful → 내장 스텁 파서로
  자기검증 경로는 유지. 스텁은 raw_body 안의 마커만 인식(자기검증용).

사용:
  python cafe_backfill.py --self-check              # 네트워크 0 전 경로 실측
  python cafe_backfill.py --reparse-only [--board N] [--parser-version-below X]
  python cafe_backfill.py --stats
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

import cafe_db
import cafe_persist_glue

# 현재 파서 버전 — glue 모듈 SoT 참조(중복 정의 제거).
PARSER_VERSION = cafe_persist_glue.PARSER_VERSION

# board_menu → 실 파서 kind (main.py MENUS 와 일치)
_BOARD_KIND = {cafe_db.BOARD_THEME: "theme_map", cafe_db.BOARD_MARKET: "limit_hl"}


def _call_parser(parse_post, raw: str, title, board: int) -> dict:
    """실 파서엔 board→kind 를 명시 주입(확정 라우팅). 스텁 등 kind 미지원은 graceful."""
    kind = _BOARD_KIND.get(board)
    if kind is not None:
        try:
            return parse_post(raw, title, kind=kind)
        except TypeError:
            pass  # kind 인자 미지원(스텁) → 하위호환 호출
    return parse_post(raw, title)


# ── 파서 어댑터 ─────────────────────────────────────────────────
# main.py parse_post() 반환을 cafe.db 스키마 형태로 변환한다.
# 반환 계약(파서가 제공해야 할 dict):
#   parse_post(html, title) -> {
#       parse_format: "rank_table"|"short_note"|"essay"|"unknown",
#       parse_status: "ok"|"unsupported_format",
#       sections: [ { type: str, stocks: [ {name, ticker, theme_label, ...} ] } ],
#       post_date: str|None,
#   }
#   extract_stock_news_blocks(html) -> [ { url, host, text/theme_label, ... } ]
def _load_real_parser():
    try:
        import main  # noqa: PLC0415

        return main.parse_post, getattr(main, "extract_stock_news_blocks", None)
    except Exception as e:  # noqa: BLE001  (graceful: 파서 부재/의존성 결여 허용)
        print(f"[warn] 실 파서 import 실패 → 스텁 파서 사용: {e}", file=sys.stderr)
        return _stub_parse_post, _stub_news_blocks


def _stub_parse_post(html: str, title: str | None = None) -> dict:
    """자기검증 전용 스텁. raw_body 안의 마커만 인식.

    형식 마커:
      "@@THEME@@ 테마명|부모|종목A:reasonA;종목B" → theme_map
      "@@MARKET@@ 상승|종목A:reasonA;하락|종목B"   → market_summary
    그 외 → unknown.
    """
    text = html or ""
    if "@@THEME@@" in text:
        line = text.split("@@THEME@@", 1)[1].strip().splitlines()[0]
        theme_part, _, stock_part = line.partition("|")
        parent, _, stock_part2 = stock_part.partition("|")
        stocks = []
        for tok in stock_part2.split(";"):
            tok = tok.strip()
            if not tok:
                continue
            name, _, reason = tok.partition(":")
            stocks.append(
                {
                    "name": name.strip(),
                    "ticker": None,
                    "theme_label": reason.strip() or None,
                }
            )
        return {
            "parse_format": "rank_table",
            "parse_status": "ok",
            "post_date": "2026-07-06",
            "sections": [
                {
                    "type": theme_part.strip() or "테마",
                    "parent": parent.strip() or None,
                    "stocks": stocks,
                }
            ],
        }
    if "@@MARKET@@" in text:
        line = text.split("@@MARKET@@", 1)[1].strip().splitlines()[0]
        sections = []
        for seg in line.split(";"):
            seg = seg.strip()
            if not seg:
                continue
            sec, _, rest = seg.partition("|")
            name, _, reason = rest.partition(":")
            sections.append(
                {
                    "type": sec.strip(),
                    "stocks": [
                        {
                            "name": name.strip(),
                            "ticker": None,
                            "theme_label": reason.strip() or None,
                        }
                    ],
                }
            )
        return {
            "parse_format": "rank_table",
            "parse_status": "ok",
            "post_date": "2026-07-06",
            "sections": sections,
        }
    return {
        "parse_format": "unknown",
        "parse_status": "unsupported_format",
        "sections": [],
        "post_date": None,
    }


def _stub_news_blocks(html: str) -> list[dict]:
    """@@NEWS@@ url|anchor;url|anchor → 뉴스 링크."""
    text = html or ""
    if "@@NEWS@@" not in text:
        return []
    line = text.split("@@NEWS@@", 1)[1].strip().splitlines()[0]
    out = []
    for seg in line.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        url, _, anchor = seg.partition("|")
        url = url.strip()
        out.append(
            {
                "url": url,
                "host": urlparse(url).netloc or None,
                "text": anchor.strip() or None,
            }
        )
    return out


# ── 스키마 변환 ─────────────────────────────────────────────────
# glue 모듈(cafe_persist_glue)이 SoT — main.py 실시간 배선과 공유(중복/누락 방지).
# 하위호환 얇은 alias 만 유지(기존 호출부/테스트 보존).
_to_theme_mappings = cafe_persist_glue.theme_map_to_mappings
_to_market_items = cafe_persist_glue.limit_hl_to_market_items
_to_news_links = cafe_persist_glue.news_blocks_to_links


# ── 재파싱 코어 ─────────────────────────────────────────────────
def reparse_one(conn, parse_post, news_fn, post_row: dict) -> str:
    """단일 post 재파싱 → 파생 테이블 재생성. 반환 = parse_status."""
    post_id = post_row["post_id"]
    board = post_row["board_menu"]
    raw = post_row["raw_body"] or ""
    title = post_row.get("title")
    try:
        parsed = _call_parser(parse_post, raw, title, board)
    except Exception as e:  # noqa: BLE001
        cafe_db._set_post_parse_meta(  # noqa: SLF001
            conn, post_id, parse_status="error", parser_version=PARSER_VERSION
        )
        conn.commit()
        print(f"[error] post {post_id} 파싱 예외: {e}", file=sys.stderr)
        return "error"

    if board == cafe_db.BOARD_THEME:
        cafe_db.persist_theme_map(
            conn,
            post_id,
            _to_theme_mappings(parsed),
            parser_version=PARSER_VERSION,
            parse_status=parsed.get("parse_status", "ok"),
        )
    elif board == cafe_db.BOARD_MARKET:
        blocks = news_fn(raw) if news_fn else []
        cafe_db.persist_market_summary(
            conn,
            post_id,
            _to_market_items(parsed),
            _to_news_links(blocks),
            parser_version=PARSER_VERSION,
            parse_status=parsed.get("parse_status", "ok"),
        )
    else:
        cafe_db._set_post_parse_meta(  # noqa: SLF001
            conn, post_id, parse_status="skip", parser_version=PARSER_VERSION
        )
        conn.commit()
        return "skip"
    return parsed.get("parse_status", "ok")


def reparse_only(path, board=None, version_below=None, parser=None) -> dict:
    """네트워크 0 재파싱. raw_body → 파생 테이블 재생성.

    parser: (parse_post, news_fn) 튜플 주입 시 실 파서 대신 사용
            (자기검증 격리용 — 스텁 파서 강제).
    """
    parse_post, news_fn = parser if parser else _load_real_parser()
    conn = cafe_db.connect(path)
    try:
        q = "SELECT post_id, board_menu, title, raw_body FROM cafe_post WHERE raw_body IS NOT NULL"
        args: list = []
        if board is not None:
            q += " AND board_menu = ?"
            args.append(int(board))
        if version_below is not None:
            q += " AND (parser_version IS NULL OR parser_version < ?)"
            args.append(version_below)
        rows = [dict(r) for r in conn.execute(q, args)]
        counts: dict = {"total": len(rows)}
        for r in rows:
            st = reparse_one(conn, parse_post, news_fn, r)
            counts[st] = counts.get(st, 0) + 1
        return counts
    finally:
        conn.close()


# ── 자기검증 (네트워크 0) ───────────────────────────────────────
def self_check(path: str) -> bool:
    """합성 레코드로 init→upsert→persist→reparse→stats + 멱등성 실측."""
    print("=== self-check 시작 (네트워크 0) ===")
    real = cafe_db.init_db(path)
    print(f"[init_db] OK → {real}")

    conn = cafe_db.connect(path)
    try:
        # 합성 raw_body 2건 (994/167 각 1건). 스텁 파서 마커 사용.
        theme_body = (
            "게시물 원문 ...\n@@THEME@@ 2차전지|소재|에코프로:양극재;"
            "포스코퓨처엠:음극재\n기타 본문"
        )
        market_body = (
            "장마감 요약 ...\n@@MARKET@@ 상승|삼성전자:반도체 반등;"
            "하락|LG에너지솔루션:차익실현\n"
            "@@NEWS@@ https://n.news.naver.com/a1|반도체 훈풍;"
            "https://finance.daum.net/b2|2차전지 조정"
        )

        cafe_db.upsert_post(
            conn,
            {
                "post_id": 9940001,
                "board_menu": cafe_db.BOARD_THEME,
                "title": "테마맵 테스트",
                "url": "https://cafe.naver.com/x/9940001",
                "raw_body": theme_body,
                "parser_version": None,
            },
        )
        cafe_db.upsert_post(
            conn,
            {
                "post_id": 1670001,
                "board_menu": cafe_db.BOARD_MARKET,
                "title": "마켓요약 테스트",
                "url": "https://cafe.naver.com/x/1670001",
                "raw_body": market_body,
                "parser_version": None,
            },
        )
        print("[upsert_post] 2건 삽입 OK")
    finally:
        conn.close()

    # 멱등성: 동일 post_id 재 upsert → 중복 0 확인.
    conn = cafe_db.connect(path)
    try:
        cafe_db.upsert_post(
            conn,
            {
                "post_id": 9940001,
                "board_menu": cafe_db.BOARD_THEME,
                "title": "테마맵 테스트(재)",
                "raw_body": theme_body,
            },
        )
        n_post = conn.execute("SELECT COUNT(*) FROM cafe_post").fetchone()[0]
        assert n_post == 2, f"멱등성 위반: post 수={n_post} (기대 2)"
        print(f"[멱등성] 재 upsert 후 post 수 = {n_post} (중복 0) OK")
    finally:
        conn.close()

    # 워터마크
    conn = cafe_db.connect(path)
    try:
        cafe_db.set_state(conn, cafe_db.BOARD_THEME, 9940001)
        cafe_db.set_state(conn, cafe_db.BOARD_MARKET, 1670001)
        st = cafe_db.get_state(conn, cafe_db.BOARD_THEME)
        assert st and st["last_article_id"] == 9940001, "워터마크 오류"
        print(f"[watermark] 994 last_article_id = {st['last_article_id']} OK")
    finally:
        conn.close()

    # 재파싱 1회차 — 격리: 스텁 파서 강제 주입 (실 파서는 @@마커@@ 미인식).
    stub = (_stub_parse_post, _stub_news_blocks)
    c1 = reparse_only(path, parser=stub)
    print(f"[reparse #1] {c1}")

    # 재파싱 멱등성: 2회차 후 파생행 수 동일 확인.
    conn = cafe_db.connect(path)
    try:
        d1 = cafe_db.stats(conn)["derived"]
    finally:
        conn.close()
    reparse_only(path, parser=stub)
    conn = cafe_db.connect(path)
    try:
        d2 = cafe_db.stats(conn)["derived"]
    finally:
        conn.close()
    assert d1 == d2, f"재파싱 멱등성 위반: {d1} != {d2}"
    print(f"[reparse 멱등성] 파생행 2회 동일 = {d2} OK")

    # 파생 데이터 실측
    conn = cafe_db.connect(path)
    try:
        tm = conn.execute(
            "SELECT theme_name, parent_theme FROM cafe_theme_mapping"
        ).fetchall()
        ts = conn.execute("SELECT stock_name, reason FROM cafe_theme_stock").fetchall()
        mi = conn.execute("SELECT section, stock_name FROM cafe_market_item").fetchall()
        nl = conn.execute("SELECT host, anchor_text FROM cafe_news_link").fetchall()
        assert len(tm) == 1 and tm[0]["theme_name"] == "2차전지", (
            f"theme_mapping 오류: {[dict(r) for r in tm]}"
        )
        assert len(ts) == 2, f"theme_stock 수 오류: {len(ts)}"
        assert len(mi) == 2, f"market_item 수 오류: {len(mi)}"
        assert len(nl) == 2, f"news_link 수 오류: {len(nl)}"
        print(
            f"[파생 실측] theme_mapping={len(tm)} theme_stock={len(ts)} "
            f"market_item={len(mi)} news_link={len(nl)} OK"
        )
        print(
            f"  theme: {tm[0]['theme_name']}/{tm[0]['parent_theme']} "
            f"stocks={[r['stock_name'] for r in ts]}"
        )
        print(f"  market: {[(r['section'], r['stock_name']) for r in mi]}")
        print(f"  news hosts: {[r['host'] for r in nl]}")

        final = cafe_db.stats(conn)
        print(f"[stats]\n{json.dumps(final, ensure_ascii=False, indent=2)}")
    finally:
        conn.close()

    print("=== self-check PASS ===")
    return True


# ── CLI ─────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cafe.db 백필/재파싱 CLI (네트워크 0)")
    ap.add_argument(
        "--db", default=None, help="cafe.db 경로 (기본: env CAFE_DB_PATH > ./cafe.db)"
    )
    ap.add_argument(
        "--self-check", action="store_true", help="합성 레코드 전 경로 자기검증"
    )
    ap.add_argument(
        "--reparse-only", action="store_true", help="raw_body 재파싱 (네트워크 0)"
    )
    ap.add_argument(
        "--board", type=int, default=None, help="특정 board_menu 만 (994|167)"
    )
    ap.add_argument(
        "--parser-version-below",
        default=None,
        help="이 버전 미만 post 만 재파싱 (백필 오류 탈출)",
    )
    ap.add_argument("--stats", action="store_true", help="게시판별 통계 출력")
    args = ap.parse_args(argv)

    path = args.db or cafe_db.default_db_path()

    if args.self_check:
        ok = self_check(path)
        return 0 if ok else 1

    if args.reparse_only:
        cafe_db.init_db(path)
        c = reparse_only(
            path, board=args.board, version_below=args.parser_version_below
        )
        print(f"[reparse-only] {c}")
        return 0

    if args.stats:
        cafe_db.init_db(path)
        conn = cafe_db.connect(path)
        try:
            print(json.dumps(cafe_db.stats(conn), ensure_ascii=False, indent=2))
        finally:
            conn.close()
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
