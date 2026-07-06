-- /news Phase 1 SQLite schema
-- REQ-003 / DISC-20260409-001 기준
-- 법무팀 CAUTION: 원문 본문 저장 금지, 메타데이터만

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 종목 마스터
CREATE TABLE IF NOT EXISTS stocks (
  code          TEXT PRIMARY KEY,           -- 6자리 KRX 종목코드
  name          TEXT NOT NULL,
  market        TEXT,                        -- KOSPI / KOSDAQ / KONEX
  industry      TEXT,                        -- KRX 업종 분류
  sector        TEXT,                        -- 섹터 (대분류)
  last_updated  TEXT NOT NULL,               -- ISO8601
  -- REQ-20260420-REQ-001 Phase 2: 240영업일 가격 통계 (collect_dailybars 산출)
  -- 가용 일수가 240 미만이면 가용 일수 기준 (필드명 그대로)
  price_high_240d       INTEGER,
  price_high_240d_date  TEXT,
  price_low_240d        INTEGER,
  price_low_240d_date   TEXT,
  pct_from_high_240d    REAL,
  pct_from_low_240d     REAL
);

CREATE INDEX IF NOT EXISTS idx_stocks_name ON stocks(name);

-- 업종(시장 대표) 지수 일봉 — ka20006 (임무 C, 2026-04-24)
-- 용도: build_daily의 index_multiple_current(종목 change_pct vs 지수 change_pct 배수).
-- 값: 키움 응답 × 100 스케일 → /100 보정 후 REAL 저장.
-- KOSPI(001), KOSDAQ(101) 종합지수 적재.
CREATE TABLE IF NOT EXISTS index_dailybars (
  index_code  TEXT NOT NULL,               -- 'KOSPI' | 'KOSDAQ'
  date        TEXT NOT NULL,               -- YYYY-MM-DD
  open        REAL,
  high        REAL,
  low         REAL,
  close       REAL NOT NULL,
  volume      INTEGER,
  PRIMARY KEY (index_code, date)
);
CREATE INDEX IF NOT EXISTS idx_idb_date ON index_dailybars(date DESC);

-- 뉴스 히스토리 (무제한 누적)
-- 법무: title·url·published_at·source만. 원문 본문 없음.
CREATE TABLE IF NOT EXISTS news (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code    TEXT NOT NULL,
  title         TEXT NOT NULL,
  url           TEXT NOT NULL,
  published_at  TEXT NOT NULL,               -- ISO8601
  source        TEXT NOT NULL,               -- 한경 / 매경 / ...
  causal_chain  TEXT,                         -- Gemini 생성 인과 사슬
  macro_event   TEXT,                         -- 연결된 매크로 이벤트
  evidence_span TEXT,                         -- Gemini 근거 인용 (url+title 범위)
  collected_at  TEXT NOT NULL,               -- 수집 시각
  idempotency_key TEXT UNIQUE NOT NULL,      -- {code}:{published_at}:{url_hash}
  is_robot        INTEGER DEFAULT 0,         -- 1=로봇기사 (강세토픽/VI발동 등), 매칭 제외 대상
  match_source    TEXT DEFAULT 'title',      -- 'title' | 'body' | 'manual' — 매칭 근거
  FOREIGN KEY (stock_code) REFERENCES stocks(code)
);

CREATE INDEX IF NOT EXISTS idx_news_code_date ON news(stock_code, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_published ON news(published_at DESC);

-- 매크로 이벤트 클러스터 (LLM 추출 + 키워드 빈도 폴백)
CREATE TABLE IF NOT EXISTS macro_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  date          TEXT NOT NULL,                -- YYYY-MM-DD
  keyword       TEXT NOT NULL,
  summary       TEXT,
  source_count  INTEGER NOT NULL,             -- 몇 개 언론사에서 나왔나
  stock_codes_json TEXT DEFAULT '[]',        -- 연결 종목들
  created_at    TEXT NOT NULL,
  source        TEXT DEFAULT 'extract',       -- 'llm' | 'extract' | 'interpret'
  verified      INTEGER DEFAULT 1             -- 1=confirmed, 0=unconfirmed (LLM 교차검증)
);

CREATE INDEX IF NOT EXISTS idx_macro_date ON macro_events(date DESC);

-- 오늘의 종목 (거래대금 기반 일별 선정)
CREATE TABLE IF NOT EXISTS daily_picks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  date          TEXT NOT NULL,               -- YYYY-MM-DD
  stock_code    TEXT NOT NULL,
  rank          INTEGER,
  trade_amount  INTEGER,                     -- 거래대금 (원 단위)
  change_pct    REAL,
  price         INTEGER,
  open_price    REAL,                        -- 시가
  high_price    REAL,                        -- 고가
  low_price     REAL,                        -- 저가
  source        TEXT DEFAULT 'kiwoom',        -- kiwoom / manual
  created_at    TEXT NOT NULL,
  UNIQUE(date, stock_code, source)
);

CREATE INDEX IF NOT EXISTS idx_picks_date ON daily_picks(date DESC);
CREATE INDEX IF NOT EXISTS idx_picks_code ON daily_picks(stock_code);

-- 기술지표 (MA/RSI/MACD — calc_indicators.py 산출)
CREATE TABLE IF NOT EXISTS technical_indicators (
  date          TEXT NOT NULL,               -- YYYY-MM-DD
  stock_code    TEXT NOT NULL,
  ma5           REAL,
  ma10          REAL,
  ma20          REAL,
  ma60          REAL,
  ma120         REAL,
  ma240         REAL,
  rsi14         REAL,                        -- 0~100
  macd          REAL,                        -- MACD line (EMA12 - EMA26)
  macd_signal   REAL,                        -- Signal line (EMA9 of MACD)
  macd_hist     REAL,                        -- MACD histogram
  created_at    TEXT NOT NULL,
  PRIMARY KEY (date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_ti_code ON technical_indicators(stock_code, date DESC);

-- 테마 마스터 (정규화 — 대표 지시 2026-04-10)
CREATE TABLE IF NOT EXISTS themes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT UNIQUE NOT NULL,           -- 정규화된 테마명 (canonical)
  parent_id     INTEGER,                         -- 상위 테마 (트리 구조: AI발전→전력난→전력설비)
  category      TEXT,                            -- issue / direction / theme / industry
  aliases_json  TEXT DEFAULT '[]',              -- 동의어 목록 JSON
  is_active     INTEGER DEFAULT 1,              -- 1=활성, 0=가비지/비활성 (2026-04-10)
  created_at    TEXT NOT NULL,
  FOREIGN KEY (parent_id) REFERENCES themes(id)
);

CREATE INDEX IF NOT EXISTS idx_themes_name ON themes(name);

-- 종목-테마 매핑 (다대다, 날짜 포함)
CREATE TABLE IF NOT EXISTS stock_themes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code    TEXT NOT NULL,
  theme_id      INTEGER NOT NULL,
  date_added    TEXT NOT NULL,                  -- YYYY-MM-DD (처음 이 테마가 붙은 날)
  date_last     TEXT NOT NULL,                  -- YYYY-MM-DD (마지막으로 이 테마가 활성인 날)
  source        TEXT DEFAULT 'ishikawa',        -- ishikawa / togusa / owner / pipeline
  UNIQUE(stock_code, theme_id),
  FOREIGN KEY (stock_code) REFERENCES stocks(code),
  FOREIGN KEY (theme_id) REFERENCES themes(id)
);

CREATE INDEX IF NOT EXISTS idx_st_code ON stock_themes(stock_code);
CREATE INDEX IF NOT EXISTS idx_st_theme ON stock_themes(theme_id);
CREATE INDEX IF NOT EXISTS idx_st_date ON stock_themes(date_last DESC);

-- 테마 일별 통계 (거래대금 기반 강도 트렌드)
CREATE TABLE IF NOT EXISTS theme_daily_stats (
  date          TEXT NOT NULL,
  theme_id      INTEGER NOT NULL,
  stock_count   INTEGER DEFAULT 0,
  total_trade_amount INTEGER DEFAULT 0,
  avg_change_pct REAL DEFAULT 0.0,
  PRIMARY KEY (date, theme_id),
  FOREIGN KEY (theme_id) REFERENCES themes(id)
);

CREATE INDEX IF NOT EXISTS idx_tds_theme ON theme_daily_stats(theme_id, date DESC);

-- 뉴스-테마 매핑 (어떤 뉴스가 어떤 테마와 연결되는가)
CREATE TABLE IF NOT EXISTS news_themes (
  news_id       INTEGER NOT NULL,
  theme_id      INTEGER NOT NULL,
  PRIMARY KEY (news_id, theme_id),
  FOREIGN KEY (news_id) REFERENCES news(id),
  FOREIGN KEY (theme_id) REFERENCES themes(id)
);

CREATE INDEX IF NOT EXISTS idx_nt_theme ON news_themes(theme_id);
CREATE INDEX IF NOT EXISTS idx_nt_news ON news_themes(news_id);

-- 일별 내러티브 (오늘의 뉴스요약 — DB가 Source of Truth)
CREATE TABLE IF NOT EXISTS daily_narratives (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  date          TEXT NOT NULL,                -- YYYY-MM-DD
  narrative     TEXT NOT NULL,                -- 요약 문장
  source        TEXT DEFAULT 'pipeline',      -- pipeline / ishikawa / togusa
  created_at    TEXT NOT NULL,
  UNIQUE(date, narrative)
);

CREATE INDEX IF NOT EXISTS idx_narr_date ON daily_narratives(date DESC);

-- DART 공시 (collect_disclosures.py)
CREATE TABLE IF NOT EXISTS disclosures (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code    TEXT NOT NULL,
  corp_code     TEXT NOT NULL,
  date          TEXT NOT NULL,
  title         TEXT NOT NULL,
  report_nm     TEXT,
  rcept_no      TEXT UNIQUE NOT NULL,
  pblntf_ty     TEXT,
  disclosure_cat TEXT,
  sentiment     INTEGER DEFAULT 0,               -- -2 ~ +2 (내부 기록용, 프론트 미노출)
  summary       TEXT,
  source_url    TEXT,
  source        TEXT DEFAULT 'DART',              -- 'DART' | 'KIND' (REQ-20260415-REQ-001)
  period_start  TEXT,                               -- 기간성 공시 시작일 (YYYY-MM-DD)
  period_end    TEXT,                               -- 기간성 공시 종료일 (YYYY-MM-DD)
  condition_text TEXT,                              -- 조건부 규제 텍스트 (예: 종가 5,000원 이상 시)
  regulation_period TEXT,                           -- 조건 충족 시 적용 기간 요약
  created_at    TEXT NOT NULL,
  FOREIGN KEY (stock_code) REFERENCES stocks(code)
);

CREATE INDEX IF NOT EXISTS idx_disc_stock_date ON disclosures(stock_code, date);
CREATE INDEX IF NOT EXISTS idx_disc_date ON disclosures(date);

-- DART 법인코드 ↔ 종목코드 매핑
CREATE TABLE IF NOT EXISTS dart_corp_map (
  corp_code     TEXT PRIMARY KEY,
  stock_code    TEXT,
  corp_name     TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- 토구사(Togusa) 검증 결과 — 매매 관점 종목별 판정
CREATE TABLE IF NOT EXISTS togusa_verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  stock_code TEXT NOT NULL,
  verdict TEXT NOT NULL,
  rules_json TEXT NOT NULL,
  is_theme_leader INTEGER DEFAULT 0,
  theme_leader_rank INTEGER,
  source TEXT DEFAULT 'togusa',
  created_at TEXT NOT NULL,
  UNIQUE(date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_togusa_date ON togusa_verdicts(date DESC);

-- 토구사(Togusa) 테마별 검증 결과
CREATE TABLE IF NOT EXISTS togusa_theme_verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  theme_name TEXT NOT NULL,
  leader_code TEXT,
  leader_days INTEGER DEFAULT 0,
  theme_strength TEXT,
  narrative_check TEXT DEFAULT 'pass',
  warn_reason TEXT,
  source TEXT DEFAULT 'togusa',
  created_at TEXT NOT NULL,
  UNIQUE(date, theme_name)
);

CREATE INDEX IF NOT EXISTS idx_togusa_theme_date ON togusa_theme_verdicts(date DESC);

-- 휴지(Hugepark) 최종 게이트 — GO/NOGO 판정
CREATE TABLE IF NOT EXISTS hugepark_gate (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  stock_code TEXT NOT NULL,
  grade TEXT NOT NULL,
  reason TEXT,
  source TEXT DEFAULT 'hugepark',
  created_at TEXT NOT NULL,
  UNIQUE(date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_hugepark_date ON hugepark_gate(date DESC);

-- 대표 알림 (시장 경고 등)
CREATE TABLE IF NOT EXISTS owner_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  acknowledged INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_date ON owner_alerts(date DESC);

-- DART API 사용량 로그 (일일 한도 40,000건)
CREATE TABLE IF NOT EXISTS dart_api_usage (
  date          TEXT PRIMARY KEY,             -- YYYY-MM-DD
  call_count    INTEGER DEFAULT 0,
  last_call_at  TEXT                          -- ISO8601
);

-- Gemini API 사용량 로그 (경영지원팀: 일일 카운터)
CREATE TABLE IF NOT EXISTS gemini_usage (
  date          TEXT PRIMARY KEY,             -- YYYY-MM-DD
  call_count    INTEGER DEFAULT 0,
  input_tokens  INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  est_cost_usd  REAL DEFAULT 0.0
);

-- 분봉 스냅샷 (ka10080 — sparkline 렌더용, 당일 스냅샷)
CREATE TABLE IF NOT EXISTS intraday_snapshot (
  date          TEXT NOT NULL,                -- YYYY-MM-DD
  stock_code    TEXT NOT NULL,
  step_min      INTEGER NOT NULL,             -- 분봉 간격 (예: 10)
  open          INTEGER NOT NULL,             -- 기준가 (시가)
  prices_json   TEXT NOT NULL,                -- JSON array: [int,...]
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_intraday_date ON intraday_snapshot(date DESC);

-- takedown 요청 로그 (법무팀 SOP)
CREATE TABLE IF NOT EXISTS takedowns (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  requested_at  TEXT NOT NULL,
  requester     TEXT NOT NULL,                -- 요청 주체
  url           TEXT NOT NULL,
  action        TEXT NOT NULL,                -- deleted / disputed
  completed_at  TEXT
);

-- LLM 호출 캐시 (REQ-20260420-REQ-003)
-- 같은 입력·같은 모델이면 같은 결과 보장. domain별 분리 (ishikawa/togusa/macro/...)
CREATE TABLE IF NOT EXISTS llm_cache (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  domain        TEXT NOT NULL,                  -- ishikawa_news / togusa_news / extract_macros / ...
  target_id     TEXT NOT NULL,                  -- 종목코드, 날짜 등 도메인 고유 식별자
  input_hash    TEXT NOT NULL,                  -- sha256(정규화된 입력)
  model_version TEXT NOT NULL,                  -- 모델명 (변경 시 자동 invalidate)
  response_json TEXT NOT NULL,                  -- LLM 원본 응답
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  hit_count     INTEGER DEFAULT 0,              -- 이 캐시 항목이 HIT된 횟수 (재사용 횟수)
  UNIQUE(domain, target_id, input_hash, model_version)
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_lookup ON llm_cache(domain, target_id);
CREATE INDEX IF NOT EXISTS idx_llm_cache_created ON llm_cache(created_at DESC);

-- 종목 경보 이력 — 임무 D (DOC-20260423-FLR-002 후속)
-- disclosures 기반으로 투자주의/경고/위험/단기과열 지정·해제·예고 시계열 복원.
-- 투자위험 predicted 경로(투자경고 지정 10거래일 내 재급등) 판정용.
CREATE TABLE IF NOT EXISTS stock_alert_history (
  stock_code        TEXT NOT NULL,                       -- FK stocks.code
  date              TEXT NOT NULL,                       -- 이벤트 발생일 (disclosures.date)
  stage             TEXT NOT NULL,                       -- 투자주의/투자경고/투자위험/단기과열
  event_type        TEXT NOT NULL,                       -- designated|released|notice
  source            TEXT NOT NULL DEFAULT 'disclosure',  -- disclosure|predicted|manual
  raw_disclosure_id INTEGER,                             -- FK disclosures.id
  period_start      TEXT,                                -- 지정 시작일
  period_end        TEXT,                                -- 해제 예정일 (NULL 허용)
  created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (stock_code, date, stage, event_type),
  FOREIGN KEY (raw_disclosure_id) REFERENCES disclosures(id)
);
CREATE INDEX IF NOT EXISTS idx_sah_stock_stage
  ON stock_alert_history(stock_code, stage, date DESC);
CREATE INDEX IF NOT EXISTS idx_sah_date
  ON stock_alert_history(date);
CREATE INDEX IF NOT EXISTS idx_sah_date_stage
  ON stock_alert_history(date, stage);

-- 종목 상태 배지 SoT (REQ-20260429-REQ-004 / DOC-20260429-REQ-004)
-- 기존: status_badges는 build_daily.py가 JSON으로만 산출 → SQL 필터 표현 불가.
-- 신설: 상한가/공시조치/단기과열/VI 등 모든 상태 배지를 DB에 영속화.
-- 정합 룰 (FLR-20260428-TEC-001 회피): build_daily.py의 단일 함수에서
--   JSON 산출 ⊕ DB UPSERT 동시 수행. 양 끝 동시 변경 강제.
-- badge_type: '상한가' | '하한가' | 'VI' | '신고가240d' | '투자주의' | '투자경고'
--             | '투자경고 예고' | '투자위험' | '투자위험 근접' | '단기과열' | '거래정지' ...
-- source:     'kiwoom_ka10017' | 'disclosure' | 'predicted' | 'pipeline_high'
-- payload_json: 응답 원본/계산값 (예: 상한가 cur_prc/flu_rt/trde_qty/trde_prica)
-- REQ-080 interval 모델 (2026-04-29 전환):
--   active_until IS NULL  → 현재 활성 (chip 노출 후보)
--   active_until IS NOT NULL → 풀림 (history 보존)
--   재진입 시 새 row INSERT (id auto, UNIQUE는 active_from 포함)
-- source 정책:
--   pipeline_chg     영구 활성 (EOD)
--   kiwoom_ka10017   풀림 가능 (장중)
--   disclosure       영구 활성
--   pipeline_high    영구 활성 (240d 신고가)
CREATE TABLE IF NOT EXISTS stock_status_badges (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  date          TEXT NOT NULL,
  stock_code    TEXT NOT NULL,
  badge_type    TEXT NOT NULL,
  source        TEXT NOT NULL,
  payload_json  TEXT,
  active_from   TEXT NOT NULL,
  active_until  TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (date, stock_code, badge_type, source, active_from)
);
CREATE INDEX IF NOT EXISTS idx_ssb_current
  ON stock_status_badges(date, badge_type, active_until);
CREATE INDEX IF NOT EXISTS idx_ssb_stock
  ON stock_status_badges(stock_code, date DESC);
CREATE INDEX IF NOT EXISTS idx_ssb_source
  ON stock_status_badges(source, date);

-- 거래대금 정렬 covering 인덱스 (REQ-080 §2.2)
-- 용도: build_daily 정렬, "오늘의 종목" 거래대금 역순 페이지네이션.
-- covering: date+trade_amount DESC+stock_code → SELECT date·stock_code·trade_amount
-- 만 사용하는 쿼리에서 테이블 random IO 회피 (TEMP B-TREE 부재 EXPLAIN 검증).
CREATE INDEX IF NOT EXISTS idx_picks_date_amount
  ON daily_picks(date, trade_amount DESC, stock_code);

-- 보조 인덱스 (REQ-080 §2.3 P1)
CREATE INDEX IF NOT EXISTS idx_disc_date_stock
  ON disclosures(date, stock_code);
