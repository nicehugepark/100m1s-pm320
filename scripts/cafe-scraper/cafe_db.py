"""cafe.db 영속 계층 — 네이버 카페 스크랩 정규화 영구 저장소.

대표 승인 스키마(2026-07-06)의 SQLite 영속 API.
- 파서(main.py)와 독립 — 이 모듈은 raw_body 보존 + 파생 테이블 멱등 갱신만 담당.
- 재파싱 원천 = cafe_post.raw_body. parser_version 올리면
  `--reparse-only --parser-version-below <신버전>` 으로 백필 오류 탈출.
- 네트워크 접근 없음. init_db → upsert_post → persist_* → get/set_state 만.

board_menu 매핑:
  994 = 테마맵     → cafe_theme_mapping / cafe_theme_stock
  167 = 마켓요약   → cafe_market_item / cafe_news_link

경로 우선순위: 인자 path > env CAFE_DB_PATH > 스크래퍼 디렉토리/cafe.db
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path

# ── 상수 ────────────────────────────────────────────────────────
BOARD_THEME = 994  # 테마맵
BOARD_MARKET = 167  # 마켓요약

_SCHEMA_SQL = Path(__file__).with_name("cafe_schema.sql")


def default_db_path() -> str:
    """env CAFE_DB_PATH > 스크래퍼 디렉토리/cafe.db."""
    env = os.environ.get("CAFE_DB_PATH")
    if env:
        return env
    return str(Path(__file__).with_name("cafe.db"))


def _now_iso() -> str:
    # naive now() → astimezone() 가 로컬 타임존(KST) 부여. UTC 심볼 불요
    # (ruff UP017 vs Python 3.9 datetime.UTC 부재 충돌 회피).
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_text(s: str | None) -> str | None:
    if s is None:
        return None
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── 연결 / 스키마 ───────────────────────────────────────────────
def connect(path: str | None = None) -> sqlite3.Connection:
    """WAL + FK on 연결. 호출자가 close 책임."""
    p = path or default_db_path()
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(path: str | None = None) -> str:
    """멱등 스키마 생성. 반환 = 실제 DB 경로."""
    p = path or default_db_path()
    ddl = _SCHEMA_SQL.read_text(encoding="utf-8")
    conn = connect(p)
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()
    return p


# ── 게시물 upsert (post_id 멱등) ────────────────────────────────
def upsert_post(conn: sqlite3.Connection, meta: dict) -> int:
    """post_id 기준 멱등 upsert.

    meta 키:
      post_id (int, 필수), board_menu (int, 필수), title, post_date, url,
      raw_body, fetched_at, reparsed_at, parse_status, parse_format,
      parser_version
    raw_body_hash 는 raw_body 로부터 자동 계산.
    ON CONFLICT(post_id): title/post_date/url/raw_body/hash/reparsed_at/
      parse_status/parse_format/parser_version 갱신. fetched_at 은 최초값 보존
      (COALESCE 로 기존 유지, 신규 시에만 세팅).
    반환 = post_id.
    """
    post_id = int(meta["post_id"])
    board_menu = int(meta["board_menu"])
    raw_body = meta.get("raw_body")
    row = {
        "post_id": post_id,
        "board_menu": board_menu,
        "title": meta.get("title"),
        "post_date": meta.get("post_date"),
        "url": meta.get("url"),
        "raw_body": raw_body,
        "raw_body_hash": sha256_text(raw_body),
        "fetched_at": meta.get("fetched_at") or _now_iso(),
        "reparsed_at": meta.get("reparsed_at"),
        "parse_status": meta.get("parse_status"),
        "parse_format": meta.get("parse_format"),
        "parser_version": meta.get("parser_version"),
    }
    conn.execute(
        """
        INSERT INTO cafe_post
            (post_id, board_menu, title, post_date, url, raw_body, raw_body_hash,
             fetched_at, reparsed_at, parse_status, parse_format, parser_version)
        VALUES
            (:post_id, :board_menu, :title, :post_date, :url, :raw_body, :raw_body_hash,
             :fetched_at, :reparsed_at, :parse_status, :parse_format, :parser_version)
        ON CONFLICT(post_id) DO UPDATE SET
            board_menu     = excluded.board_menu,
            title          = excluded.title,
            post_date      = excluded.post_date,
            url            = excluded.url,
            raw_body       = COALESCE(excluded.raw_body, cafe_post.raw_body),
            raw_body_hash  = COALESCE(excluded.raw_body_hash, cafe_post.raw_body_hash),
            reparsed_at    = excluded.reparsed_at,
            parse_status   = excluded.parse_status,
            parse_format   = excluded.parse_format,
            parser_version = excluded.parser_version
            -- fetched_at 은 최초값 보존 (갱신 안 함)
        """,
        row,
    )
    conn.commit()
    return post_id


def _set_post_parse_meta(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    parse_status: str | None = None,
    parse_format: str | None = None,
    parser_version: str | None = None,
    reparsed_at: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE cafe_post SET
            parse_status   = COALESCE(?, parse_status),
            parse_format   = COALESCE(?, parse_format),
            parser_version = COALESCE(?, parser_version),
            reparsed_at    = COALESCE(?, reparsed_at)
        WHERE post_id = ?
        """,
        (
            parse_status,
            parse_format,
            parser_version,
            reparsed_at or _now_iso(),
            post_id,
        ),
    )


# ── 994: 테마맵 파생 (delete-then-insert) ───────────────────────
def persist_theme_map(
    conn: sqlite3.Connection,
    post_id: int,
    mappings: list[dict],
    *,
    parser_version: str | None = None,
    parse_status: str = "ok",
) -> tuple[int, int]:
    """테마 매핑 재생성. 재파싱 멱등 = post_id 파생행 전부 삭제 후 재삽입.

    mappings 각 항목:
      { theme_name: str, parent_theme: str|None, seq: int|None,
        stocks: [ { stock_name: str, ticker: str|None, reason: str|None }, ... ] }
    반환 = (매핑 수, 종목 수).
    """
    conn.execute("DELETE FROM cafe_theme_mapping WHERE post_id = ?", (post_id,))
    # cafe_theme_stock 은 ON DELETE CASCADE 로 함께 삭제.
    n_map = n_stock = 0
    for i, m in enumerate(mappings):
        theme_name = (m.get("theme_name") or "").strip()
        if not theme_name:
            continue
        cur = conn.execute(
            "INSERT INTO cafe_theme_mapping (post_id, theme_name, parent_theme, seq) "
            "VALUES (?, ?, ?, ?)",
            (post_id, theme_name, m.get("parent_theme"), m.get("seq", i)),
        )
        mapping_id = cur.lastrowid
        n_map += 1
        for st in m.get("stocks", []):
            name = (st.get("stock_name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT INTO cafe_theme_stock (mapping_id, stock_name, ticker, reason) "
                "VALUES (?, ?, ?, ?)",
                (mapping_id, name, st.get("ticker"), st.get("reason")),
            )
            n_stock += 1
    _set_post_parse_meta(
        conn,
        post_id,
        parse_status=parse_status,
        parse_format="theme_map",
        parser_version=parser_version,
    )
    conn.commit()
    return n_map, n_stock


# ── 167: 마켓요약 파생 (delete-then-insert) ─────────────────────
def persist_market_summary(
    conn: sqlite3.Connection,
    post_id: int,
    items: list[dict],
    news_links: list[dict] | None = None,
    *,
    parser_version: str | None = None,
    parse_status: str = "ok",
) -> tuple[int, int]:
    """마켓 요약 아이템 + 뉴스 링크 재생성 (재파싱 멱등).

    items 각 항목:
      { summary_date: str|None, section: str, stock_name: str,
        ticker: str|None, reason: str|None }
    news_links 각 항목:
      { url: str, host: str|None, anchor_text: str|None, seq: int|None }
    반환 = (아이템 수, 뉴스링크 수).
    """
    conn.execute("DELETE FROM cafe_market_item WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM cafe_news_link WHERE post_id = ?", (post_id,))
    n_item = 0
    for it in items:
        name = (it.get("stock_name") or "").strip()
        if not name:
            continue
        conn.execute(
            "INSERT INTO cafe_market_item "
            "(post_id, summary_date, section, stock_name, ticker, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                post_id,
                it.get("summary_date"),
                it.get("section"),
                name,
                it.get("ticker"),
                it.get("reason"),
            ),
        )
        n_item += 1
    n_link = 0
    for i, lk in enumerate(news_links or []):
        url = (lk.get("url") or "").strip()
        if not url:
            continue
        conn.execute(
            "INSERT INTO cafe_news_link (post_id, url, host, anchor_text, seq) "
            "VALUES (?, ?, ?, ?, ?)",
            (post_id, url, lk.get("host"), lk.get("anchor_text"), lk.get("seq", i)),
        )
        n_link += 1
    _set_post_parse_meta(
        conn,
        post_id,
        parse_status=parse_status,
        parse_format="market_summary",
        parser_version=parser_version,
    )
    conn.commit()
    return n_item, n_link


# ── 워터마크 ────────────────────────────────────────────────────
def get_state(conn: sqlite3.Connection, board_menu: int) -> dict | None:
    """{ board_menu, last_article_id, last_processed_at } 또는 None."""
    r = conn.execute(
        "SELECT board_menu, last_article_id, last_processed_at "
        "FROM cafe_scrape_state WHERE board_menu = ?",
        (int(board_menu),),
    ).fetchone()
    return dict(r) if r else None


def set_state(
    conn: sqlite3.Connection,
    board_menu: int,
    last_article_id: int,
    ts: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO cafe_scrape_state (board_menu, last_article_id, last_processed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(board_menu) DO UPDATE SET
            last_article_id   = excluded.last_article_id,
            last_processed_at = excluded.last_processed_at
        """,
        (int(board_menu), int(last_article_id), ts or _now_iso()),
    )
    conn.commit()


# ── 통계 ────────────────────────────────────────────────────────
def stats(conn: sqlite3.Connection) -> dict:
    """게시판별 post 수·최신 post_date·parser_version 분포."""
    out: dict = {"boards": {}, "parser_version": {}, "parse_status": {}}
    for r in conn.execute(
        "SELECT board_menu, COUNT(*) n, MAX(post_date) latest "
        "FROM cafe_post GROUP BY board_menu"
    ):
        out["boards"][r["board_menu"]] = {
            "posts": r["n"],
            "latest_post_date": r["latest"],
        }
    for r in conn.execute(
        "SELECT COALESCE(parser_version,'(none)') v, COUNT(*) n "
        "FROM cafe_post GROUP BY v"
    ):
        out["parser_version"][r["v"]] = r["n"]
    for r in conn.execute(
        "SELECT COALESCE(parse_status,'(none)') s, COUNT(*) n FROM cafe_post GROUP BY s"
    ):
        out["parse_status"][r["s"]] = r["n"]
    out["derived"] = {
        "theme_mapping": conn.execute(
            "SELECT COUNT(*) FROM cafe_theme_mapping"
        ).fetchone()[0],
        "theme_stock": conn.execute("SELECT COUNT(*) FROM cafe_theme_stock").fetchone()[
            0
        ],
        "market_item": conn.execute("SELECT COUNT(*) FROM cafe_market_item").fetchone()[
            0
        ],
        "news_link": conn.execute("SELECT COUNT(*) FROM cafe_news_link").fetchone()[0],
    }
    return out
