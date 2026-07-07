"""
테마 일별 통계 집계 + theme-trend.json 정적 빌드.
daily_picks × stock_themes → theme_daily_stats → JSON.
"""

# Python 3.9 런타임에서 `Path | None` 등 PEP 604 함수 어노테이션 평가 차단
# (def 평가 시점 TypeError 회피). 모든 어노테이션 lazy 문자열화.
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, HOMEPAGE
from .db import connect


def _backfill_db_path(conn) -> Path | None:
    """보강용 백필 DB 경로 결정 (단일 SSOT — ATTACH·보강 헬퍼 공유).

    반환:
        Path = 사용 가능한 백필 DB 경로 (존재 + 서빙 DB 와 다른 파일)
        None = 미설정/명시적 비활성화/파일 부재/서빙 자기 자신 → skip

    경로 규칙 (_attach_backfill_dailybars 와 동일):
      - M1S_DAILYBARS_BACKFILL_DB 미설정 → 기본 = pm320 레포 로컬 서빙 DB(config.DATA_DIR/stocks.db).
        기본값이 서빙 자기 자신이라 아래 "동일 경로면 None" 가드로 자연히 skip
        (별도 백필 DB 를 붙이려면 env 로 명시 override). 유령 클론·메인레포 경로 참조 금지.
      - "" (빈 문자열) → None (명시적 비활성화)
      - 그 외 → 해당 경로
    서빙 DB 와 동일 resolve 경로면 union 의미 없음 → None.
    """
    env_path = os.environ.get("M1S_DAILYBARS_BACKFILL_DB")
    if env_path is None:
        backfill_path = DATA_DIR / "stocks.db"
    elif env_path == "":
        return None
    else:
        backfill_path = Path(env_path)

    if not backfill_path.exists():
        return None
    # 자기 자신(서빙 DB)과 동일 경로면 union 의미 없음 → skip.
    try:
        serving_path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
        if serving_path.resolve() == backfill_path.resolve():
            return None
    except Exception:
        pass
    return backfill_path


def _load_backfill_supplements(conn, codes, dates):
    """백필 DB 에서 종목명·거래대금 보강 dict 를 read-only 로 로드.

    ROOT (2026-06-18, 백필 종목 종목명=코드·거래대금 빈칸): chain SQL 의 base 는
    백필 dailybars union(_bars_src)으로 확장됐지만, 종목명(stocks)·거래대금
    (daily_picks/dailybars trade_amount) 보강 단계는 **서빙 DB 만** 조회한다.
    서빙 universe(~40종) 밖에서 백필 union 으로 들어온 상한가 종목은 서빙
    stocks/daily_picks/dailybars 에 행이 없어 name=코드(fallback)·trade_amount=NULL
    (프론트 빈칸). 백필 DB 는 전종목 누적이라 동일 code/(code,date) 의 종목명과
    dailybars.trade_amount 를 보유 → read-only 보강.

    설계 원칙 (_attach_backfill_dailybars 와 동일):
      - **비파괴·read-only**: ?mode=ro 로만 open, SELECT 만 (write 0건).
      - **API 호출 0 증가**: 백필 DB 기존 적재 데이터만 조회.
      - **graceful**: 경로 None/파일 부재/테이블 부재/조회 실패 시 빈 dict 반환
        → 서빙 단독 동작(기존) 유지 = 회귀 0.
      - **서빙 우선 불변**: 본 함수는 *보강용* dict 만 반환. 호출부에서 서빙
        값이 이미 있으면 덮어쓰지 않는다(name_map.setdefault / ta is None 가드).

    Args:
        codes: 보강 대상 종목코드 iterable (chain ∪ daily_picks 결과 전체).
        dates: 보강 대상 거래일 iterable (target_dates_asc).

    Returns:
        (name_supp, ta_supp)
          name_supp: {code: name}            — 백필 stocks 종목명
          ta_supp:   {(code, date): trade_amount} — 백필 dailybars 거래대금
    """
    name_supp: dict[str, str] = {}
    ta_supp: dict[tuple[str, str], int] = {}
    codes = list(codes)
    dates = list(dates)
    if not codes:
        return name_supp, ta_supp
    backfill_path = _backfill_db_path(conn)
    if backfill_path is None:
        return name_supp, ta_supp
    try:
        bconn = sqlite3.connect(f"file:{backfill_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        print(f"[limit-up-trend] 백필 보강 open skip ({e}) — 서빙 단독 보강")
        return name_supp, ta_supp
    try:
        code_ph = ",".join("?" for _ in codes)
        # 종목명 보강 (백필 stocks).
        try:
            for row in bconn.execute(
                f"SELECT code, name FROM stocks WHERE code IN ({code_ph})", codes
            ):
                if row[1]:
                    name_supp[row[0]] = row[1]
        except sqlite3.OperationalError as e:
            print(f"[limit-up-trend] 백필 stocks 보강 skip ({e})")
        # 거래대금 보강 (백필 dailybars.trade_amount, 대상 날짜만).
        if dates:
            date_ph = ",".join("?" for _ in dates)
            try:
                for row in bconn.execute(
                    f"SELECT code, date, trade_amount FROM dailybars "
                    f"WHERE code IN ({code_ph}) AND date IN ({date_ph}) "
                    f"AND trade_amount IS NOT NULL",
                    codes + dates,
                ):
                    ta_supp[(row[0], row[1])] = row[2]
            except sqlite3.OperationalError as e:
                print(f"[limit-up-trend] 백필 dailybars 거래대금 보강 skip ({e})")
    finally:
        bconn.close()
    return name_supp, ta_supp


def _attach_backfill_dailybars(conn) -> bool:
    """백필(전종목 누적) DB의 dailybars 를 read-only 로 ATTACH (별칭 backfill).

    ROOT (2026-06-18, 상한가 과소수집): cron 서빙 DB(M1S_HOMEPAGE/data/stocks.db)의
    dailybars 는 당일 union universe(latest_stocks ∪ carded, ~40종) 만 적재한다.
    폭등장에 그 universe 밖에서 상한가가 다수 발생하면(예: 6/11 8종 중 7종이
    서빙 DB dailybars 부재) chain SQL 이 잡지 못해 limit-up-trend.json 과소수집.
    별도 백필 DB(env M1S_DAILYBARS_BACKFILL_DB 로 지정, 전종목 누적본)는 동일
    날짜에 더 많은 종목 dailybars 를 보유 → read-only union 으로 누락 보강.

    설계 원칙:
      - **비파괴·read-only**: ATTACH 후 SELECT 만. 백필 DB 에 write 0건
        (SQLite 가 readonly 위반을 'attempt to write a readonly database' 로 차단).
      - **API 호출 0 증가**: cron 수집 universe 불변, 기존 적재 데이터만 합집합.
      - **graceful**: env 미설정·파일 부재·서빙 DB 와 동일 경로(자기 자신)·ATTACH
        실패 시 모두 skip → 서빙 단독 chain (기존 동작 = 회귀 0).
      - **env 옵션화**: M1S_DAILYBARS_BACKFILL_DB 로 경로 override. 미설정 시
        기본값 = pm320 로컬 서빙 DB(자기 자신 → skip). 명시적으로 비활성화하려면
        M1S_DAILYBARS_BACKFILL_DB="" (빈 문자열) 설정.

    Returns:
        True  = ATTACH 성공 (chain SQL 이 backfill.dailybars 참조 가능)
        False = skip (서빙 단독)
    """
    # 경로 결정·존재·자기자신 가드는 _backfill_db_path 단일 SSOT 재사용
    # (FLR-20260406-TEC-001 동형 회피 — 경로 규칙이 두 곳에 분산되면 한쪽만
    #  고쳐지는 사고. ATTACH·보강 헬퍼 모두 동일 경로 로직을 공유한다).
    backfill_path = _backfill_db_path(conn)
    if backfill_path is None:
        return False

    try:
        conn.execute(
            "ATTACH DATABASE ? AS backfill", (f"file:{backfill_path}?mode=ro",)
        )
        # 실제로 dailybars 테이블이 있는지 가벼운 확인 (없으면 union 무의미).
        conn.execute("SELECT 1 FROM backfill.dailybars LIMIT 1")
        return True
    except sqlite3.OperationalError as e:
        print(f"[limit-up-trend] 백필 dailybars ATTACH skip ({e}) — 서빙 단독 chain")
        try:
            conn.execute("DETACH DATABASE backfill")
        except sqlite3.OperationalError:
            pass
        return False


def _json_dates_count(path: Path) -> int:
    """기존 JSON 파일의 dates 항목 수 반환. 파일 없거나 파싱 실패 시 0."""
    try:
        if path.exists():
            d = json.loads(path.read_text())
            return len(d.get("dates", []) or d.get("items", []))
    except Exception:
        pass
    return 0


def _json_dates_list(path: Path) -> list[str]:
    """기존 JSON 파일의 dates 목록 반환. 파일 없거나 파싱 실패 시 []."""
    try:
        if path.exists():
            d = json.loads(path.read_text())
            return list(d.get("dates", []) or d.get("items", []))
    except Exception:
        pass
    return []


def _load_theme_trend_historical(out_path: Path, trend_business_days: int) -> dict:
    """기존 theme-trend.json에서 historical baseline 로드.

    cron DB에 historical 데이터가 없을 때(신규 설치·초기 구동·cron DB truncated 상황)
    기존 JSON의 themes[].data 엔트리를 슬라이딩 윈도우 baseline으로 보존한다.

    반환값:
        {root_id: {date: entry_dict}}  — 기존 파일 없거나 파싱 실패 시 {}

    NOTE: 두 서빙 DB의 theme_id divergence(같은 name이 다른 id로 존재)를 해소하기 위해
    HISTORICAL-ONLY-ROOT 복원 블록에서는 root_name 기준 병합을 추가로 수행한다.
    본 함수는 id 키 반환을 유지하되, 호출부에서 name→id 역매핑으로 dedup한다.

    사용 패턴 (build_json 내):
        historical = _load_theme_trend_historical(out_path, TREND_BUSINESS_DAYS)
        # cron DB 신규 분으로 UPSERT (cron 우선)
        for t in trend_themes:
            hist = historical.get(t["id"], {})
            cron_dates = {e["date"] for e in t["data"]}
            for hist_date, hist_entry in hist.items():
                if hist_date not in cron_dates:
                    t["data"].append(hist_entry)
            t["data"].sort(key=lambda d: d["date"])
        # dates_set도 병합 후 최근 N일 슬라이딩
    """
    if not out_path.exists():
        return {}
    try:
        d = json.loads(out_path.read_text())
        existing_dates = d.get("dates", [])
        themes = d.get("themes", [])
        # 최근 trend_business_days 날짜만 보존 (슬라이딩 윈도우)
        cutoff_dates = set(sorted(existing_dates, reverse=True)[:trend_business_days])
        result = {}
        for t in themes:
            tid = t.get("id")
            if tid is None:
                continue
            for entry in t.get("data", []):
                d_str = entry.get("date")
                if d_str and d_str in cutoff_dates:
                    result.setdefault(tid, {})[d_str] = entry
        return result
    except Exception:
        return {}


def _safe_write_json(
    path: Path, out: dict, new_dates_key: str = "dates", target_window: int = 0
) -> bool:
    """슬라이딩 윈도우·신규 추가는 허용, 대량 소실(회귀)만 차단.

    허용 조건:
      1. 기존 파일 없음 (신규 생성)
      2. count 증가/유지 (new >= existing) — 정상 증분 또는 슬라이딩 윈도우
      3. count 감소지만: 날짜 전진 AND 기존 dates 80%+ 보존
         (슬라이딩 윈도우: 가장 오래된 1개 빠지고 1개 추가)
      4. count 감소지만: 목표 윈도우 크기 도달(new >= target_window) AND 80%+ 보존
         (날짜 미전진 허용 — historical 병합 후 슬라이딩 안착, 동일 날짜 latest 유지)
         target_window=0이면 조건 4 비활성.

    차단 조건 (회귀):
      - count 감소 AND (날짜 미전진 OR 기존 dates 80% 미만 보존)
        → 사고 패턴: 기존 21일치를 1건이 덮는 경우

    Returns:
        True  = 실제 쓰기 완료
        False = SKIP (회귀 차단)
    """
    new_dates = list(out.get(new_dates_key, []) or out.get("items", []))
    new_count = len(new_dates)
    existing_dates = _json_dates_list(path)
    existing_count = len(existing_dates)

    # 기존 파일 없으면 무조건 쓰기
    if existing_count == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        return True

    # 허용: count 증가 또는 유지
    if new_count >= existing_count:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        return True

    # count 감소 케이스 — 공통 메트릭 계산
    new_latest = max(new_dates) if new_dates else ""
    existing_latest = max(existing_dates) if existing_dates else ""
    date_advanced = new_latest > existing_latest

    existing_set = set(existing_dates)
    new_set = set(new_dates)
    preserved = len(existing_set & new_set)
    preserve_ratio = preserved / existing_count if existing_count > 0 else 0.0

    if date_advanced and preserve_ratio >= 0.8:
        # 슬라이딩 윈도우 정상 패턴: 최신 날짜 추가 + 오래된 날짜 일부 제거
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        return True

    if target_window > 0 and new_count >= target_window and preserve_ratio >= 0.8:
        # 목표 윈도우 안착 패턴: historical 병합 후 슬라이딩 윈도우가 target_window에
        # 도달 + 80%+ 보존. 날짜 미전진 허용 (same-day re-run, 기존 최신 == 신규 최신).
        # 1건 덮기 사고 차단: 1 < target_window(20) 이므로 이 분기 진입 불가.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        return True

    # 차단: 대량 소실 또는 날짜 후퇴
    print(
        f"[JSON-GUARD] SKIP write {path.name}: "
        f"new dates={new_count} (latest={new_latest}) < existing={existing_count} (latest={existing_latest}), "
        f"보존율={preserve_ratio:.0%} — 회귀 차단"
        f"{' (날짜 미전진)' if not date_advanced else ' (80% 미만 소실)'}"
    )
    return False


def _load_latest_stocks_per_date(dates: Iterable[str]) -> dict[str, set[str]]:
    """일자별 `latest_stocks` ticker 집합 로드 — 카드 SSOT (Q-20260514-058 5/14 결정).

    `kiwoom/{date}.json` 의 `latest_stocks` 배열 (마지막 snapshot ≥500억 satisfier)이
    프론트 카드 렌더의 1차 SSOT (`renderer.js:349 latest_stocks || daily_top`,
    `build_daily.py:1882 latest_stocks or daily_top or stocks`). 트렌드 차트는
    그동안 `daily_picks WHERE source='kiwoom'` 전체 (snapshot 누적 50종 + LU union)
    를 합산 → 카드와 source mismatch (5/27 반도체 18종 3.79조 차트 vs 6종 2.50조
    카드). 이 helper = 차트 source 정합용 ticker subset filter SoT.

    fallback 정책:
    - latest_stocks 부재 (JSON 파일 자체 없음 / latest_stocks 키 누락) → 빈 set 반환
      → 호출측이 fallback (legacy 일자 = 전체 kiwoom source).
    - 영웅식(`heroshik_strict_*`) source 존재 일자는 호출측에서 우선 적용
      (heroshik 정합이 더 정확).
    """
    out: dict[str, set[str]] = {}
    kiwoom_dir = HOMEPAGE / "data" / "kiwoom"
    for date in dates:
        path = kiwoom_dir / f"{date}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        ls = data.get("latest_stocks") or []
        codes = {s.get("ticker") for s in ls if isinstance(s, dict) and s.get("ticker")}
        if codes:
            out[date] = codes
    return out


def _load_stock_card_codes_per_date(dates: Iterable[str]) -> dict[str, set[str]]:
    """일자별 `stock-{date}.json` (카드 SSOT 최종 표출) ticker 집합 로드.

    Q-20260604-DRILLDOWN-FALLBACK (build_tree_json 동형, 2026-06-04 대표 catch
    "정치테마 비활성") — `latest_stocks` 8건만으로 차트 합산하면 카드 SSOT 최종
    표출 set (`stock-{date}.json:stocks[].code` = `latest_stocks || daily_top || stocks`
    cascade 결과, build_daily.py L1882) 와 mismatch. 진양화학 (051630) 6/4 사례:
    - latest_stocks (kiwoom/2026-06-04.json): 8건, 051630 미포함
    - 카드 SSOT (stock-2026-06-04.json): 18건, 051630 포함
    - 결과: 차트 정치테마 root trade=0 + chip 회색 비활성 (오세훈 leaf 진양화학
      drill-down 자연 배제) ↔ 카드 chip "오세훈" 노출 mismatch

    build_tree_json L605-620 동형 helper — chart 합산도 카드 SSOT union 채택.
    """
    out: dict[str, set[str]] = {}
    card_dir = HOMEPAGE / "data" / "interpreted"
    for date in dates:
        path = card_dir / f"stock-{date}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        codes = {
            s.get("code")
            for s in data.get("stocks", [])
            if isinstance(s, dict) and s.get("code")
        }
        if codes:
            out[date] = codes
    return out


# 테마 트렌드 차트에 표시할 영업일 수
# 변경 이력: 14 → 5 (REQ-057-D: DOC-20260414-REQ-001 원안 복구) → 20 (REQ-061:
# 라이브 검증 후 5영업일은 분석 윈도우로 부족하다는 대표 판단, 2026-04-28).
TREND_BUSINESS_DAYS = 20


def aggregate():
    """daily_picks와 stock_themes를 JOIN하여 테마별 일별 거래대금 집계."""
    with connect() as conn:
        # theme_daily_stats 테이블 생성 (없으면)
        conn.execute("""CREATE TABLE IF NOT EXISTS theme_daily_stats (
            date TEXT NOT NULL, theme_id INTEGER NOT NULL,
            stock_count INTEGER DEFAULT 0, total_trade_amount INTEGER DEFAULT 0,
            avg_change_pct REAL DEFAULT 0.0,
            PRIMARY KEY (date, theme_id))""")

        # 기존에 삽입된 휴장일 데이터 삭제 (일회성 정리 + 향후 방어)
        from .config import is_market_holiday

        existing_dates = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT date FROM theme_daily_stats"
            ).fetchall()
        ]
        holiday_dates = [d for d in existing_dates if is_market_holiday(d)]
        if holiday_dates:
            placeholders = ",".join("?" for _ in holiday_dates)
            conn.execute(
                f"DELETE FROM theme_daily_stats WHERE date IN ({placeholders})",
                holiday_dates,
            )
            conn.commit()
            print(
                f"purged {len(holiday_dates)} holiday dates from theme_daily_stats: {holiday_dates}"
            )

        # REQ-076 Phase 4-mini (FLR-TEC-002 §4) — retired_v3 행은 stats 집계 제외
        rows = conn.execute("""
            SELECT dp.date, st.theme_id, t.name as theme_name,
                   COUNT(DISTINCT dp.stock_code) as stock_count,
                   SUM(dp.trade_amount) as total_trade_amount,
                   AVG(dp.change_pct) as avg_change_pct
            FROM daily_picks dp
            JOIN stock_themes st ON st.stock_code = dp.stock_code
            JOIN themes t ON t.id = st.theme_id
            WHERE t.is_active = 1
              AND dp.source = 'kiwoom'
              AND COALESCE(st.source, '') != 'retired_v3'
            GROUP BY dp.date, st.theme_id
            ORDER BY dp.date, total_trade_amount DESC
        """).fetchall()

        # 휴장일(토/일/공휴일) 데이터 제외 — 거짓 데이터 방지
        from .config import is_market_holiday

        rows = [r for r in rows if not is_market_holiday(r["date"])]

        # 잔존 데이터 정리: aggregate 결과에 없는 날짜의 기존 행 삭제.
        # kiwoom_ranking만 있는 날짜가 daily_picks에 추가된 후 aggregate되면
        # NOT IN 필터로 결과에 빠지지만, 기존 행이 UPSERT 대상이 아니라 잔존.
        valid_dates = {r["date"] for r in rows}
        stale = [
            d for d in existing_dates if d not in valid_dates and d not in holiday_dates
        ]
        if stale:
            ph = ",".join("?" for _ in stale)
            conn.execute(f"DELETE FROM theme_daily_stats WHERE date IN ({ph})", stale)
            print(f"purged {len(stale)} stale dates from theme_daily_stats: {stale}")

        # 날짜 내 stale theme 엔트리 정리:
        # stock_themes 변경으로 특정 날짜의 특정 테마가 aggregate 결과에서 빠지면
        # 기존 행이 UPSERT 대상이 아니므로 잔존한다. (date, theme_id) 단위로 삭제.
        valid_keys = {(r["date"], r["theme_id"]) for r in rows}
        for d in valid_dates:
            existing_themes = conn.execute(
                "SELECT theme_id FROM theme_daily_stats WHERE date = ?", (d,)
            ).fetchall()
            stale_themes = [
                t["theme_id"]
                for t in existing_themes
                if (d, t["theme_id"]) not in valid_keys
            ]
            if stale_themes:
                ph = ",".join("?" for _ in stale_themes)
                conn.execute(
                    f"DELETE FROM theme_daily_stats WHERE date = ? AND theme_id IN ({ph})",
                    (d, *stale_themes),
                )
                print(f"purged {len(stale_themes)} stale theme entries for {d}")

        for r in rows:
            conn.execute(
                """INSERT INTO theme_daily_stats(date, theme_id, stock_count, total_trade_amount, avg_change_pct)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(date, theme_id) DO UPDATE SET
                     stock_count=excluded.stock_count,
                     total_trade_amount=excluded.total_trade_amount,
                     avg_change_pct=excluded.avg_change_pct""",
                (
                    r["date"],
                    r["theme_id"],
                    r["stock_count"],
                    r["total_trade_amount"] or 0,
                    round(r["avg_change_pct"] or 0, 2),
                ),
            )
        conn.commit()

    print(f"aggregated {len(rows)} theme-day entries")
    return rows


def _get_tree_leaf_ids():
    """(legacy) theme-tree.json에서 leaf 노드 ID 집합을 반환.

    REQ-081 이후 theme-trend는 root 단위 집계를 사용하므로 본 함수는
    fallback에서만 사용된다. 유지 이유: 향후 leaf/mixed 옵션 대응.
    """
    tree_path = HOMEPAGE / "data" / "themes" / "theme-tree.json"
    if not tree_path.exists():
        return None
    try:
        tree_data = json.loads(tree_path.read_text())
        nodes = tree_data.get("nodes", [])
        if not nodes:
            return None
        # parent_id로 참조되는 ID = 비leaf
        parent_ids = {n["parent_id"] for n in nodes if n.get("parent_id")}
        leaf_ids = {n["id"] for n in nodes if n["id"] not in parent_ids}
        return leaf_ids
    except Exception:
        return None


def _get_tree_root_ids():
    """design-news-time-state-v1 (catch 3) — theme-tree.json의 1단계 root 노드 (id, name) 리스트.

    SSOT 정합: trend 차트의 root list = tree 1단계와 1:1. 거래대금 0인 root도 빈 series로 포함하여
    legend 갯수 불일치(33 vs 32) 자연 봉쇄. 6G 누락 fix.
    빌드 순서: build()는 build_tree_json() → build_json() 순. tree.json이 먼저 출력됨.
    """
    tree_path = HOMEPAGE / "data" / "themes" / "theme-tree.json"
    if not tree_path.exists():
        return []
    try:
        tree_data = json.loads(tree_path.read_text())
        nodes = tree_data.get("nodes", [])
        if not nodes:
            return []
        # parent_id가 None = 1단계 root
        roots = [(n["id"], n["name"]) for n in nodes if n.get("parent_id") is None]
        return roots
    except Exception:
        return []


def get_root_themes(conn) -> dict[int, tuple[int, str]]:
    """REQ-081 — 각 활성 테마 → 최상위 root 테마 매핑.

    SoT: theme_parents (N:M, REQ-076 인프라).
    - parents 없는 테마 = root (자기 자신 매핑)
    - parents 있는 테마 = 첫 번째 parent의 root까지 재귀 (옵션 B 단순 정책)
    - 다중 parent 시 child_id에 대해 가장 작은 parent_id 채택 (결정성)
    - cycle 방어: depth ≤ 10 (recursive CTE 단계 제한)

    옵션 비교:
      A. weight 가중 분배 — 정확하나 weight=1.0 default라 차이 없음.
      B. 첫 번째 root 단일 — 단순, 결정적. ★ 채택
      C. 모든 root 이중 카운팅 — 차트 중복.

    Returns:
        {theme_id: (root_id, root_name)} — 활성 테마만.
    """
    rows = conn.execute(
        """
        WITH RECURSIVE theme_root AS (
          -- base: (1) theme_parents에 child로 미등록 OR (2) themes.parent_id IS NULL인 활성 테마 = root
          -- 조건 (2) 추가 이유: MLCC처럼 themes.parent_id=NULL로 독립 승격되었으나
          -- theme_parents에 레거시 child 항목이 잔존하는 경우도 root로 올바르게 인식.
          SELECT t.id AS theme_id,
                 t.id AS root_id,
                 t.name AS root_name,
                 0 AS depth
          FROM themes t
          WHERE t.is_active = 1
            AND (t.id NOT IN (SELECT child_id FROM theme_parents)
                 OR t.parent_id IS NULL)
          UNION ALL
          -- recursive: 자식 → root 전파
          -- 다중 parent 시 MIN(parent_id) 단일 채택 (옵션 B, 결정성)
          SELECT tp.child_id, tr.root_id, tr.root_name, tr.depth + 1
          FROM theme_parents tp
          JOIN theme_root tr ON tp.parent_id = tr.theme_id
          WHERE tr.depth < 10
            AND tp.parent_id = (
              SELECT MIN(parent_id) FROM theme_parents
              WHERE child_id = tp.child_id
            )
        )
        SELECT tr.theme_id, tr.root_id, tr.root_name
        FROM theme_root tr
        JOIN themes t ON t.id = tr.theme_id
        WHERE t.is_active = 1
        """
    ).fetchall()
    return {r["theme_id"]: (r["root_id"], r["root_name"]) for r in rows}


def build_json():
    """theme-trend.json 정적 빌드 — 프론트 차트용.

    REQ-081 (2026-04-29): leaf → root 부모 단위 집계 변경.
    - theme_parents (N:M) SoT 활용
    - 각 leaf의 거래대금 → 해당 leaf의 root로 합산 (옵션 B 단일 매핑)
    - 같은 root 내 중복 종목은 dedupe (종목 코드 기준)
    - 출력: name=root명, level='root', aggregated_from=[leaf 이름들]
    """
    from .config import is_market_holiday

    with connect() as conn:
        # REQ-081 — 모든 활성 테마 → root 매핑
        root_map = get_root_themes(conn)  # {theme_id: (root_id, root_name)}

        # 최근 N영업일 거래대금 통계 (leaf 단위, retired 제외)
        stats = conn.execute(
            """
            SELECT tds.date, tds.theme_id, t.name as theme_name,
                   tds.stock_count, tds.total_trade_amount, tds.avg_change_pct
            FROM theme_daily_stats tds
            JOIN themes t ON t.id = tds.theme_id
            WHERE tds.date IN (
                SELECT DISTINCT date FROM theme_daily_stats
                ORDER BY date DESC LIMIT ?
            )
              AND t.is_active = 1
              AND (t.category IS NULL OR t.category IN ('theme', 'direction', 'industry', 'event'))
            ORDER BY tds.date
            """,
            (TREND_BUSINESS_DAYS,),
        ).fetchall()
        stats = [r for r in stats if not is_market_holiday(r["date"])]

    # ── leaf → root 집계 ──
    # (date, root_id) 단위로 합산. 종목은 dedup (같은 root 내 동일 종목 1회만)
    # 종목 dedup이 필요하므로, leaf의 stock_count/trade_amount 합산은 1차 집계.
    # 정확한 root별 trade_amount는 종목 단위 SUM (아래 별도 쿼리)로 재계산.

    # 1) date 별 root_id → leaf 이름 리스트 (aggregated_from 디버깅 필드)
    aggregated_from = {}  # (date, root_id) -> set(leaf_name)
    for r in stats:
        tid = r["theme_id"]
        if tid not in root_map:
            continue
        root_id, _root_name = root_map[tid]
        key = (r["date"], root_id)
        aggregated_from.setdefault(key, set()).add(r["theme_name"])

    # 2) (date, root) 단위 종목 집계 — daily_picks × stock_themes × root_map
    # SQL 한 번으로: 각 (date, root_id) → 종목별 trade_amount (dedup 후)
    # 종목 dedup: 같은 종목이 root 산하 여러 leaf에 속해도 1회만
    dates_set = sorted({r["date"] for r in stats})
    if not dates_set:
        out = {"generated_at": datetime.now().isoformat(), "dates": [], "themes": []}
        out_path = HOMEPAGE / "data" / "themes" / "theme-trend.json"
        # [JSON-GUARD] 0 dates → 기존 파일 보존 (1건 DB로 21일치 파괴 방지)
        existing = _json_dates_count(out_path)
        if existing > 0:
            print(
                f"[JSON-GUARD] SKIP write {out_path.name}: "
                f"0 dates (DB 데이터 부족) — 기존 {existing}일치 보존"
            )
            return out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        print(f"wrote {out_path}: 0 themes (no data)")
        return out_path

    with connect() as conn2:
        # 최근 N일치 daily_picks × stock_themes × leaf → root 매핑 (Python측)
        # source 'kiwoom' = 트렌드 차트 trade_amount/stock_count 누적 SoT (영웅식 무관, 분리).
        date_ph = ",".join("?" for _ in dates_set)
        rows = conn2.execute(
            f"""
            SELECT dp.date,
                   st.theme_id AS leaf_id,
                   t.name AS leaf_name,
                   s.code,
                   s.name AS stock_name,
                   dp.price,
                   dp.change_pct,
                   dp.trade_amount,
                   COALESCE(dp.open_price, dp.price / (1 + COALESCE(dp.change_pct, 0) / 100.0)) as open_price,
                   COALESCE(dp.high_price, MAX(COALESCE(dp.open_price, dp.price / (1 + COALESCE(dp.change_pct, 0) / 100.0)), dp.price)) as high_price,
                   COALESCE(dp.low_price, MIN(COALESCE(dp.open_price, dp.price / (1 + COALESCE(dp.change_pct, 0) / 100.0)), dp.price)) as low_price
            FROM daily_picks dp
            JOIN stock_themes st ON st.stock_code = dp.stock_code
            JOIN themes t ON t.id = st.theme_id
            JOIN stocks s ON s.code = dp.stock_code
            WHERE dp.date IN ({date_ph})
              AND dp.source = 'kiwoom'
              AND COALESCE(st.source, '') != 'retired_v3'
              AND t.is_active = 1
              AND (t.category IS NULL OR t.category IN ('theme', 'direction', 'industry', 'event'))
            """,
            tuple(dates_set),
        ).fetchall()

        # SC (stock-{date}.json) = daily_picks WHERE source='heroshik_strict_<m_d>'.
        # drill-down stocks list 정합 (영웅식 satisfier만 노출). 대표 catch P0
        # (2026-05-08 18:44 KST): "거래대금 추이에 하이닉스가 포함되었는데 완료된건가?"
        # 차트 trade_amount/stock_count(='kiwoom' source SoT)는 변경 없음 — drill-down stocks list만 필터.
        her_codes_by_date: dict[str, set[str]] = {}
        her_rows = conn2.execute(
            f"""SELECT date, stock_code FROM daily_picks
                WHERE date IN ({date_ph}) AND source LIKE 'heroshik_strict_%'""",
            tuple(dates_set),
        ).fetchall()
        for hr in her_rows:
            her_codes_by_date.setdefault(hr["date"], set()).add(hr["stock_code"])

    # 카드 SSOT (`latest_stocks`) ticker subset — Q-20260514-058 (5/14 결정) 정합.
    # 5/18+ 일자는 영웅식(`heroshik_strict_*`) source 부재 → 차트가 daily_picks
    # source='kiwoom' 전체 (snapshot 누적 50종 + LU union)를 합산했으나, 카드는
    # `latest_stocks` 만 (마지막 snapshot ≥500억 satisfier, 9~30종) 표출 → mismatch
    # (5/27 반도체 차트 3.79조 vs 카드 2.50조). 대표 5/28 08:25 KST verbatim
    # 옵션 A 채택: "그래프도 카드와 같은 source(latest_stocks)로 일치".
    latest_codes_by_date = _load_latest_stocks_per_date(dates_set)
    # Q-20260604-DRILLDOWN-FALLBACK — 카드 SSOT (`stock-{date}.json`) union 추가.
    # build_tree_json L609-620 동형 cascade. 진양화학(051630) 6/4 사례 = latest_stocks
    # 8건에 미포함이지만 카드 SSOT 18건에는 포함 → 정치테마 root trade=0 chip 회색
    # 비활성 mismatch (대표 catch 15:05 KST "정치테마가 비활성화되어있다") 봉쇄.
    stock_card_codes_by_date = _load_stock_card_codes_per_date(dates_set)
    _ = latest_codes_by_date  # ruff: noqa — 사용처는 root 단위 루프 (아래 L405~)
    _ = stock_card_codes_by_date  # ruff: noqa — 동일

    # (date, root_id) → {stock_code: stock_dict} (dedup)
    root_date_stocks: dict[tuple[str, int], dict[str, dict]] = {}
    root_id_to_name: dict[int, str] = {}
    for row in rows:
        leaf_id = row["leaf_id"]
        if leaf_id not in root_map:
            continue
        root_id, root_name = root_map[leaf_id]
        root_id_to_name[root_id] = root_name
        key = (row["date"], root_id)
        bucket = root_date_stocks.setdefault(key, {})
        # 같은 root 내 중복 종목 1회만 (다른 leaf에 속해도 dedup)
        if row["code"] in bucket:
            continue
        bucket[row["code"]] = {
            "name": row["stock_name"],
            "code": row["code"],
            "price": row["price"],
            "change_pct": round(row["change_pct"], 2)
            if row["change_pct"] is not None
            else 0.0,
            "trade_amount": row["trade_amount"] or 0,
            "open_price": row["open_price"],
            "high_price": row["high_price"],
            "low_price": row["low_price"],
        }

    # ── root 단위 시계열 구성 ──
    themes_by_root: dict[int, dict] = {}
    for (date, root_id), stocks_dict in root_date_stocks.items():
        if root_id not in themes_by_root:
            themes_by_root[root_id] = {
                "id": root_id,
                "name": root_id_to_name.get(root_id, f"root-{root_id}"),
                "level": "root",
                "data": [],
            }
        stocks = sorted(stocks_dict.values(), key=lambda s: -(s["trade_amount"] or 0))
        # drill-down stocks = SC (stock-{date}.json heroshik_strict satisfier)와 동일 정합
        # cascade (우선순위):
        #   1) heroshik_strict_* source 존재 → 영웅식 satisfier subset (최고 정합)
        #   2) latest_stocks + 카드 SSOT (stock-{date}.json) union → 카드 1:1 정합
        #      (Q-20260514-058 5/14 결정 + Q-20260604-DRILLDOWN-FALLBACK 6/4 정정)
        #      build_tree_json L625-632 동형 — 정치테마 chip 회색 비활성 mismatch 봉쇄.
        #   3) 모두 부재 → 전체 stocks (legacy fallback, pre-2026-05-06 일자)
        her_codes = her_codes_by_date.get(date)
        latest_codes = latest_codes_by_date.get(date)
        card_codes = stock_card_codes_by_date.get(date)
        if her_codes:
            drill_stocks = [s for s in stocks if s["code"] in her_codes]
        elif latest_codes or card_codes:
            union_codes = (latest_codes or set()) | (card_codes or set())
            drill_stocks = [s for s in stocks if s["code"] in union_codes]
        else:
            drill_stocks = stocks  # legacy fallback (heroshik/latest/카드 모두 부재)
        # 차트 trade_amount/stock_count = 영웅식 또는 latest_stocks satisfier 합산.
        # 대표 catch P0 (2026-05-08 19:05 KST): "목록에는 하이닉스가 빠지긴했지만
        # 거래대금 자체는 포함된 가격이다 차트에서" → kiwoom SoT 폐기, drill-down
        # 합산으로 통일 (영웅식). 후속 (2026-05-28): 영웅식 부재 일자도 카드 SSOT
        # (latest_stocks) 와 정합 — chart vs 카드 mismatch (5/27 반도체 3.79조 vs
        # 2.50조) 봉쇄.
        total_amount = sum(s["trade_amount"] for s in drill_stocks)
        avg_change = (
            sum(s["change_pct"] for s in drill_stocks) / len(drill_stocks)
            if drill_stocks
            else 0.0
        )
        leaf_names = sorted(aggregated_from.get((date, root_id), set()))
        entry = {
            "date": date,
            "trade_amount": total_amount,
            "stock_count": len(drill_stocks),
            "avg_change_pct": round(avg_change, 2),
            "aggregated_from": leaf_names,
        }
        if drill_stocks:
            entry["stocks"] = drill_stocks
        themes_by_root[root_id]["data"].append(entry)

    # 날짜순 정렬
    for t in themes_by_root.values():
        t["data"].sort(key=lambda d: d["date"])

    # design-news-time-state-v1 (catch 3) — theme-tree.json 1단계 root 1:1 sync.
    # 거래대금 0인 root도 빈 series (date list × stock_count=0)로 포함하여
    # legend 갯수 불일치(예: 33 vs 32) 자연 봉쇄. 6G 누락 fix.
    tree_roots = _get_tree_root_ids()  # [(id, name), ...] — 빌드 순서상 tree가 먼저
    if tree_roots:
        for root_id, root_name in tree_roots:
            if root_id in themes_by_root:
                continue  # 이미 데이터 있는 root
            # 빈 series — dates_set 모든 일자에 stock_count=0 entry
            empty_data = [
                {
                    "date": d,
                    "trade_amount": 0,
                    "stock_count": 0,
                    "avg_change_pct": 0.0,
                    "aggregated_from": [],
                }
                for d in dates_set
            ]
            themes_by_root[root_id] = {
                "id": root_id,
                "name": root_name,
                "level": "root",
                "data": empty_data,
            }

    # 모든 tree 1단계 root + 데이터 있는 root 포함. 정렬은 최근일 거래대금 기준 (0인 root는 후순위).
    trend_themes = list(themes_by_root.values())
    trend_themes.sort(
        key=lambda t: -(t["data"][-1]["trade_amount"] if t["data"] else 0)
    )

    out_path = HOMEPAGE / "data" / "themes" / "theme-trend.json"

    # [HISTORICAL-MERGE] cron DB에 historical 없을 때 기존 JSON 보존 병합.
    # cron DB는 최근 1~2일치만 보유 → theme_daily_stats → dates_set이 2일뿐
    # → _safe_write_json 가드(80% 보존율)가 매일 차단 → 21거래일 대기 부채.
    # 해결: 기존 theme-trend.json historical 엔트리를 baseline으로,
    # cron 신규 분을 UPSERT (cron 우선) → dates_set ∪ historical = 슬라이딩 윈도우.
    # 가드·DB는 건드리지 않음 — 오늘 수동 복구(55e8be3af)와 동일 원리의 자동화.
    historical = _load_theme_trend_historical(out_path, TREND_BUSINESS_DAYS)
    if historical:
        # [HISTORICAL-ONLY-ROOT] cron DB(theme_daily_stats 최근 N일) + theme-tree 기준으로
        # 생성된 trend_themes 에는 cron 보유 root 만 들어간다. 기존 JSON 에만 존재하고
        # cron 신규 분에는 없는 root(예: 최근 거래대금 0 으로 cron tree 에서 빠진 과거
        # 활성 테마)는 trend_themes 에 누락 → 아래 root_id UPSERT 루프(`for t in
        # trend_themes`)·dates_set union 양쪽에서 통째로 빠져 dates_set 이 cron 보유
        # 일수(수일)로 붕괴 → _safe_write_json 80% 가드가 매 cron 차단(=당일 6/18 영구
        # 미반영, limit-up-trend 와 비대칭). 해결: historical 에만 있는 root 를
        # trend_themes 에 추가해 기존 머지/슬라이딩/union 로직이 그대로 커버하게 한다.
        # root 메타(name/level)는 기존 JSON 원본에서 복원(_load_*는 data 만 반환).
        _existing_root_meta: dict = {}
        try:
            _ej = json.loads(out_path.read_text())
            for _t in _ej.get("themes", []):
                _tid = _t.get("id")
                if _tid is not None:
                    _existing_root_meta[_tid] = {
                        "name": _t.get("name", str(_tid)),
                        "level": _t.get("level", "root"),
                    }
        except Exception:
            _existing_root_meta = {}

        # [NAME-DEDUP] 두 서빙 DB의 theme_id divergence 해소:
        # 같은 name이지만 historical id ≠ cron id인 경우(예: "반도체" id=90 vs id=5),
        # historical 데이터를 cron 테마에 흡수하고 중복 root를 추가하지 않는다.
        # 정확 일치(normalized strip)만 병합 — 부분 문자열 매칭 금지.
        _trend_name_to_idx: dict[str, int] = {
            t["name"].strip(): i for i, t in enumerate(trend_themes)
        }
        # historical id → cron trend 인덱스 매핑 (name 기준)
        _hist_id_to_trend_idx: dict = {}
        for _hid, _hmeta in _existing_root_meta.items():
            _hname = _hmeta["name"].strip()
            if _hname in _trend_name_to_idx:
                _hist_id_to_trend_idx[_hid] = _trend_name_to_idx[_hname]
        # historical id→{date:entry}를 cron 테마 데이터에 직접 흡수 (날짜 겹치면 cron 우선)
        _name_dedup_count = 0
        for _hid, _trend_idx in _hist_id_to_trend_idx.items():
            _hist_entries = historical.get(_hid, {})
            if not _hist_entries:
                continue
            _t_target = trend_themes[_trend_idx]
            _cron_dates_target = {e["date"] for e in _t_target["data"]}
            for _hdate, _hentry in _hist_entries.items():
                if _hdate not in _cron_dates_target:
                    _is_empty = _hentry.get("stock_count", 0) == 0 and not _hentry.get(
                        "aggregated_from"
                    )
                    if not _is_empty:
                        _t_target["data"].append(_hentry)
                        _name_dedup_count += 1
        if _name_dedup_count > 0:
            print(
                f"[NAME-DEDUP] name 기준 historical 흡수 {_name_dedup_count} entries "
                f"(두 DB theme_id divergence 해소)"
            )

        _trend_ids = {t["id"] for t in trend_themes}
        # HISTORICAL-ONLY-ROOT 추가 시 name 중복 방지 (cron DB에 데이터 없는 historical id끼리 동일 name)
        # _seen_hist_names: 이미 추가된 name 추적 (정확 일치만)
        _seen_hist_names: dict[str, int] = {}  # name → trend_themes 인덱스
        for _hid in historical.keys():
            if _hid in _trend_ids:
                continue
            # name 기준 이미 흡수된 historical id는 별도 root로 추가하지 않는다
            if _hid in _hist_id_to_trend_idx:
                continue
            _meta = _existing_root_meta.get(_hid, {"name": str(_hid), "level": "root"})
            _hname_stripped = _meta["name"].strip()
            # 동일 name이 이미 추가됐으면 해당 root에 데이터 흡수 (중복 root 방지)
            if _hname_stripped in _seen_hist_names:
                _existing_idx = _seen_hist_names[_hname_stripped]
                _hist_id_to_trend_idx[_hid] = _existing_idx
                continue
            # 현재 trend_themes에 동일 name이 있으면 흡수
            _curr_name_idx = {t["name"].strip(): i for i, t in enumerate(trend_themes)}
            if _hname_stripped in _curr_name_idx:
                _hist_id_to_trend_idx[_hid] = _curr_name_idx[_hname_stripped]
                continue
            _new_idx = len(trend_themes)
            _seen_hist_names[_hname_stripped] = _new_idx
            trend_themes.append(
                {
                    "id": _hid,
                    "name": _meta["name"],
                    "level": _meta["level"],
                    "data": [],  # 아래 UPSERT 루프가 historical entry 로 채움
                }
            )

        # root_id 기준 UPSERT: cron 신규 날짜는 그대로, historical에만 있는 날짜 보충
        # _hist_id_to_trend_idx 역매핑: trend_themes 인덱스 → 흡수된 historical id 목록
        _trend_idx_to_hist_ids: dict[int, list] = {}
        for _abs_hid, _abs_tidx in _hist_id_to_trend_idx.items():
            _trend_idx_to_hist_ids.setdefault(_abs_tidx, []).append(_abs_hid)
        hist_dates_added = 0
        for _tidx, t in enumerate(trend_themes):
            # 기본: t["id"] historical + 흡수된 alias historical 병합
            hist = dict(historical.get(t["id"], {}))
            for _alias_hid in _trend_idx_to_hist_ids.get(_tidx, []):
                if _alias_hid == t["id"]:
                    continue
                for _d, _e in historical.get(_alias_hid, {}).items():
                    if _d not in hist:  # cron canonical 우선, alias는 보완
                        hist[_d] = _e
            cron_dates = {e["date"] for e in t["data"]}
            for hist_date, hist_entry in hist.items():
                if hist_date not in cron_dates:
                    # 순수 빈 entry 제외: stock_count==0 AND aggregated_from 비어있음
                    # 55e8be3af 수동 병합이 강제 추가한 빈 series 73건 보존 차단.
                    # stock_count>0 또는 aggregated_from 비어있지 않은 실데이터는 보존.
                    is_empty = hist_entry.get(
                        "stock_count", 0
                    ) == 0 and not hist_entry.get("aggregated_from")
                    if is_empty:
                        continue
                    t["data"].append(hist_entry)
                    hist_dates_added += 1
            t["data"].sort(key=lambda e: e["date"])
            # TREND_BUSINESS_DAYS 슬라이딩: 초과 시 오래된 엔트리 제거
            if len(t["data"]) > TREND_BUSINESS_DAYS:
                t["data"] = t["data"][-TREND_BUSINESS_DAYS:]
        # 빈 series root도 historical dates로 채우기 (tree_roots 패턴 동형)
        all_historical_dates: set[str] = set()
        for date_map in historical.values():
            all_historical_dates.update(date_map.keys())
        for t in trend_themes:
            if t["data"]:
                continue  # 이미 데이터 있는 root
            # 완전히 빈 root: historical dates로 빈 entry 생성
            for hist_date in sorted(all_historical_dates):
                t["data"].append(
                    {
                        "date": hist_date,
                        "trade_amount": 0,
                        "stock_count": 0,
                        "avg_change_pct": 0.0,
                        "aggregated_from": [],
                    }
                )
            if len(t["data"]) > TREND_BUSINESS_DAYS:
                t["data"] = t["data"][-TREND_BUSINESS_DAYS:]
        # dates_set 재계산: 병합 후 모든 root data 날짜 union → 최근 N일
        merged_dates_all: set[str] = set()
        for t in trend_themes:
            merged_dates_all.update(e["date"] for e in t["data"])
        dates_set = sorted(merged_dates_all, reverse=True)[:TREND_BUSINESS_DAYS]
        dates_set = sorted(dates_set)
        if hist_dates_added > 0:
            print(
                f"[HISTORICAL-MERGE] historical {hist_dates_added} entries 보충 "
                f"→ dates_set {len(dates_set)}일 (cron DB historical 부재 보완)"
            )

    out = {
        "generated_at": datetime.now().isoformat(),
        "dates": dates_set,
        "themes": trend_themes,
        "aggregation": "root",  # REQ-081 명시
    }

    written = _safe_write_json(
        out_path, out, new_dates_key="dates", target_window=TREND_BUSINESS_DAYS
    )
    if written:
        print(
            f"wrote {out_path}: {len(trend_themes)} root themes, {len(dates_set)} dates "
            f"(REQ-081 root aggregation)"
        )
    return out_path


def build_tree_json():
    """theme-tree.json — 마인드맵 트리 차트용 정적 빌드.

    오늘 pick된 종목에 할당된 테마 + 그 조상 테마만 포함.
    daily_picks에 없는 종목의 테마는 제외 (FLR-AGT-002 가짜 데이터 방지).
    """
    with connect() as conn:
        # 최근 1영업일 산출 — theme_daily_stats 기준
        latest_date_row = conn.execute(
            "SELECT MAX(date) as d FROM theme_daily_stats"
        ).fetchone()
        latest_date = latest_date_row["d"] if latest_date_row else None
        if not latest_date:
            print("build_tree_json: no data in theme_daily_stats — skipped")
            return None

        # B3 fix: daily_picks에 해당 날짜 데이터가 있는지 확인.
        # theme_daily_stats와 daily_picks 날짜가 불일치하면 종목 조회가 빈 결과를 반환.
        dp_check = conn.execute(
            "SELECT COUNT(*) as cnt FROM daily_picks WHERE date = ?",
            (latest_date,),
        ).fetchone()
        if dp_check["cnt"] == 0:
            # daily_picks의 최신 날짜로 폴백
            dp_latest = conn.execute(
                "SELECT MAX(date) as d FROM daily_picks"
            ).fetchone()
            picks_date = dp_latest["d"] if dp_latest else latest_date
            print(
                f"build_tree_json: daily_picks has no data for {latest_date}, "
                f"using {picks_date} for stock lookup"
            )
        else:
            picks_date = latest_date

        # Step 1: 전체 기간에서 한 번이라도 등장한 테마 (dateOverride 지원)
        # 최신 날짜 데이터를 기본값으로, 과거에만 등장한 테마도 노드에 포함
        active_nodes = conn.execute(
            """
            SELECT t.id, t.name, t.parent_id, t.category,
                   COALESCE(tds_today.stock_count, 0)          AS stock_count,
                   COALESCE(tds_today.total_trade_amount, 0)   AS trade_amount,
                   COALESCE(tds_today.avg_change_pct, 0.0)     AS avg_change_pct
            FROM themes t
            INNER JOIN (
                SELECT DISTINCT theme_id FROM theme_daily_stats
                WHERE total_trade_amount > 0
            ) tds_any ON tds_any.theme_id = t.id
            LEFT JOIN theme_daily_stats tds_today
              ON tds_today.theme_id = t.id AND tds_today.date = ?
            WHERE t.is_active = 1
              AND (t.category IS NULL
               OR t.category IN ('theme', 'direction', 'industry', 'event'))
            ORDER BY trade_amount DESC
            """,
            (latest_date,),
        ).fetchall()

        # Step 2: 조상 테마 수집 — 트리 연결을 위해 필요
        active_ids = {r["id"] for r in active_nodes}
        all_themes = {
            r["id"]: r
            for r in conn.execute(
                """SELECT id, name, parent_id, category FROM themes
                   WHERE is_active = 1
                     AND (category IS NULL
                      OR category IN ('theme', 'direction', 'industry', 'event'))"""
            ).fetchall()
        }

        # 부모 체인을 따라가며 조상 ID 수집
        ancestor_ids = set()
        for nid in active_ids:
            theme = all_themes.get(nid)
            pid = theme["parent_id"] if theme else None
            while pid and pid not in active_ids and pid not in ancestor_ids:
                if pid in all_themes:
                    ancestor_ids.add(pid)
                    pid = all_themes[pid]["parent_id"]
                else:
                    break

        # Step 3: 테마별 종목 목록 (daily_picks JOIN stock_themes)
        # B3 fix: picks_date 사용 (daily_picks 날짜 불일치 대응)
        # source cascade (chart build_json 와 동형 — 카드 SSOT 정합):
        #   1) heroshik_strict_* (picks_date) 존재 → 영웅식 satisfier subset
        #   2) latest_stocks (kiwoom/{picks_date}.json) 존재 → 카드 SSOT subset
        #      (Q-20260514-058 5/14 결정, 대표 5/28 08:42 옵션 A — chart 통합)
        #   3) stock-{picks_date}.json:stocks[].code subset → 카드 SSOT fallback
        #      (Q-20260604-DRILLDOWN-FALLBACK 2026-06-04, 대표 catch 진양그룹
        #      오세훈 drill-down 빈 list — 5/28 옵션 A 결정 부수효과 정정):
        #      카드(stock-{date}.json)에는 진양화학 포함되나 latest_stocks 8건에는
        #      미포함 → L2 filter 매칭 0건 → drill-down stocks=[] 자연 cascade →
        #      카드 chip "오세훈" 노출 vs drill-down 빈 list 사용자 충돌. 카드
        #      SSOT 정합 의도 유지하되 cascade source 확대 (build_daily 의
        #      `latest_stocks || daily_top || stocks` fallback 경로 정합).
        #   4) 모두 부재 → 전체 daily_picks source='kiwoom' (legacy fallback)
        # 이 cascade 적용 후 descendant_stock_count 가 카드 갯수와 정합
        # (5/27 반도체 17→6 감소 예측, sub-agent a9551b5 권고 정합).
        her_codes_today = {
            r["stock_code"]
            for r in conn.execute(
                """SELECT stock_code FROM daily_picks
                   WHERE date = ? AND source LIKE 'heroshik_strict_%'""",
                (picks_date,),
            ).fetchall()
        }
        latest_codes_today = _load_latest_stocks_per_date([picks_date]).get(
            picks_date, set()
        )
        # Q-20260604-DRILLDOWN-FALLBACK — stock-{date}.json 카드 SSOT fallback
        # build_daily.py 의 cascade (latest_stocks || daily_top || stocks) 의
        # 최종 카드 표출 종목 set 과 정합. 진양화학 같이 latest_stocks 미포함
        # 이지만 카드에는 노출되는 종목 drill-down 회복.
        stock_card_codes_today: set[str] = set()
        stock_card_path = HOMEPAGE / "data" / "interpreted" / f"stock-{picks_date}.json"
        if stock_card_path.exists():
            try:
                _card_data = json.loads(stock_card_path.read_text())
                stock_card_codes_today = {
                    s.get("code")
                    for s in _card_data.get("stocks", [])
                    if isinstance(s, dict) and s.get("code")
                }
            except (OSError, json.JSONDecodeError):
                pass
        if her_codes_today:
            tree_code_filter_sql = ""
            tree_code_filter_params: tuple = ()
            tree_source_filter = "AND dp.source LIKE 'heroshik_strict_%'"
        elif latest_codes_today or stock_card_codes_today:
            # L2+L3 union — latest_stocks (strict 8종) ∪ 카드 SSOT (18종)
            # 카드와 1:1 정합 + drill-down 빈 list 사고 봉쇄
            union_codes = latest_codes_today | stock_card_codes_today
            ph = ",".join("?" for _ in union_codes)
            tree_code_filter_sql = f" AND dp.stock_code IN ({ph})"
            tree_code_filter_params = tuple(union_codes)
            tree_source_filter = "AND dp.source = 'kiwoom'"
        else:
            tree_code_filter_sql = ""
            tree_code_filter_params = ()
            tree_source_filter = "AND dp.source = 'kiwoom'"
        theme_stocks_rows = conn.execute(
            f"""
            SELECT st.theme_id,
                   dp.stock_code AS code,
                   s.name,
                   dp.change_pct,
                   dp.trade_amount
            FROM daily_picks dp
            JOIN stock_themes st ON st.stock_code = dp.stock_code
            JOIN stocks s ON s.code = dp.stock_code
            WHERE dp.date = ? {tree_source_filter}{tree_code_filter_sql}
              AND COALESCE(st.source, '') != 'retired_v3'
            ORDER BY st.theme_id, dp.trade_amount DESC
            """,
            (picks_date, *tree_code_filter_params),
        ).fetchall()

    # 테마별 종목 dict 구성
    theme_stocks = {}  # theme_id -> [{"code", "name", "change_pct", "trade_amount"}, ...]
    for r in theme_stocks_rows:
        tid = r["theme_id"]
        if tid not in theme_stocks:
            theme_stocks[tid] = []
        theme_stocks[tid].append(
            {
                "code": r["code"],
                "name": r["name"],
                "change_pct": round(r["change_pct"], 2)
                if r["change_pct"] is not None
                else 0.0,
                "trade_amount": r["trade_amount"] or 0,
            }
        )

    MAX_STOCKS_PER_NODE = 5

    # 노드 리스트 구성: 활성 노드 + 조상 노드 (거래대금 0)
    node_list = []
    for r in active_nodes:
        tid = r["id"]
        stocks_all = theme_stocks.get(tid, [])
        node_list.append(
            {
                "id": tid,
                "name": r["name"],
                "parent_id": r["parent_id"],
                "trade_amount": r["trade_amount"],
                "stock_count": r["stock_count"],
                "avg_change_pct": round(r["avg_change_pct"], 2),
                "stocks": stocks_all[:MAX_STOCKS_PER_NODE],
                "total_stock_count": len(stocks_all),
            }
        )
    for aid in ancestor_ids:
        t = all_themes[aid]
        stocks_all = theme_stocks.get(aid, [])
        node_list.append(
            {
                "id": t["id"],
                "name": t["name"],
                "parent_id": t["parent_id"],
                "trade_amount": 0,
                "stock_count": 0,
                "avg_change_pct": 0.0,
                "stocks": stocks_all[:MAX_STOCKS_PER_NODE],
                "total_stock_count": len(stocks_all),
            }
        )

    # ── 부모/자식 종목 중복 제거 + descendant_stock_count 계산 ──
    # 종목은 가장 깊은(가장 구체적인) 테마에만 표시.
    # 부모 노드의 stocks에서 자식에 이미 존재하는 종목을 제거한다.
    node_by_id = {n["id"]: n for n in node_list}
    children_map = {}  # parent_id -> [child_id, ...]
    for n in node_list:
        pid = n.get("parent_id")
        if pid is not None:
            children_map.setdefault(pid, []).append(n["id"])

    def _collect_descendant_codes(nid):
        """nid의 모든 자손(자신 제외)의 종목 코드 set 반환."""
        codes = set()
        for cid in children_map.get(nid, []):
            child_all = theme_stocks.get(cid, [])
            codes.update(s["code"] for s in child_all)
            codes |= _collect_descendant_codes(cid)
        return codes

    def _collect_all_codes(nid):
        """nid 노드 + 모든 자손의 종목 코드 set (중복 제거)."""
        node = node_by_id.get(nid)
        codes = set()
        if node:
            all_stocks = theme_stocks.get(nid, [])
            codes.update(s["code"] for s in all_stocks)
        for cid in children_map.get(nid, []):
            codes |= _collect_all_codes(cid)
        return codes

    # leaf → root 순으로 처리하기 위해 위상 정렬 (leaf first)
    # 각 부모에서 자식에 이미 속한 종목을 제거
    for n in node_list:
        nid = n["id"]
        child_codes = _collect_descendant_codes(nid)
        if child_codes:
            # theme_stocks에서 자식 종목 제거
            original = theme_stocks.get(nid, [])
            deduped = [s for s in original if s["code"] not in child_codes]
            theme_stocks[nid] = deduped
            # 노드의 stocks/stock_count/trade_amount도 갱신
            n["stocks"] = deduped[:MAX_STOCKS_PER_NODE]
            n["total_stock_count"] = len(deduped)
            n["stock_count"] = len(deduped)
            # 부모 고유 종목의 거래대금만 합산
            n["trade_amount"] = sum(s["trade_amount"] for s in deduped)

    for n in node_list:
        desc_codes = _collect_all_codes(n["id"])
        n["descendant_stock_count"] = len(desc_codes)

    # ── 고유 종목 기반 거래대금 합산 (형제 테마 간 중복 제거) ──
    # 형제 테마가 같은 종목을 공유할 때, 부모 노드의 합산에서 중복 카운트 방지
    def _collect_unique_amounts(nid):
        """nid + 모든 자손의 종목별 거래대금 dict (종목 코드 기준 중복 제거)."""
        amounts = {}
        for s in theme_stocks.get(nid, []):
            if s["code"] not in amounts:
                amounts[s["code"]] = s["trade_amount"]
        for cid in children_map.get(nid, []):
            for code, amt in _collect_unique_amounts(cid).items():
                if code not in amounts:
                    amounts[code] = amt
        return amounts

    for n in node_list:
        unique_amounts = _collect_unique_amounts(n["id"])
        n["unique_trade_amount"] = sum(unique_amounts.values())

    # date 필드 정합 (backend SSOT — frontend misleading 봉쇄):
    #   - date: tree 빌드 기준 일자 (theme_daily_stats 최신, 발효일)
    #   - source_date: nodes의 stocks(change_pct/trade_amount) 실 source 시점
    #     = daily_picks(picks_date) 행의 date 값. picks_date는 latest_date와 다를 수 있음
    #     (theme_daily_stats vs daily_picks 불일치 시 fallback 경로). frontend renderer는
    #     date != source_date 또는 PRE_MARKET 시 source_date를 표시하여 오해 차단.
    out = {
        "date": latest_date,
        "source_date": picks_date,
        "nodes": node_list,
    }
    out_path = HOMEPAGE / "data" / "themes" / "theme-tree.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(
        f"wrote {out_path}: {len(node_list)} nodes, "
        f"date={latest_date}, source_date={picks_date}"
    )
    return out_path


def build_map_json():
    """theme-map.json — 테마별 종목 매핑 (칩 UI 용).

    stock-{date}.json의 themes 필드를 기반으로 빌드.
    정규화는 build_daily에서 이미 적용되므로 여기선 집계만.
    """
    from .config import pipeline_date

    today = pipeline_date()
    stock_file = HOMEPAGE / "data" / "interpreted" / f"stock-{today}.json"
    if not stock_file.exists():
        print(f"build_map_json: {stock_file} not found — skipped")
        return None

    stock_data = json.loads(stock_file.read_text())
    stocks = stock_data.get("stocks", [])

    # 테마별 종목 집계
    theme_stocks = {}  # theme_name -> [stock_info, ...]
    with connect() as conn:
        for s in stocks:
            for t in s.get("themes", []):
                if t not in theme_stocks:
                    # DB에서 테마 ID 조회
                    row = conn.execute(
                        "SELECT id FROM themes WHERE name=?", (t,)
                    ).fetchone()
                    theme_stocks[t] = {
                        "id": row["id"] if row else hash(t) % 10000,
                        "name": t,
                        "stocks": [],
                    }
                theme_stocks[t]["stocks"].append(
                    {
                        "code": s["code"],
                        "name": s["name"],
                        "industry": s.get("industry", ""),
                    }
                )

    # stock_count 추가 + 정렬
    themes_list = list(theme_stocks.values())
    for t in themes_list:
        t["stock_count"] = len(t["stocks"])
        t["parent_id"] = None  # 플랫 리스트 (칩 UI는 트리 불필요)
    themes_list.sort(key=lambda x: -x["stock_count"])

    out = {
        "generated_at": datetime.now().isoformat(),
        "themes": themes_list,
    }
    out_path = HOMEPAGE / "data" / "themes" / "theme-map.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {out_path}: {len(themes_list)} themes")
    return out_path


def build_limit_up_trend_json():
    """REQ-pm320-ux-cycle #2 — 일자별 상한가 종목 추이 JSON.

    상한가 SSOT = 가격기반 (종가 등락률 >= 29.79%) — 대표 3회 확정 (D축, 2026-06-16).
      - 1순위 후보 풀: dailybars (close - prev_close)/prev_close >= 29.79% (chain SQL).
        dailybars 가 healthy 하면 종가 기준 정확 판정 (권리락 adj_ratio 보정 포함).
      - 2순위 (dailybars stale/누락 보강): daily_picks.change_pct >= 29.79% union.
        cron 사고 등으로 dailybars 히스토리가 막혀도 당일 change_pct 로 상한가 판정.
        상한가 flag 는 히스토리 불요 — 당일 등락률만으로 판정 (D축 데이터 확보 근거).
      - change_pct: 종가 기준 (dailybars chain) / daily_picks.change_pct (2순위).
      - consecutive_count: 직전 거래일(dailybars) close 기준 +29.79% chain.

    종전 ka10017 (stock_status_badges) SoT 폐기 사유 (D축, 2026-06-16):
      키움 조건검색·ka10017 은 씨에스베어링(297090, +29.79%) 같은 진짜 상한가를
      거래대금/조건식 미달로 누락 → 가격(>=29.79%)이 유일 완전 판정.
      종전 audit 3중 결함도 유지 차단:
        A. ka10017 = 장중 +30% 터치 시 누적 적재 (풀린 종목 잔존)
        B. daily_picks.change_pct = 고가 기준일 수 있음 → 2순위는 close 정합 가드
        C. consecutive_count chain 이 high 기준일 수 있음 → +1 오버카운트

    daily_picks 는 (a) 가격기반 상한가 판정 source(2순위) + (b) metadata 보강
    (trade_amount/OHLC/이름) 둘 다. stock_status_badges 는 trade_amount 3순위
    fallback 으로만 잔존 (상한가 판정 SoT 아님).
    """
    from .config import is_market_holiday

    LIMIT_UP_BUSINESS_DAYS = 20
    # 상한가 임계 = 종가 등락률 >= 29.79% (D축, 대표 3회 확정 2026-06-16).
    # build_daily._calc_limit_up_streak 와 동일 값 (소스 정합 의무).
    LIMIT_UP_THRESHOLD = 29.79
    with connect() as conn:
        # 1순위 후보 풀 — dailybars close 기준 chain 산출 (healthy 환경).
        # 결과: code, date, close, prev_close, chg_pct_close.
        # 거래일이 비연속이라도 LAG는 직전 행을 사용 → dailybars의 거래일 시퀀스가 SSOT.
        # Fix B Phase 3 (DOC-20260514-REQ-002 §3.3 + DOC-20260515-DSN-001 §1.4):
        #   권리락(액면분할/무상증자/주식병합) 직후 chg_pct 가짜 산출 차단.
        #   adj_ratio = prev_date(exclusive) ~ date(inclusive) 사이 발생한 권리락 ratio.
        #   adjusted_prev_close = raw_prev_close × adj_ratio (단일 권리락 가정, LIMIT 1).
        #   adj_ratio DEFAULT 1.0 (권리락 미발생 종목은 효과 무변).
        #   30.5% cap = 한국 일일 상하한가 ±30% + 0.5% buffer (stale collect 결함 패턴 추가 차단).
        # D축(2026-06-16): dailybars 테이블이 없는 환경(예: cron 서빙 DB 미보유)에서는
        #   chain SQL 이 OperationalError(no such table) → graceful skip 후 daily_picks
        #   2순위 union(아래)이 SSOT 가 된다. 카드(build_daily v1 판정 = daily_picks
        #   change_pct >= 29.79)와 동일 source 라 게이트 정합. dailybars 가 healthy 하면
        #   chain 1순위 + daily_picks union 합집합.
        #
        # 상한가 과소수집 보강 (2026-06-18, ROOT = 서빙 dailybars universe ~40종 협소):
        #   백필 DB(전종목 누적, 통상 메인 homepage worktree)를 read-only ATTACH 해
        #   chain 의 base 를 (서빙 ∪ 백필, (code,date) 중복 서빙 우선) 로 확장한다.
        #   서빙에 없는 종목/날짜의 dailybars 가 백필에 있으면 chain 이 잡아 누락 0.
        #   ATTACH 실패/부재 시 base 는 서빙 단독(_attach_backfill_dailybars=False) →
        #   기존 동작 유지(회귀 0). dailybars_adjustments 도 동일 union (권리락 보정 보존).
        backfill_attached = _attach_backfill_dailybars(conn)
        if backfill_attached:
            # 서빙 우선 union: 동일 (code,date) 는 서빙 행 채택(백필은 보강).
            _bars_src = (
                "(SELECT code, date, close FROM dailybars "
                " UNION "  # UNION = 중복 제거; 서빙·백필 동일 행이면 1행
                " SELECT b.code, b.date, b.close FROM backfill.dailybars b "
                " WHERE NOT EXISTS (SELECT 1 FROM dailybars s "
                "   WHERE s.code=b.code AND s.date=b.date))"
            )
            _adj_src = (
                "(SELECT code, date, ratio FROM dailybars_adjustments "
                " UNION "
                " SELECT b.code, b.date, b.ratio FROM backfill.dailybars_adjustments b "
                " WHERE NOT EXISTS (SELECT 1 FROM dailybars_adjustments s "
                "   WHERE s.code=b.code AND s.date=b.date))"
            )
        else:
            _bars_src = "dailybars"
            _adj_src = "dailybars_adjustments"
        try:
            chain_rows = conn.execute(
                f"""WITH chain AS (
                     SELECT code, date, close,
                            LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev_close,
                            LAG(date)  OVER (PARTITION BY code ORDER BY date) AS prev_date
                     FROM {_bars_src}
                     WHERE close IS NOT NULL AND close > 0
                   ),
                   chain_adj AS (
                     SELECT code, date, close, prev_close, prev_date,
                            COALESCE((
                              SELECT ratio FROM {_adj_src} adj
                              WHERE adj.code = chain.code
                                AND adj.date > chain.prev_date
                                AND adj.date <= chain.date
                              ORDER BY adj.date DESC LIMIT 1
                            ), 1.0) AS adj_ratio
                     FROM chain
                     WHERE prev_close IS NOT NULL AND prev_close > 0
                   )
                   SELECT code, date, close,
                          (prev_close * adj_ratio) AS prev_close,
                          ROUND((close - prev_close * adj_ratio) * 100.0
                                / (prev_close * adj_ratio), 2) AS chg_pct_close
                   FROM chain_adj
                   WHERE (prev_close * adj_ratio) > 0
                     AND (close - prev_close * adj_ratio) * 100.0
                         / (prev_close * adj_ratio) >= ?
                     AND (close - prev_close * adj_ratio) * 100.0
                         / (prev_close * adj_ratio) <= 30.5
                   ORDER BY date ASC, code""",
                (LIMIT_UP_THRESHOLD,),
            ).fetchall()
        except sqlite3.OperationalError as _e_chain:
            # dailybars(또는 dailybars_adjustments) 테이블 부재 환경 → daily_picks union 단독.
            print(
                f"[limit-up-trend] dailybars chain skip ({_e_chain}) — "
                "daily_picks.change_pct SSOT 단독 사용 (D축)"
            )
            chain_rows = []
        finally:
            if backfill_attached:
                try:
                    conn.execute("DETACH DATABASE backfill")
                except sqlite3.OperationalError:
                    pass

        # 일자별 limit-up 코드 set + 일자별 chg_pct map
        from collections import OrderedDict

        date_codes = OrderedDict()  # date -> [(code, chg_pct_close), ...]
        all_dates_set = set()
        for r in chain_rows:
            d = r["date"]
            if is_market_holiday(d):
                continue
            all_dates_set.add(d)
            date_codes.setdefault(d, []).append(
                (r["code"], r["chg_pct_close"], r["close"], r["prev_close"])
            )

        # ─── 카드 ↔ 추이 SSOT 정합 — daily_picks 2순위 union (D축, 2026-06-16) ─────
        # 카드 상한가 = build_daily 가 v1 조건검색 등락률(>= 29.79%)로 판정한 집합.
        # 그 판정 source 인 daily_picks.change_pct (build_daily UPSERT) 가 곧 카드의
        # SoT 다. 위 chain SQL(dailybars close 기준)은 (a) dailybars 가 healthy 한
        # 환경에서 정확하지만 (b) cron 서빙 DB 처럼 dailybars 테이블이 없거나 거래정지-
        # 해제 후 LAG(close)가 정지 전 종가를 prev 로 잡아 adj_chg 가 30.5 cap 초과로
        # 누락되는 케이스가 있다. ⇒ daily_picks.change_pct >= 29.79 union 으로 카드
        # 집합과 1:1 정합 (D축 게이트 limit_up_card_trend_ssot 정합).
        #
        # 종전 ka10017 stock_status_badges rescue union 폐기 (D축):
        #   collect_kiwoom_limit_up(ka10017) 가 폐기되어 badge SoT 가 비므로 ka10017
        #   기반 rescue 는 cutover 후 死 코드. daily_picks(= v1 판정 source)가 대체.
        #   027040 류(정지-해제 상한가)·297090 류(거래대금 하위 상한가) 모두
        #   daily_picks.change_pct 로 catch (build_daily 가 v1 목록 전체를 UPSERT).
        #
        # chg_pct = daily_picks.change_pct (SoT, 카드와 동일 값).
        # close/prev_close 는 OHLC 보강 단계(아래 daily_picks/dailybars 우선)에서 채움 —
        # union 시점엔 close=None placeholder, prev_close 역산. 실제 mini-candle 값은
        # 후속 보강이 dailybars/daily_picks 우선으로 덮어쓴다.
        rescued_picks = conn.execute(
            """SELECT date, stock_code, change_pct, price
                 FROM daily_picks
                WHERE change_pct IS NOT NULL
                  AND change_pct >= ?""",
            (LIMIT_UP_THRESHOLD,),
        ).fetchall()
        for rp in rescued_picks:
            d = rp["date"]
            code = rp["stock_code"]
            chg = rp["change_pct"]
            if not d or not code or chg is None or is_market_holiday(d):
                continue
            # 이미 chain SQL 에서 잡힌 종목이면 중복 추가 금지 (정상 케이스)
            if any(c == code for c, *_ in date_codes.get(d, [])):
                continue
            # change_pct 가 정상 상한가 상한 밖(데이터 오염)이면 union 제외 (회귀 안전).
            # 한국 일일 상한 +30% + buffer. 하한(>= 29.79)은 WHERE 절에서 이미 보장.
            if chg > 31.5:
                continue
            close = rp["price"] if rp["price"] and rp["price"] > 0 else None
            prev_close = round(close / (1.0 + chg / 100.0)) if close else None
            all_dates_set.add(d)
            date_codes.setdefault(d, []).append((code, chg, close, prev_close))

        # ─── /daily_picks 2순위 union ───

        # Q-20260512-LIMIT-UP-TREND-5-12-MISSING (옵션 A):
        # qualifying 0건 영업일도 데이터 존재 시 빈 entry 추가 (장중 시각 일관).
        # theme-trend.json과 동일 패턴 — 사용자에게 "오늘 데이터 누락" 오해 차단.
        # D축(2026-06-16): dailybars 우선, 부재 환경(cron 서빙 DB)은 daily_picks.date 로
        #   영업일 목록 산출 (graceful — chain skip 과 동일 토폴로지).
        try:
            biz_dates_rows = conn.execute(
                "SELECT DISTINCT date FROM dailybars WHERE date IS NOT NULL "
                "ORDER BY date DESC LIMIT ?",
                (LIMIT_UP_BUSINESS_DAYS * 2,),  # holiday 여유분
            ).fetchall()
        except sqlite3.OperationalError:
            biz_dates_rows = conn.execute(
                "SELECT DISTINCT date FROM daily_picks WHERE date IS NOT NULL "
                "ORDER BY date DESC LIMIT ?",
                (LIMIT_UP_BUSINESS_DAYS * 2,),
            ).fetchall()
        for br in biz_dates_rows:
            d = br["date"]
            if d and not is_market_holiday(d):
                all_dates_set.add(d)

        # 최근 LIMIT_UP_BUSINESS_DAYS 영업일만
        target_dates = sorted(all_dates_set, reverse=True)[:LIMIT_UP_BUSINESS_DAYS]
        target_dates_asc = sorted(target_dates)

        # 종목명 조회 (stocks 테이블 또는 daily_picks)
        all_codes = set()
        for d in target_dates_asc:
            for code, _chg, _c, _pc in date_codes.get(d, []):
                all_codes.add(code)
        # 백필 보강 dict (종목명 + 거래대금) — 서빙 universe 밖 백필 union 종목용.
        # read-only, graceful(빈 dict 시 서빙 단독 = 회귀 0). 서빙 우선은 호출부 가드.
        name_supp, ta_supp = _load_backfill_supplements(
            conn, all_codes, target_dates_asc
        )
        name_map = {}
        if all_codes:
            placeholders = ",".join("?" for _ in all_codes)
            try:
                nrows = conn.execute(
                    f"SELECT code, name FROM stocks WHERE code IN ({placeholders})",
                    list(all_codes),
                ).fetchall()
                for nr in nrows:
                    name_map[nr["code"]] = nr["name"]
            except Exception:
                pass
            missing = [c for c in all_codes if c not in name_map]
            if missing:
                placeholders2 = ",".join("?" for _ in missing)
                try:
                    nrows2 = conn.execute(
                        f"SELECT DISTINCT stock_code, name FROM daily_picks WHERE stock_code IN ({placeholders2}) AND name IS NOT NULL",
                        missing,
                    ).fetchall()
                    for nr in nrows2:
                        name_map.setdefault(nr["stock_code"], nr["name"])
                except Exception:
                    pass
            # ROOT (2026-06-18): 백필 union 으로 들어온 종목(서빙 universe 밖)은
            # 서빙 stocks/daily_picks 에 행이 없어 name=코드(fallback) → 프론트가
            # 종목코드 그대로 노출. 백필 DB(전종목 누적)의 stocks 종목명으로 보강.
            # 서빙 우선 불변(setdefault) — 서빙에서 이미 해석된 종목명은 보존.
            for _code, _name in name_supp.items():
                name_map.setdefault(_code, _name)
            # FLR-20260507 (dev-stocks-master-backfill): stocks master 부재 +
            # daily_picks 부재 시 코드값 fallback 표시는 사용자 혼란 (5/4 006345 사고).
            # 잔존 missing 종목은 console warn으로 명시 → 다음 backfill 사이클 trigger.
            still_missing = [c for c in all_codes if c not in name_map]
            if still_missing:
                print(
                    f"WARN: limit-up-trend stocks master 부재 (code fallback 표시): "
                    f"{still_missing}. 키움 ka10001 또는 KIND seed_master.py로 backfill 필요."
                )

        # consecutive_count: dailybars close chain — 직전 거래일 close 기준 +29.79%
        # build_daily.py _calc_limit_up_streak 동일 임계(LIMIT_UP_THRESHOLD), dailybars 기반.
        # D축(2026-06-16): dailybars 가 healthy 한 환경은 dailybars close chain 우선.
        # dailybars 가 비거나(예: cron 서빙 DB 미보유) prev 행이 없으면 카드와 동일
        # source 인 daily_picks.change_pct chain 으로 fallback → 카드 consecutive_count
        # (build_daily._calc_limit_up_streak = daily_picks chain)와 정합.
        def _streak_from_daily_picks(code, d):
            cc = 0
            prev_rows = conn.execute(
                """SELECT date, change_pct FROM daily_picks
                   WHERE stock_code=? AND date<? ORDER BY date DESC LIMIT 30""",
                (code, d),
            ).fetchall()
            for r in prev_rows:
                pc = r["change_pct"]
                if pc is None or pc < LIMIT_UP_THRESHOLD:
                    break
                cc += 1
            return cc

        def _streak_from_dailybars(code, d):
            cc = 0
            try:
                prev_rows = conn.execute(
                    """SELECT date, close FROM dailybars
                       WHERE code=? AND date<? ORDER BY date DESC LIMIT 30""",
                    (code, d),
                ).fetchall()
            except sqlite3.OperationalError:
                # dailybars 테이블 부재 환경 → daily_picks chain fallback (카드 source 정합).
                return _streak_from_daily_picks(code, d)
            if len(prev_rows) < 2:
                # dailybars 부족 → daily_picks chain fallback (카드 source 정합).
                return _streak_from_daily_picks(code, d)
            # close chain: 두 인접 행의 (cur - prev)/prev >= 29.79%
            for i in range(len(prev_rows) - 1):
                cur = prev_rows[i]["close"]
                prev = prev_rows[i + 1]["close"]
                if not prev or prev <= 0:
                    break
                pct = (cur - prev) * 100.0 / prev
                if pct >= LIMIT_UP_THRESHOLD:
                    cc += 1
                else:
                    break
            return cc

        items = []
        total_count = 0
        for d in target_dates_asc:
            entries = sorted(date_codes.get(d, []), key=lambda x: x[0])
            stocks_out = []
            for code, chg_pct_close, close, _prev_close in entries:
                # daily_picks에서 trade_amount + OHLC 보강 (있으면 사용, 없으면 None)
                pick = conn.execute(
                    "SELECT trade_amount, price, open_price, high_price, low_price FROM daily_picks WHERE stock_code=? AND date=?",
                    (code, d),
                ).fetchone()
                ta = pick["trade_amount"] if pick else None
                price = pick["price"] if pick else close
                open_price = pick["open_price"] if pick else None
                high_price = pick["high_price"] if pick else None
                low_price = pick["low_price"] if pick else None

                # FLR-20260507-TEC (dev-trade-amount-null-fix): trade_amount NULL 105/212 (49.5%) 결함.
                # daily_picks JOIN만으로는 ka10017 상한가 종목 중 daily_picks 미적재 종목 누락.
                # → dailybars.trade_amount fallback 추가 (SSOT는 dailybars).
                #
                # Q-20260512-LIMIT-UP-OPEN-PRICE-NULL (대표 5/12 23:24 catch):
                # daily_picks에 ta는 있지만 open_price/high_price/low_price만 NULL인 경우
                # (5/12 12종 중 8종 = 66.7%) → fallback 미트리거 → mini-candle 일률 모양 결함.
                #
                # Q-20260512-LIMIT-UP-HIGH-LOW-FALLBACK (대표 5/12 23:48 catch):
                # daily_picks의 high_price/low_price가 close와 동일하게 적재된 결함 (ka10017 수집기
                # 잘못된 채움 → 잘못된 값이라 is None 조건만으로는 fallback 미트리거).
                # 5/12 12종 중 9종 = 75%에서 h=l=c → mini-candle 일률 모양.
                # SSOT는 dailybars. dailybars row 존재 시 OHLC는 dailybars 우선 채택.
                # D축(2026-06-16): dailybars 부재 환경(cron 서빙 DB)은 db_row=None →
                #   daily_picks 값(price/OHLC) 유지 (graceful).
                try:
                    db_row = conn.execute(
                        "SELECT trade_amount, open, high, low, close FROM dailybars "
                        "WHERE code=? AND date=?",
                        (code, d),
                    ).fetchone()
                except sqlite3.OperationalError:
                    db_row = None
                if db_row:
                    # dailybars 우선 — SSOT. daily_picks 값은 fallback.
                    # Q-20260515-LIMIT-UP-PRICE-FIX (대표 5/15 16:14 catch):
                    # daily_picks.price = ka10017 장중 cur_prc raw → 종가(close) 미반영.
                    # 5/15 069640 한세엠케이 dailybars.close=1613 (상한가) vs daily_picks.price=1298
                    # → 미니캔들 close 1298 raw 사용 → 짧은 빨간 도지 (실 상한가 시각화 결함).
                    # SSOT는 dailybars. dailybars row 존재 시 close (= mini-candle price) 우선 채택.
                    if db_row["close"] is not None:
                        price = db_row["close"]
                    if db_row["open"] is not None:
                        open_price = db_row["open"]
                    if db_row["high"] is not None:
                        high_price = db_row["high"]
                    if db_row["low"] is not None:
                        low_price = db_row["low"]
                    if ta is None and db_row["trade_amount"] is not None:
                        ta = db_row["trade_amount"]

                # Q-20260519-CYCLE11-006 (대표 5/19 15:43 catch): limit-up-trend.json
                # 9건 trade_amount NULL — daily_picks 미적재 + dailybars.trade_amount NULL.
                # 본질: builder가 stock_status_badges.payload.trde_prica_calc (ka10017+ka10081
                # 정합 source, Q-CYCLE11-001 2026-05-19 박제)를 미사용 → 글로벌 일관성 결함.
                # Fix: 3순위 fallback 추가. payload는 ka10081 1순위 + ka10017 trde_qty×cur_prc
                # fallback 2순위 (collect_kiwoom_limit_up.py _row_to_payload).
                # 5/15 케스피온 / 5/18 4종 / 5/19 4종 9건 본질 해소.
                # D축(2026-06-16): ka10017 폐기 후 신규 일자엔 이 payload 가 비어
                # graceful 하게 ta=None 유지(카드도 동일). 과거 일자 잔존 payload 는
                # 보강 source 로 무해하게 잔류. trade_amount 1·2순위(daily_picks/
                # dailybars)가 D축 주 source.
                if ta is None:
                    payload_row = conn.execute(
                        """SELECT CAST(json_extract(payload_json, '$.trde_prica_calc')
                                       AS INTEGER) as ta_payload
                           FROM stock_status_badges
                           WHERE date=? AND stock_code=? AND badge_type='상한가'
                           ORDER BY active_until IS NULL DESC, id DESC LIMIT 1""",
                        (d, code),
                    ).fetchone()
                    if payload_row and payload_row["ta_payload"]:
                        ta = payload_row["ta_payload"]

                # 4순위 (2026-06-18): 서빙 3순위 모두 NULL 인 백필 union 종목
                # (서빙 daily_picks/dailybars/badges 에 행 없음) → 백필 dailybars
                # trade_amount 보강. 서빙 값이 하나라도 있으면(ta is not None) 미사용
                # = 서빙 우선 불변. 백필도 없으면 ta=None 유지 → 프론트 graceful 처리.
                if ta is None:
                    ta = ta_supp.get((code, d))

                cc_prev = _streak_from_dailybars(code, d)
                total_streak = cc_prev + 1  # 오늘 자체 +1

                stocks_out.append(
                    {
                        "code": code,
                        "name": name_map.get(code, code),
                        "change_pct": chg_pct_close,
                        "trade_amount": ta,
                        "consecutive_count": total_streak,
                        "price": price,
                        "open_price": open_price,
                        "high_price": high_price,
                        "low_price": low_price,
                    }
                )
            items.append({"date": d, "count": len(stocks_out), "stocks": stocks_out})
            total_count += len(stocks_out)

    out = {
        "generated_at": datetime.now().isoformat(),
        "dates": target_dates_asc,
        "total_count": total_count,
        "items": items,
    }
    out_path = HOMEPAGE / "data" / "limit-up-trend.json"
    written = _safe_write_json(out_path, out, new_dates_key="dates")
    if written:
        print(
            f"wrote {out_path}: {len(target_dates_asc)} dates, {total_count} limit-up entries "
            f"(SSOT=dailybars.close ∪ daily_picks.change_pct>={LIMIT_UP_THRESHOLD}, D축)"
        )
    return out_path


def build():
    aggregate()
    build_tree_json()  # tree가 정본 — 먼저 빌드
    build_json()  # trend는 tree leaf에 종속
    build_limit_up_trend_json()  # REQ-pm320-ux-cycle #2 — 상한가 추이
    return build_map_json()


if __name__ == "__main__":
    build()
