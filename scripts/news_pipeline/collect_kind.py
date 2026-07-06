"""
KIND (한국거래소 전자공시) 수집 — daily_picks 종목 고유 공시만.
법무 가드레일 준수 (REQ-20260415-REQ-001).

수집 필드 화이트리스트 (변경 금지):
- stock_code, 공시유형(category), 제목(title), 시각(datetime), 원문링크(source_url)
본문/PDF/HWP 저장 금지.

법무 가드레일:
- Rate limit: 2.5초/요청, 동시 1커넥션, 일 1,000건 상한
- 장중 회피: 09:00~15:30 KST skip
- User-Agent 고정
- 연속 4xx/5xx 10회 → 자동 중단
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import pipeline_date
from .db import connect

logger = logging.getLogger(__name__)

# KIND 엔드포인트 (문서화되지 않음 — 실제 응답으로 스키마 파악 필요)
KIND_URL = "https://kind.krx.co.kr/disclosure/todaydisclosure.do"

# 법무 가드레일 상수 (변경 시 법무 재검토 필수)
USER_AGENT = "100m1s-bot/1.0 (contact: nicehugepark@gmail.com)"
RATE_LIMIT_SEC = 2.5
DAILY_CALL_LIMIT = 1000
CONSECUTIVE_ERROR_LIMIT = 10
REQUEST_TIMEOUT = 15

# 장중 회피 시간대 (KST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 0
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN = 30
KST = timezone(timedelta(hours=9))

# DART에 없는 KIND 고유 카테고리 키워드 → category
KIND_EXCLUSIVE_RULES = [
    (["불성실공시"], "불성실공시"),
    (["조회공시"], "조회공시"),
    (["투자위험종목", "투자위험"], "투자위험"),
    (["투자경고종목", "투자경고"], "투자경고"),
    (["투자주의종목", "투자주의"], "투자주의"),
    (["단기과열"], "단기과열"),
    (["변동성완화장치", "VI 발동", "VI발동"], "VI"),
    (["매매거래정지", "거래정지"], "거래정지"),
    (["관리종목 지정", "관리종목지정"], "관리종목"),
]


def is_kind_exclusive(title: str) -> str | None:
    """DART에 없는 KIND 고유 공시만 카테고리 반환. 아니면 None."""
    if not title:
        return None
    for keywords, cat in KIND_EXCLUSIVE_RULES:
        for kw in keywords:
            if kw in title:
                return cat
    return None


def _is_market_hours() -> bool:
    """현재 KST가 09:00~15:30 장중이면 True."""
    now = datetime.now(KST)
    if now.weekday() >= 5:  # 주말은 장중 아님
        return False
    open_m = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN
    close_m = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    cur_m = now.hour * 60 + now.minute
    return open_m <= cur_m <= close_m


def _get_daily_call_count(conn, date_str: str) -> int:
    """KIND 일일 호출 카운트."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kind_api_usage (
            date TEXT PRIMARY KEY, call_count INTEGER DEFAULT 0, last_call_at TEXT
        )"""
    )
    row = conn.execute(
        "SELECT call_count FROM kind_api_usage WHERE date=?", (date_str,)
    ).fetchone()
    return row["call_count"] if row else 0


def _increment_call_count(conn, date_str: str):
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO kind_api_usage (date, call_count, last_call_at)
           VALUES (?, 1, ?)
           ON CONFLICT(date) DO UPDATE SET
             call_count = call_count + 1,
             last_call_at = ?""",
        (date_str, now, now),
    )
    conn.commit()


# KIND 행 파싱용 정규식 (실측 스키마 2026-04-15 기준)
# - 시각: <td class="first txc">HH:MM</td>
# - 종목명: <a id="companysum" ... title='종목명'> 종목명</a>
# - rcept_no: openDisclsViewer('YYYYMMDDNNNNNN','')
# - 제목: <a href="#viewer" ... title='...'>제목</a> (정정 태그 포함 가능)
# 주의: KIND HTML은 종목 ticker(6자리)를 노출하지 않음. companysummary_open/fnPopStockPrices
# 의 인자는 KIND 내부 회사 ID이므로 stocks.name → code 매핑으로 해결.
_RE_ROW = re.compile(r'<tr id="parkman"[^>]*>(.*?)</tr>', re.S)
_RE_TIME = re.compile(r'<td class="first txc">\s*(\d{1,2}:\d{2})\s*</td>')
_RE_CORP = re.compile(r"companysummary_open\(\'(\d+)\'\)[^>]*title=\'([^\']+)\'")
_RE_RCEPT = re.compile(r"openDisclsViewer\('(\d+)','([^']*)'\)")
_RE_TITLE = re.compile(
    r'<a href="#viewer" onclick="openDisclsViewer\([^)]*\)" title=\'([^\']+)\'>(.*?)</a>',
    re.S,
)


def _parse_kind_html(body: str, date_str: str) -> list[dict]:
    """KIND HTML 행 파싱 → 화이트리스트 dict 리스트.

    반환 필드: stock_name, rcept_no, title, datetime(HH:MM), source_url
    stock_code는 미노출 → collect()에서 stocks.name 매핑으로 채움.
    """
    out: list[dict] = []
    for row in _RE_ROW.findall(body):
        m_corp = _RE_CORP.search(row)
        m_rcept = _RE_RCEPT.search(row)
        m_time = _RE_TIME.search(row)
        m_title = _RE_TITLE.search(row)
        if not (m_corp and m_rcept and m_title):
            continue
        stock_name = m_corp.group(2).strip()
        rcept_no = m_rcept.group(1)
        # title 속성이 [정정] 태그 없는 순수 제목. 내부 텍스트는 [정정] 포함 가능.
        title = m_title.group(1).strip()
        hhmm = m_time.group(1) if m_time else ""
        # KIND 공시 뷰어 링크 (원문 상세 페이지)
        source_url = f"https://kind.krx.co.kr/common/disclsviewer.do?method=search&acptno={rcept_no}"
        out.append(
            {
                "stock_name": stock_name,
                "rcept_no": rcept_no,
                "title": title,
                "datetime": f"{date_str} {hhmm}" if hhmm else date_str,
                "source_url": source_url,
            }
        )
    return out


def fetch_kind_today(date_str: str) -> list[dict]:
    """KIND todaydisclosure.do POST + HTML 파서.

    반환 dict 화이트리스트: stock_name, rcept_no, title, datetime, source_url
    (본문/PDF/HWP 절대 금지)
    """
    # POST 파라미터 (실측 2026-04-15: 100건 정상 반환 확인)
    form = {
        "method": "searchTodayDisclosureSub",
        "currentPageSize": "100",
        "pageIndex": "1",
        "orderMode": "0",
        "orderStat": "D",
        "forward": "todaydisclosure_sub",
        "chose": "S",
        "todayFlag": "Y",
        "selDate": date_str,
    }
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        KIND_URL,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": KIND_URL,
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning("KIND 호출 실패: %s", e)
        raise

    if not body:
        return []
    return _parse_kind_html(body, date_str)


def _get_target_stock_codes(conn, date_str: str) -> set[str]:
    """daily_picks 종목코드 집합."""
    rows = conn.execute(
        "SELECT DISTINCT stock_code FROM daily_picks WHERE date=?", (date_str,)
    ).fetchall()
    return {r["stock_code"] for r in rows}


def _get_name_to_code_map(conn, codes: set[str]) -> dict[str, str]:
    """target 종목의 name→code 역매핑. KIND HTML은 ticker 미노출 → 종목명 매칭."""
    if not codes:
        return {}
    q = "SELECT code, name FROM stocks WHERE code IN ({})".format(
        ",".join("?" * len(codes))
    )
    rows = conn.execute(q, tuple(codes)).fetchall()
    return {r["name"]: r["code"] for r in rows if r["name"]}


def collect(date_str: str = None) -> int:
    """KIND 수집 메인. 법무 가드레일 전체 적용."""
    today = date_str or pipeline_date()

    # 장중 회피 (과거 날짜 재실행은 허용)
    if today == datetime.now(KST).strftime("%Y-%m-%d") and _is_market_hours():
        logger.info("KIND: 장중 시간대 (09:00~15:30 KST) — skip")
        print("KIND: skipped (market hours)")
        return 0

    now_iso = datetime.now().isoformat()

    with connect() as conn:
        # 일일 상한 체크
        call_count = _get_daily_call_count(conn, today)
        if call_count >= DAILY_CALL_LIMIT:
            logger.warning(
                "KIND 일일 상한 도달: %d/%d — 중단", call_count, DAILY_CALL_LIMIT
            )
            print(f"KIND: daily limit reached ({call_count}/{DAILY_CALL_LIMIT})")
            return 0

        targets = _get_target_stock_codes(conn, today)
        if not targets:
            logger.info("KIND: daily_picks 비어있음 (%s)", today)
            print(f"KIND: no target stocks for {today}")
            return 0
        name_to_code = _get_name_to_code_map(conn, targets)

        # 엔드포인트 1회 호출 (일자 전체 리스트 수신)
        try:
            _increment_call_count(conn, today)
            items = fetch_kind_today(today)
            time.sleep(RATE_LIMIT_SEC)  # 레이트리밋
        except Exception as e:
            logger.warning("KIND fetch 실패 — 빈 결과로 처리: %s", e)
            print(f"KIND: fetch failed ({e})")
            return 0

        if not items:
            print(f"KIND: 0 disclosures for {today} (parser returned empty)")
            return 0

        consecutive_errors = 0
        inserted = 0
        for item in items:
            # KIND HTML은 ticker 미노출 → 종목명으로 target 매칭
            stock_name = item.get("stock_name", "")
            stock_code = name_to_code.get(stock_name, "")
            if not stock_code:
                continue

            title = item.get("title", "")
            cat = is_kind_exclusive(title)
            if not cat:
                # KIND 고유 공시만 수집 (DART와 중복 방지)
                continue

            rcept_no = (
                item.get("rcept_no") or f"KIND-{stock_code}-{item.get('datetime', '')}"
            )
            source_url = item.get("source_url", "")
            item.get("datetime") or today

            try:
                conn.execute(
                    """INSERT INTO disclosures
                       (stock_code, corp_code, date, title, report_nm, rcept_no,
                        pblntf_ty, disclosure_cat, sentiment, summary,
                        source_url, source, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        stock_code,
                        "",  # corp_code 없음 (KIND는 종목코드 기반)
                        today,
                        title[:200],
                        title[:200],
                        rcept_no,
                        "KIND",
                        cat,
                        0,  # sentiment 별도 미지정
                        f"[{cat}] {title[:20]}",
                        source_url,
                        "KIND",
                        now_iso,
                    ),
                )
                inserted += 1
                consecutive_errors = 0
            except Exception as e:
                if "UNIQUE" in str(e):
                    continue  # silent skip
                consecutive_errors += 1
                logger.warning("KIND INSERT 실패: %s", e)
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    logger.error(
                        "KIND 연속 오류 %d회 — 자동 중단", CONSECUTIVE_ERROR_LIMIT
                    )
                    print(
                        f"KIND: aborted after {CONSECUTIVE_ERROR_LIMIT} consecutive errors"
                    )
                    break

        conn.commit()
        print(f"KIND: {inserted} disclosures inserted for {today}")
        return inserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        collect()
    except Exception as e:
        logger.warning("KIND 수집 전체 실패 — 파이프라인 유지: %s", e)
        print(f"KIND: skipped ({e})")
