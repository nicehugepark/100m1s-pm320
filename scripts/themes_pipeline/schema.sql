-- themes.db 스키마 (Phase 5 / REQ-20260420-REQ-004)
-- 메인 stocks.db와 격리. 메인은 ATTACH read-only.
-- 법무 CAUTION: 카페 원문 본문 저장 금지 (메타데이터만). project_cafe_legal_stance.md 참조.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 이시카와 1차 분석 raw (RSS+카페 통합 분석 직후, 검증 전)
CREATE TABLE IF NOT EXISTS themes_raw (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  date          TEXT NOT NULL,                 -- YYYY-MM-DD
  theme_name    TEXT NOT NULL,                  -- canonical 테마명
  parent_theme  TEXT,                           -- 상위 테마 (트리 인과)
  summary       TEXT,                           -- "사건 → 영향 → 섹터" 인과 사슬
  source_count  INTEGER DEFAULT 0,              -- 보도 매체 수
  source_kind   TEXT NOT NULL DEFAULT 'rss',    -- rss / cafe / hybrid
  input_hash    TEXT NOT NULL,                  -- 입력 뉴스 ID 집합 해시 (LLM dedupe 키)
  evidence_json TEXT DEFAULT '[]',              -- 근거 뉴스 ID/제목 JSON
  model_version TEXT,                           -- ISHIKAWA_MODEL 버전
  created_at    TEXT NOT NULL,
  UNIQUE(date, theme_name, source_kind, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_traw_date ON themes_raw(date DESC);
CREATE INDEX IF NOT EXISTS idx_traw_theme ON themes_raw(theme_name);

-- 토구사 검증 결과
CREATE TABLE IF NOT EXISTS themes_verified (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  date          TEXT NOT NULL,
  theme_name    TEXT NOT NULL,
  verdict       TEXT NOT NULL,                  -- pass / weak / reject
  reason        TEXT,                           -- 토구사 판정 근거
  raw_id        INTEGER,                        -- themes_raw.id 참조 (논리 FK, 동일 DB 내)
  input_hash    TEXT NOT NULL,                  -- 입력 themes_raw 해시 (LLM dedupe 키)
  model_version TEXT,                           -- TOGUSA_MODEL 버전
  created_at    TEXT NOT NULL,
  UNIQUE(date, theme_name, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_tver_date ON themes_verified(date DESC);

-- 날짜별 테마 트리 (메인이 ATTACH read-only로 참조)
CREATE TABLE IF NOT EXISTS theme_map (
  date          TEXT PRIMARY KEY,               -- YYYY-MM-DD
  tree_json     TEXT NOT NULL,                  -- 테마 트리 JSON (이슈→방향성→파생→종목)
  built_from    TEXT NOT NULL,                  -- 'themes_verified' 등 빌드 출처
  created_at    TEXT NOT NULL
);

-- 날짜별 매크로 요약 (best-of, 메인이 ATTACH read-only로 참조)
CREATE TABLE IF NOT EXISTS macro_summary (
  date          TEXT NOT NULL,
  rank          INTEGER NOT NULL,               -- 1=가장 중요
  keyword       TEXT NOT NULL,
  summary       TEXT NOT NULL,
  source_count  INTEGER DEFAULT 0,
  verified      INTEGER DEFAULT 0,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (date, rank)
);

CREATE INDEX IF NOT EXISTS idx_macro_summary_date ON macro_summary(date DESC);

-- ── 카페 영역 (테이블만, 구현 보류 — DOC-YYYYMMDD-DES-NNN-cafe-pipeline 참조) ──

-- 카페 게시글 메타데이터 (원문 본문 저장 금지 — 법무 Critical 근접)
CREATE TABLE IF NOT EXISTS cafe_articles (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  cafe_name       TEXT NOT NULL,                -- 카페 식별자
  article_url     TEXT NOT NULL,                -- 원본 URL (본문 미저장)
  title           TEXT,                         -- 제목 메타만
  format_kind     TEXT,                         -- 파서 분류 (단일 마커 가정 금지)
  posted_at       TEXT,
  collected_at    TEXT NOT NULL,
  url_hash        TEXT UNIQUE NOT NULL,         -- dedupe 키
  takedown_status TEXT DEFAULT 'active'         -- active / removed (운영자 요청 시)
);

CREATE INDEX IF NOT EXISTS idx_cafe_arts_cafe ON cafe_articles(cafe_name, posted_at DESC);

-- 카페 추출 결과 (LLM 해석 산출, 본문 저장 금지)
CREATE TABLE IF NOT EXISTS cafe_extracts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id    INTEGER NOT NULL,
  date          TEXT NOT NULL,
  theme_name    TEXT,
  summary       TEXT,                           -- 인과 사슬 요약 (원문 인용 금지)
  input_hash    TEXT NOT NULL,                  -- LLM dedupe 키
  model_version TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(article_id, input_hash),
  FOREIGN KEY (article_id) REFERENCES cafe_articles(id)
);

CREATE INDEX IF NOT EXISTS idx_cafe_ext_date ON cafe_extracts(date DESC);

-- ── LLM 캐시 (Phase 3 동일 스키마, domain in theme_extract / theme_verify / cafe_extract) ──
-- 트랙 B(REQ-003) stocks.db.llm_cache와 컬럼·인덱스 통일
CREATE TABLE IF NOT EXISTS llm_cache (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  domain        TEXT NOT NULL,                  -- 'theme_extract' | 'theme_verify' | 'cafe_extract'
  target_id     TEXT NOT NULL,                  -- 의미적 타겟 (예: 날짜+kind, article_id)
  input_hash    TEXT NOT NULL,                  -- 입력 해시
  model_version TEXT NOT NULL,                  -- 모델 식별자
  response_json TEXT NOT NULL,                  -- LLM 응답 raw
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  hit_count     INTEGER DEFAULT 0,
  UNIQUE(domain, target_id, input_hash, model_version)
);

CREATE INDEX IF NOT EXISTS idx_llm_cache_lookup ON llm_cache(domain, target_id);
CREATE INDEX IF NOT EXISTS idx_llm_cache_created ON llm_cache(created_at DESC);

-- 마이그레이션 추적
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename      TEXT PRIMARY KEY,
  applied_at    TEXT NOT NULL
);
