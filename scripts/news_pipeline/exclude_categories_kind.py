"""KIND 시장조치 4종 (관리/환기/정리/불성실) 종목 list fetch + 종목코드 매핑.

heroshik_strict_5_6_v3 의 영웅식 5종 EXCLUDE 룰 중 4종 보강 모듈.
나머지 1종 (ETF) 은 stocks.market = 'ETF' 또는 별도 ETF list로 처리.

KIND 엔드포인트 (2026-05-08 실측):
  - 관리종목: investwarn/adminissue.do (searchAdminIssueSub, forward=adminissue_sub)
  - 투자환기: investwarn/hwangiissue.do (searchHwangiIssueSub, forward=hwangiissue_sub)
  - 정리매매: investwarn/delcompany.do (searchDelCompanySub, forward=delcompany_sub)
  - 불성실공시: investwarn/undisclosure.do (searchUnfaithfulDisclosureCorpSub, forward=undisclosure_sub)

스크레이핑 패턴 (collect_kind.py 기존 룰 정합):
  - User-Agent 고정, JSESSIONID 쿠키 1회 발급 후 재사용
  - 각 엔드포인트 1회 POST → HTML 파싱
  - 응답 인코딩 자동 감지 (utf-8 / euc-kr 혼재)
  - 실패 시 graceful — 빈 set + WARN

종목명 → code 매핑 (KIND HTML은 ticker 미노출):
  - stocks.name 정확 매칭 (KIND 표기명과 stocks.name 정확 일치 가정)
  - 미매칭 종목은 unmatched list로 보고 (조용히 무시 금지)
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

USER_AGENT = "100m1s-bot/1.0 (contact: nicehugepark@gmail.com)"
REQUEST_TIMEOUT = 20
RATE_LIMIT_SEC = 2.5

KIND_BASE = "https://kind.krx.co.kr"
KIND_LANDING_URL = f"{KIND_BASE}/main.do?method=loadInitPage"

# (tag, endpoint, method, forward)
KIND_STATUS_ENDPOINTS = [
    ("관리종목", "investwarn/adminissue.do", "searchAdminIssueSub", "adminissue_sub"),
    (
        "투자환기",
        "investwarn/hwangiissue.do",
        "searchHwangiIssueSub",
        "hwangiissue_sub",
    ),
    ("정리매매", "investwarn/delcompany.do", "searchDelCompanySub", "delcompany_sub"),
    (
        "불성실공시",
        "investwarn/undisclosure.do",
        "searchUnfaithfulDisclosureCorpSub",
        "undisclosure_sub",
    ),
]

_RE_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
_RE_LEAD_NUM = re.compile(r"^\d+\s*")


def _decode_response(raw: bytes) -> str:
    """KIND 응답 인코딩 자동 감지 (utf-8 / euc-kr / cp949)."""
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _open_session() -> str:
    """KIND 메인 페이지 GET → JSESSIONID 쿠키 추출. 빈 문자열이면 실패."""
    try:
        req = urllib.request.Request(
            KIND_LANDING_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            cookies = resp.headers.get_all("Set-Cookie") or []
        for c in cookies:
            m = re.search(r"JSESSIONID=([^;]+)", c)
            if m:
                return f"JSESSIONID={m.group(1)}"
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning("KIND 세션 발급 실패: %s", e)
    return ""


def _post_kind_list(
    cookie: str,
    endpoint: str,
    method: str,
    forward: str,
) -> str:
    """KIND 시장조치 sub-action POST. 본문(HTML) 반환. 실패 시 raise."""
    form = {
        "method": method,
        "currentPageSize": "500",
        "pageIndex": "1",
        "forward": forward,
        "orderMode": "",
        "orderStat": "",
        "searchCorpName": "",
        "searchCodeType": "",
        "marketType": "",
        "repIsuSrtCd": "",
        "isuSrtCd2": "",
    }
    data = urllib.parse.urlencode(form).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": f"{KIND_BASE}/{endpoint}",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(
        f"{KIND_BASE}/{endpoint}", data=data, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    return _decode_response(raw)


def _parse_status_rows(body: str) -> list[tuple[str, str]]:
    """KIND 시장조치 응답 HTML → [(name, designation_date), ...].

    파싱 규칙:
      - <tr>...</tr> 블록 단위
      - 태그 제거 후 첫 YYYY-MM-DD 토큰을 designation_date 로
      - date 앞 텍스트의 leading 숫자(rank) 제거 후 첫 토큰을 name 으로
      - date 미발견 행은 skip (헤더 등)
    """
    out: list[tuple[str, str]] = []
    for row in _RE_TR.findall(body):
        text = _RE_TAG.sub(" ", row)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        m = _RE_DATE.search(text)
        if not m:
            continue
        date = m.group(1)
        head = text[: m.start()].strip()
        head = _RE_LEAD_NUM.sub("", head)
        if not head:
            continue
        name = head.split()[0]
        out.append((name, date))
    return out


def fetch_kind_status_lists() -> dict[str, list[tuple[str, str]]]:
    """4개 KIND 시장조치 list 일괄 fetch.

    Returns:
        {tag: [(name, designation_date), ...]}
        실패한 tag는 빈 list 로 (graceful).
    """
    result: dict[str, list[tuple[str, str]]] = {}
    cookie = _open_session()
    if not cookie:
        logger.warning("KIND 세션 미확보 — 빈 cookie로 강행 (실패 가능)")

    for i, (tag, endpoint, method, forward) in enumerate(KIND_STATUS_ENDPOINTS):
        try:
            body = _post_kind_list(cookie, endpoint, method, forward)
            rows = _parse_status_rows(body)
            result[tag] = rows
            logger.info("KIND %s: %d rows", tag, len(rows))
        except Exception as e:
            logger.warning("KIND %s fetch 실패 — 빈 set: %s", tag, e)
            result[tag] = []
        if i < len(KIND_STATUS_ENDPOINTS) - 1:
            time.sleep(RATE_LIMIT_SEC)
    return result


def filter_by_designation_date(
    rows: list[tuple[str, str]], on_or_before: str
) -> list[tuple[str, str]]:
    """designation_date <= on_or_before 종목만."""
    return [(n, d) for n, d in rows if d <= on_or_before]


def map_names_to_codes(
    conn: sqlite3.Connection, names: list[str]
) -> tuple[dict[str, str], list[str]]:
    """stocks.name → code 정확 매칭. (matched_dict, unmatched_names) 반환."""
    if not names:
        return {}, []
    placeholders = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT name, code FROM stocks WHERE name IN ({placeholders})", tuple(names)
    ).fetchall()
    name_to_code: dict[str, str] = {}
    for r in rows:
        # sqlite3.Row vs tuple 모두 지원
        if hasattr(r, "keys"):
            name_to_code[r["name"]] = r["code"]
        else:
            name_to_code[r[0]] = r[1]
    unmatched = [n for n in names if n not in name_to_code]
    return name_to_code, unmatched


def collect_excluded_codes(
    conn: sqlite3.Connection, target_date: str
) -> dict[str, dict]:
    """target_date 시점 KIND 4종 시장조치 exclude code set + 통계.

    Returns:
        {
            tag: {
                "fetched": int,        # KIND 응답 총 행
                "before_date": int,    # designation_date <= target_date
                "matched": int,        # stocks.name 매칭 성공
                "unmatched_names": list[str],
                "codes": set[str],
            }
        }
    """
    raw = fetch_kind_status_lists()
    out: dict[str, dict] = {}
    for tag, rows in raw.items():
        before = filter_by_designation_date(rows, target_date)
        names = [n for n, _ in before]
        name_to_code, unmatched = map_names_to_codes(conn, names)
        out[tag] = {
            "fetched": len(rows),
            "before_date": len(before),
            "matched": len(name_to_code),
            "unmatched_names": unmatched,
            "codes": set(name_to_code.values()),
        }
    return out


def main():
    """단독 실행 시 5/6 시점 4종 카테고리 list 출력 (검증용)."""
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", default="2026-05-06")
    # --db default: config.py DB_PATH (env M1S_HOMEPAGE override 가능).
    # cron worktree 격리 (lead-meta §11.32) 정합 — 2026-05-28 cycle25 cron-isolation A1.
    from .config import DB_PATH as _DEFAULT_DB

    parser.add_argument(
        "--db",
        default=str(_DEFAULT_DB),
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    result = collect_excluded_codes(conn, args.target_date)
    print(f"\n=== KIND 4종 exclusion @ {args.target_date} ===")
    for tag, info in result.items():
        print(
            f"{tag}: fetched={info['fetched']} before_date={info['before_date']} "
            f"matched={info['matched']} unmatched={len(info['unmatched_names'])}"
        )
        if info["unmatched_names"]:
            print(f"  unmatched sample: {info['unmatched_names'][:5]}")
    all_codes = set().union(*(info["codes"] for info in result.values()))
    print(f"\nUNION codes: {len(all_codes)}")
    conn.close()


if __name__ == "__main__":
    main()
