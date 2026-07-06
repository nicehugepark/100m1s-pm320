"""SQLite 헬퍼 — WAL 모드 + 스키마 초기화."""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import DART_CALL_WARN_THRESHOLD, DART_DAILY_CALL_LIMIT, DATA_DIR, DB_PATH

SCHEMA = Path(__file__).parent / "schema.sql"
logger = logging.getLogger(__name__)

# Phase 5 (REQ-20260420-REQ-004): 테마뉴스 분리 — themes.db read-only ATTACH
# 메인은 themes.db에 절대 write 금지. 부재 시 ATTACH skip (graceful degrade).
THEMES_DB_PATH = DATA_DIR / "themes.db"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # REQ-076 Phase 2-B — 동일 프로세스 내 중첩 connect()가 트랜잭션 진행 중인
    # 다른 connection의 commit을 기다릴 수 있도록 busy_timeout 설정.
    # interpret() 내부 `with connect()` 블록 안에서 link_stock_theme이 새 connect를
    # 여는 패턴이 sandbox 환경에서 즉시 LOCKED으로 표면화 → 5초 대기로 정상 commit.
    conn.execute("PRAGMA busy_timeout=5000")
    _attach_themes_readonly(conn)
    return conn


def _attach_themes_readonly(conn):
    """themes.db를 read-only로 ATTACH. 부재 시 skip (Phase 5 미배포 환경 대응).

    메인 코드는 `themes_db.theme_map` / `themes_db.macro_summary` 등을 SELECT만 가능.
    INSERT/UPDATE/DELETE는 SQLite가 'attempt to write a readonly database'로 차단.
    """
    if not THEMES_DB_PATH.exists():
        return  # Phase 5 themes-pipeline 미가동 환경: skip
    try:
        conn.execute(f"ATTACH DATABASE 'file:{THEMES_DB_PATH}?mode=ro' AS themes_db")
    except sqlite3.Error as exc:
        logger.warning("themes.db ATTACH skip: %s", exc)


def init_schema():
    with connect() as conn:
        conn.executescript(SCHEMA.read_text())
        _migrate_disclosures_source(conn)
        _ensure_llm_cache(conn)
        _ensure_stock_alert_history(conn)
        _ensure_index_dailybars(conn)
        _ensure_theme_parents(conn)
        _ensure_stock_theme_overrides(conn)
        _ensure_stock_status_badges(conn)
        _ensure_news_fetch_log(conn)
        conn.commit()


def _ensure_news_fetch_log(conn):
    """news_fetch_log 테이블 보장 (DOC-20260531 — interpret_stocks 본문 fetch 메트릭 layer).

    fetch_article_body() 호출 직후 매체·URL·body_len·success·selector_hit 1행 insert.
    매체별 실 실패율을 가시화하여 후속 결정 (예: 매체별 셀렉터 dict 신설 여부)의
    근거 데이터로 활용. 1주일 누적 후 매체별 실패율 SELECT 후 evidence 기반 결정.

    저장 정책:
    - 모든 fetch 시도 (성공 + 실패 + timeout + HTTP error) 1행 기록
    - DB write 부하 최소화 — 1행 insert만, batch 0건
    - 오래된 행은 cron audit (예: 30일 이전 자동 삭제) 별도 의무
    - 메인 DB write 부하 risk: 일 ~수백건 호출 추정 (5/24~5/31 기준 일 ~470건),
      30일 누적 ~14k행 = 무시 가능 (llm_cache 412 entries / hit_count 424 비교)

    cumulative recurring 19회차 lead 환각 직후 cycle session 신축 — evidence-first
    원칙 정합. 매체별 셀렉터 dict 신축은 본 메트릭 1주일 누적 후 결정 (FLR-AGT-002
    동형 증명 부재 셀렉터 신축 회피).

    근거: DOC-20260531 (본 임무), lead-meta §11.6 evidence 4종 (commit / file path /
    시각 / 행위자 명시 의무), FLR-AGT-002 거짓 충실성 hub.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news_fetch_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at    TEXT NOT NULL,
            url           TEXT NOT NULL,
            source        TEXT,
            selector_hit  TEXT,
            body_len      INTEGER,
            success       INTEGER NOT NULL DEFAULT 0,
            error_msg     TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nfl_source_date "
        "ON news_fetch_log(source, fetched_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nfl_success "
        "ON news_fetch_log(success, fetched_at DESC)"
    )


def _ensure_stock_status_badges(conn):
    """stock_status_badges 테이블 + 인덱스 보장 (REQ-080 §2.1 + interval v2).

    DOC-20260429-REQ-004 §2 + 2026-04-29 interval 모델 전환.

    interval 모델 (active_from/active_until):
      - active_until IS NULL  → 현재 활성 (chip 노출 후보)
      - active_until IS NOT NULL → 풀림 (history 보존)
      - 재진입 시 새 row INSERT (PK auto, UNIQUE는 active_from 포함)

    source별 정책:
      - 'pipeline_chg'   : 영구 활성 (EOD 종가 ≥29.5%, 풀림 없음)
      - 'kiwoom_ka10017' : 풀림 가능 (장중 응답 사라지면 active_until 설정)
      - 'disclosure'     : 영구 활성 (공시 기반)
      - 'pipeline_high'  : 영구 활성 (240d 신고가)

    재발 방지 룰 (FLR-20260428-TEC-001):
      build_daily.py 또는 collect_*.py 단일 함수에서 JSON ⊕ DB UPSERT 동시 수행.

    UNIQUE: (date, stock_code, badge_type, source, active_from)
      active_from에 마이크로초 포함하여 race condition 회피.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_status_badges (
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
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ssb_current "
        "ON stock_status_badges(date, badge_type, active_until)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ssb_stock "
        "ON stock_status_badges(stock_code, date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ssb_source ON stock_status_badges(source, date)"
    )
    # 거래대금 정렬 covering 인덱스 (REQ-080 §2.2 P0)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_picks_date_amount "
        "ON daily_picks(date, trade_amount DESC, stock_code)"
    )
    # 보조 인덱스 (REQ-080 §2.3 P1)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_disc_date_stock ON disclosures(date, stock_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sah_date_stage ON stock_alert_history(date, stage)"
    )


def _ensure_theme_parents(conn):
    """theme_parents 테이블 보장 (REQ-076 Phase 1 — 다중 부모 N:M 매핑).

    DOC-20260428-REQ-005 §3.1.
    기존 themes.parent_id 단일 컬럼이 매크로 이벤트 부모(예: "전쟁재건")와
    산업 대분류 부모(예: "건설")를 동시 표현하지 못하는 결함을 해소한다.

    Phase 1 범위: 데이터 레이어 신설만. 모든 read 경로는 잔존 themes.parent_id를
    계속 사용 (행동 변경 0). Phase 3에서 read 경로를 theme_parents로 점진 이전.

    weight: 동일 child가 여러 parent를 가질 때 매크로 우세도 (0.0~1.0). 기본 1.0.
    source: 'migrated'(기존 parent_id 복제) / 'ishikawa' / 'togusa' / 'owner'.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS theme_parents (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            child_id      INTEGER NOT NULL,
            parent_id     INTEGER NOT NULL,
            weight        REAL NOT NULL DEFAULT 1.0,
            source        TEXT NOT NULL DEFAULT 'migrated',
            created_at    TEXT NOT NULL,
            UNIQUE(child_id, parent_id),
            FOREIGN KEY (child_id) REFERENCES themes(id),
            FOREIGN KEY (parent_id) REFERENCES themes(id)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tp_child ON theme_parents(child_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tp_parent ON theme_parents(parent_id)")


def _ensure_stock_theme_overrides(conn):
    """stock_theme_overrides 테이블 보장 (DOC-20260530-REQ-002 — 대표 권한 SSOT).

    종목→테마 수동 오버라이드. industry_seeds 등 자동 부착 경로가 매 interpret마다
    종목 업종→테마를 재부여해 수동 정정이 덮이는 결함을 봉쇄한다.

    - deny_theme_id  : 이 종목에 이 theme_id 부착을 차단 (link_stock_theme 가드).
    - force_theme_id : interpret 종료 시 이 theme_id를 source='owner'로 강제 부착.
    - source         : 'owner' 고정 (대표 결정). 재해석/cron이 절대 덮지 못함.

    한 row는 deny 또는 force 중 하나 이상을 가진다. 같은 종목에 deny·force 다건 허용.
    UNIQUE(stock_code, deny_theme_id, force_theme_id)로 중복 INSERT 방지(재호출 안전).
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_theme_overrides (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code     TEXT NOT NULL,
            deny_theme_id  INTEGER,
            force_theme_id INTEGER,
            source         TEXT NOT NULL DEFAULT 'owner',
            created_at     TEXT NOT NULL,
            UNIQUE(stock_code, deny_theme_id, force_theme_id),
            FOREIGN KEY (deny_theme_id) REFERENCES themes(id),
            FOREIGN KEY (force_theme_id) REFERENCES themes(id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sto_code ON stock_theme_overrides(stock_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sto_deny "
        "ON stock_theme_overrides(stock_code, deny_theme_id)"
    )


def _ensure_index_dailybars(conn):
    """index_dailybars 테이블 보장 (임무 C — KOSPI/KOSDAQ 일봉).

    REQ: 투자경고/위험 predicted 경로의 `index_multiple_current` 계산 근거.
    collect_kiwoom_indices(ka20006)가 적재. PK (index_code, date).
    open/high/low/close 는 REAL (스케일 보정: 키움 raw / 100).
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS index_dailybars (
            index_code  TEXT NOT NULL,
            date        TEXT NOT NULL,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL NOT NULL,
            volume      INTEGER,
            PRIMARY KEY (index_code, date)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_idb_date ON index_dailybars(date DESC)"
    )


def _ensure_stock_alert_history(conn):
    """stock_alert_history 테이블이 없으면 생성 (임무 D).

    기존 DB가 schema.sql을 다시 실행하지 않고도 이력 적재 가능하도록 보장.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_alert_history (
            stock_code        TEXT NOT NULL,
            date              TEXT NOT NULL,
            stage             TEXT NOT NULL,
            event_type        TEXT NOT NULL,
            source            TEXT NOT NULL DEFAULT 'disclosure',
            raw_disclosure_id INTEGER,
            period_start      TEXT,
            period_end        TEXT,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (stock_code, date, stage, event_type)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sah_stock_stage "
        "ON stock_alert_history(stock_code, stage, date DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sah_date ON stock_alert_history(date)")


def _ensure_llm_cache(conn):
    """llm_cache 테이블이 없으면 생성 (REQ-20260420-REQ-003).

    기존 DB가 schema.sql을 다시 실행하지 않고도 캐시 사용 가능하도록 보장.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS llm_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            target_id TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            model_version TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            hit_count INTEGER DEFAULT 0,
            UNIQUE(domain, target_id, input_hash, model_version)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_cache_lookup ON llm_cache(domain, target_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_cache_created ON llm_cache(created_at DESC)"
    )


def _migrate_disclosures_source(conn):
    """disclosures.source 컬럼 추가 마이그레이션 (REQ-20260415-REQ-001).

    기존 DB: 컬럼 없음 → ADD COLUMN (DEFAULT 'DART'로 기존 레코드 유지).
    신규 DB: schema.sql로 이미 생성됨 → OperationalError skip.
    """
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(disclosures)")]
        if "source" not in cols:
            conn.execute(
                "ALTER TABLE disclosures ADD COLUMN source TEXT DEFAULT 'DART'"
            )
            conn.execute("UPDATE disclosures SET source='DART' WHERE source IS NULL")
            logger.info("disclosures.source 컬럼 추가 완료 (기존 레코드 DART로 유지)")
    except sqlite3.OperationalError as e:
        logger.warning("disclosures 마이그레이션 skip: %s", e)


# ── DART API rate limiting ──────────────────────────────────


def dart_api_check(conn, calls_needed: int = 1) -> bool:
    """DART API 호출 가능 여부 확인. 한도 초과 시 False 반환.

    Args:
        conn: sqlite3 연결 (dart_api_usage 테이블 필요)
        calls_needed: 이번에 필요한 호출 수

    Returns:
        True면 호출 가능, False면 한도 도달.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dart_api_usage (
            date TEXT PRIMARY KEY, call_count INTEGER DEFAULT 0, last_call_at TEXT
        )"""
    )
    row = conn.execute(
        "SELECT call_count FROM dart_api_usage WHERE date=?", (today,)
    ).fetchone()
    current = row["call_count"] if row else 0

    if current + calls_needed > DART_DAILY_CALL_LIMIT:
        logger.error(
            "DART API 일일 한도 도달: %d/%d (요청 %d건 불가)",
            current,
            DART_DAILY_CALL_LIMIT,
            calls_needed,
        )
        return False

    if current + calls_needed > DART_CALL_WARN_THRESHOLD:
        logger.warning(
            "DART API 일일 한도 근접: %d/%d",
            current + calls_needed,
            DART_DAILY_CALL_LIMIT,
        )

    return True


def dart_api_increment(conn, calls: int = 1):
    """DART API 호출 카운트 증가. 호출 성공 후 반드시 호출."""
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO dart_api_usage (date, call_count, last_call_at)
           VALUES (?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
             call_count = call_count + ?,
             last_call_at = ?""",
        (today, calls, now, calls, now),
    )
    conn.commit()


if __name__ == "__main__":
    init_schema()
    print(f"schema initialized at {DB_PATH}")
