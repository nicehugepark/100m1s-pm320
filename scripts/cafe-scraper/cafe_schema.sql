-- cafe.db 정규화 영구 스키마 (대표 승인 — 2026-07-06)
-- 재파싱 원천(raw_body) 보존 + 파생 테이블 delete-then-insert 멱등.
-- board_menu: 994 = 테마맵(theme_map), 167 = 마켓요약(market_summary)
-- 모든 CREATE 는 IF NOT EXISTS — init_db 멱등.

-- ── 게시물 원천 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cafe_post (
    post_id        INTEGER PRIMARY KEY,          -- 네이버 카페 article_id
    board_menu     INTEGER NOT NULL,             -- 994 | 167
    title          TEXT,
    post_date      TEXT,                          -- YYYY-MM-DD (파싱 추출)
    url            TEXT,
    raw_body       TEXT,                          -- 본문 원문(HTML) — 재파싱 원천
    raw_body_hash  TEXT,                          -- sha256(raw_body)
    fetched_at     TEXT,                          -- 최초/최종 수집 ISO ts
    reparsed_at    TEXT,                          -- 마지막 재파싱 ISO ts
    parse_status   TEXT,                          -- ok | fallback | error | skip
    parse_format   TEXT,                          -- theme_map | market_summary | essay | unknown
    parser_version TEXT                           -- 파서 버전 (백필 탈출 기준)
);
CREATE INDEX IF NOT EXISTS idx_post_board_date ON cafe_post(board_menu, post_date);
CREATE INDEX IF NOT EXISTS idx_post_date       ON cafe_post(post_date);

-- ── 994: 테마 매핑 ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cafe_theme_mapping (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL REFERENCES cafe_post(post_id) ON DELETE CASCADE,
    theme_name   TEXT NOT NULL,
    parent_theme TEXT,                             -- NULL 허용
    seq          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_theme_mapping_name ON cafe_theme_mapping(theme_name);
CREATE INDEX IF NOT EXISTS idx_theme_mapping_post ON cafe_theme_mapping(post_id);

CREATE TABLE IF NOT EXISTS cafe_theme_stock (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id INTEGER NOT NULL REFERENCES cafe_theme_mapping(id) ON DELETE CASCADE,
    stock_name TEXT NOT NULL,
    ticker     TEXT,                               -- NULL 허용
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_theme_stock_name   ON cafe_theme_stock(stock_name);
CREATE INDEX IF NOT EXISTS idx_theme_stock_ticker ON cafe_theme_stock(ticker);

-- ── 167: 마켓 요약 ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cafe_market_item (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL REFERENCES cafe_post(post_id) ON DELETE CASCADE,
    summary_date TEXT,
    section      TEXT,                             -- 상승 | 하락 | 섹터명
    stock_name   TEXT NOT NULL,
    ticker       TEXT,                             -- NULL 허용
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_item_date  ON cafe_market_item(summary_date);
CREATE INDEX IF NOT EXISTS idx_market_item_name  ON cafe_market_item(stock_name);

CREATE TABLE IF NOT EXISTS cafe_news_link (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES cafe_post(post_id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    host        TEXT,
    anchor_text TEXT,                              -- NULL 허용
    seq         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_news_link_host ON cafe_news_link(host);
CREATE INDEX IF NOT EXISTS idx_news_link_post ON cafe_news_link(post_id);

-- ── 워터마크 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cafe_scrape_state (
    board_menu       INTEGER PRIMARY KEY,          -- 994 | 167
    last_article_id  INTEGER,
    last_processed_at TEXT
);
