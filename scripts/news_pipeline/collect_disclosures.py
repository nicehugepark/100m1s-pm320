"""
DART 전자공시 수집 — daily_picks 25종목 대상.
API 키 미설정 시 경고 출력 후 skip (파이프라인 중단 안 됨).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime

from .config import DART_API_KEY, pipeline_date
from .db import connect

logger = logging.getLogger(__name__)

# DART list.json 엔드포인트
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

# report_nm 키워드 → disclosure_cat 분류
# 순서 중요: 더 구체적인 규칙이 위로 (CB가 "전환사채발행결정" 매칭 우선)
CAT_RULES = [
    (["유상증자"], "유상증자"),
    (["무상증자"], "무상증자"),
    (["전환사채", "CB", "신주인수권부사채", "BW"], "CB"),
    (["자기주식", "자사주"], "자사주"),
    (["영업실적", "매출액", "실적"], "실적"),
    (["합병", "영업양수", "영업양도", "분할", "타법인주식", "출자증권양수"], "M&A"),
    (["배당"], "배당"),
    (["횡령", "배임"], "횡령"),
    (["관리종목"], "관리종목"),
    (["상장폐지"], "상장폐지"),
    # HEAD 분기 (REQ-003 머지 측)
    (["주식소각", "소각"], "소각"),
    (["대량보유", "주식등의대량"], "지분변동"),
    (["기업설명회", "IR개최", "IR"], "IR"),
    (["증권발행결과"], "발행결과"),
    # 443ce16 분기 — 단기과열 예고/지정 구분 + 규제성 키워드 확장
    (["단기과열"], "단기과열"),
    (["투자주의"], "투자주의"),
    (["투자경고"], "투자경고"),
    (["투자위험"], "투자위험"),
    (["매매거래정지", "매매거래 정지"], "거래소조치"),
    # 폴백 (구체 규칙 미매칭 시)
    (["자율공시"], "자율공시"),
]

# sentiment 룰 (disclosure_cat → 점수)
SENTIMENT_MAP = {
    "자사주": +2,  # 자기주식 취득 = 강한 호재
    "유상증자": -2,
    "CB": -1,
    "무상증자": +1,
    "실적": 0,  # 실적은 내용 따라 다름 — 기본 중립
    "M&A": 0,
    "배당": +1,
    "횡령": -2,
    "관리종목": -2,
    "상장폐지": -2,
    "소각": +1,
    "지분변동": 0,
    "IR": 0,
    "발행결과": 0,
    "자율공시": 0,
}


def _classify_report(report_nm: str) -> str | None:
    """report_nm에서 disclosure_cat 추출."""
    if not report_nm:
        return None
    for keywords, cat in CAT_RULES:
        for kw in keywords:
            if kw in report_nm:
                return cat
    return None


def _make_summary(title: str, cat: str | None) -> str:
    """초기 요약 — title 원문 보존 (LLM 요약이 없는 경우 폴백).

    길이 제한 제거: 프론트 UI(cal-disc-summary)는 줄바꿈 허용이라 안전.
    LLM 요약(interpret_disclosures)이 있으면 이 값을 덮어씀.
    """
    title = (title or "").strip()
    if cat:
        return f"[{cat}] {title}"
    return title


def _fetch_dart_page(date_str: str, page_no: int) -> tuple[list[dict], int]:
    """DART list.json 단일 페이지. (list, total_page) 반환. 실패 시 ([], 0)."""
    compact = date_str.replace("-", "")
    params = (
        f"?crtfc_key={DART_API_KEY}"
        f"&bgn_de={compact}&end_de={compact}"
        f"&page_count=100&page_no={page_no}"
    )
    url = DART_LIST_URL + params
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "100m1s-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.error("DART API 호출 실패 (page=%d): %s", page_no, e)
        return [], 0

    status = data.get("status")
    if status == "013":
        logger.info("DART: 해당 날짜 공시 없음 (%s)", date_str)
        return [], 0
    if status != "000":
        logger.warning("DART API status=%s, message=%s", status, data.get("message"))
        return [], 0

    return data.get("list", []), int(data.get("total_page", 1) or 1)


def _fetch_dart_list(date_str: str) -> list[dict]:
    """DART list.json 전체 페이지 순회. daily_picks 종목 공시가 후행 페이지에 있어도 catch.

    Why: page_no=1만 fetch 시 total_count > 100인 날 후행 페이지 매칭 공시가 누락됨
         (FLR-20260511 5/11 catch up — 397건 중 page 1에 daily_picks 11종목 매칭 0건).
    """
    items, total_page = _fetch_dart_page(date_str, 1)
    if not items:
        return []
    for page_no in range(2, total_page + 1):
        extra, _ = _fetch_dart_page(date_str, page_no)
        items.extend(extra)
    return items


def _get_target_corps(conn, date_str: str) -> dict[str, str]:
    """daily_picks 종목의 stock_code → corp_code 매핑 반환."""
    picks = conn.execute(
        "SELECT DISTINCT stock_code FROM daily_picks WHERE date=?", (date_str,)
    ).fetchall()
    codes = [r["stock_code"] for r in picks]
    if not codes:
        return {}

    result = {}
    for code in codes:
        row = conn.execute(
            "SELECT corp_code FROM dart_corp_map WHERE stock_code=?", (code,)
        ).fetchone()
        if row:
            result[row["corp_code"]] = code
    return result


def collect(date_str: str = None):
    """메인 수집 함수."""
    if not DART_API_KEY:
        logger.warning("DART_API_KEY 미설정 — 공시 수집 skip")
        print("DART_API_KEY not set — skipping disclosure collection")
        return 0

    today = date_str or pipeline_date()
    now = datetime.now().isoformat()

    with connect() as conn:
        # daily_picks 종목 → corp_code 매핑
        corp_map = _get_target_corps(conn, today)
        if not corp_map:
            logger.info("대상 종목 없음 (daily_picks 또는 dart_corp_map 비어있음)")
            print(f"no target corps for {today}")
            return 0

        # DART API rate limit 체크 (일일 40,000건)
        from .db import dart_api_check, dart_api_increment

        if not dart_api_check(conn, calls_needed=1):
            print("DART API 일일 한도 도달 — 공시 수집 중단")
            return 0

        # DART API 호출
        items = _fetch_dart_list(today)
        dart_api_increment(conn, calls=1)  # 호출 카운트 기록
        if not items:
            print(f"DART: 0 disclosures for {today}")
            return 0

        inserted = 0
        for item in items:
            corp_code = item.get("corp_code", "")
            if corp_code not in corp_map:
                continue

            stock_code = corp_map[corp_code]
            rcept_no = item.get("rcept_no", "")
            if not rcept_no:
                continue

            title = item.get("report_nm", "")
            cat = _classify_report(title)
            sentiment = SENTIMENT_MAP.get(cat, 0) if cat else 0
            summary = _make_summary(title, cat)
            source_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

            try:
                conn.execute(
                    """INSERT INTO disclosures
                       (stock_code, corp_code, date, title, report_nm, rcept_no,
                        pblntf_ty, disclosure_cat, sentiment, summary, source_url, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        stock_code,
                        corp_code,
                        today,
                        title,
                        item.get("report_nm"),
                        rcept_no,
                        item.get("pblntf_ty"),
                        cat,
                        sentiment,
                        summary,
                        source_url,
                        now,
                    ),
                )
                inserted += 1
            except Exception as e:
                # rcept_no UNIQUE 중복 등
                if "UNIQUE" not in str(e):
                    logger.warning("INSERT 실패 rcept_no=%s: %s", rcept_no, e)

        conn.commit()
        print(
            f"DART: {inserted} disclosures inserted for {today} (checked {len(items)} items)"
        )
        return inserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collect()
