"""
/news Phase 1 공용 설정
REQ-003 / DISC-20260409-001
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# HOMEPAGE 경로: M1S_HOMEPAGE 환경변수 필수 (audit a106c8feefbc705f4 — 옛 DB write
# 봉쇄, FLR-AGT-002 거짓 충실성 cache layer 변종 hub). 폴백 폐기: 메인 worktree의
# ad-hoc 실행이 옛 DB(`~/company/100m1s-homepage/data/stocks.db`)에 write하여 cron
# DB(`~/company/100m1s-homepage-cron/data/stocks.db`)와 transaction divergence
# 발생, QA 게이트 거짓 PASS 위험. cron + lead/sub-agent는 env 설정 필수.
_HOMEPAGE_ENV = os.environ.get("M1S_HOMEPAGE")
if not _HOMEPAGE_ENV:
    raise RuntimeError(
        "M1S_HOMEPAGE 환경변수 필수 — fallback 폐기, 옛 DB write 봉쇄 "
        "(audit a106c8feefbc705f4, FLR-AGT-002). "
        "pm320 레포 자립 실행: M1S_HOMEPAGE=<pm320 레포 루트> (코드+데이터 동일 레포), "
        "launchd 초안(launchd/drafts/)은 이 env 를 pm320 레포로 자동 설정."
    )
HOMEPAGE = Path(_HOMEPAGE_ENV)
DATA_DIR = HOMEPAGE / "data"
DB_PATH = DATA_DIR / "stocks.db"
LOG_DIR = ROOT / "scripts" / "news_pipeline" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# RSS — 국내 11종 (연합뉴스 배제 — 법무팀 STOP)
# 2026-04-14 확대: 5 → 11 (증권/경제 전문 섹션 우선)
RSS_FEEDS = {
    # 기존 5종
    "한경": "https://www.hankyung.com/feed/all-news",
    "매경": "https://www.mk.co.kr/rss/30000001/",
    "이데일리": "https://rss.edaily.co.kr/stock_news.xml",
    # 조선비즈: 증권(stock) 섹션 전용 RSS로 교체 (Q-20260613-167 ①, ishikawa 진단).
    # 기존 generic 피드(/rss/?outputType=xml)는 전사 firehose — 라이브 국내 뉴스의
    # 70~76%를 독점하면서 부동산 아파트 매매가·KBO 야구·연예 가십·일본어 기사 범람
    # (종목 무관). §11.15 actual fetch 2026-06-13 KST: generic 100 item 중 증권 기사
    # 사실상 0건 vs category/stock 72 item 전건 증권/마켓(증권사·코스피·금리·뉴욕증시·
    # 주식 매매 상위 종목). Arc CMS 동일 매체 — ToS 리스크 0 (기존 조선비즈와 동일).
    # 미지 category는 HTTP 200 + 0 item(soft 404)이므로 item>0 으로 유효성 확인 완료.
    "조선비즈": "https://biz.chosun.com/arc/outboundfeeds/rss/category/stock/?outputType=xml",
    "이투데이": "https://rss.etoday.co.kr/eto/market_news.xml",
    # 2026-04-14 추가 6종
    "파이낸셜뉴스": "https://www.fnnews.com/rss/r20/fn_realnews_stock.xml",
    "서울경제": "https://www.sedaily.com/rss/finance",
    "헤럴드경제": "https://biz.heraldcorp.com/rss/google/finance",
    "아시아경제": "https://www.asiae.co.kr/rss/stock.htm",
    "뉴시스": "https://www.newsis.com/RSS/economy.xml",
    "전자신문": "https://rss.etnews.com/Section901.xml",
}
# RSS 미확인 매체 (2026-04-14 조사): 뉴스핌(404), 머니투데이(폐쇄),
# 아이뉴스24(403), ZDNet(HTML만), 디지털데일리(없음), 연합인포맥스(없음),
# 더벨(없음), 인포스탁데일리(도메인 이상), 팍스넷(404), 파이낸셜포스트(404)

# RSS — 글로벌 매크로 (트럼프 발언·관세·지정학 추적, 2026-04-10 추가)
# Reuters 2종 주석 처리 (2026-06-05, 이시카와 실측 19:38): 무료 RSS 폐지로 응답
# 없음/리다이렉트 → 죽은 피드. 유지 시 collect_global 매 fire 30s timeout 낭비.
RSS_FEEDS_GLOBAL = {
    # "Reuters_World": "https://feeds.reuters.com/Reuters/worldNews",  # 폐지 (무료 RSS 중단)
    # "Reuters_Business": "https://feeds.reuters.com/reuters/businessNews",  # 폐지
    "CNBC_World": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
}

# RSS — 미국 미장요약(US overnight digest) 전용 (Q-20260605-103 Phase 3).
# 야간 미장 마감 wrap 뉴스 → news_chips(LLM 한국어 요약) 생성용. 종목 매칭 안 함.
# 채택 기준 (§11.15 actual fetch 2026-06-05 KST):
#   - CNBC_Markets : HTTP 200, 30 entries, description 제공, 미장/경제 wrap 다수 → 채택
#   - CNN_Business : 1 stale entry(2019) = 사실상 死피드 → 배제
#   - NYT_Business : 200/49 entries 이나 ToS = personal/non-commercial only +
#                    "commercial use 금지" (공식 ToS, WebSearch 2026-06-05) → 배제
#                    (공개 서비스 노출 = commercial 해석 리스크, 법무 게이트 미통과)
#   - MarketWatch  : 200/10 entries 이나 personal-finance 편향(은퇴/주택) → 보조 보류
# CNBC 단독 채택 + 한경 글로벌(국제 섹션 + Naver 보강)로 한국 투자자 관점 보완.
RSS_FEEDS_US_DIGEST = {
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    # WSJ (대표 2026-06-06 "wsj 뉴스도 참고"). Dow Jones 공식 RSS — feeds.content.
    # dowjones.io/public/rss. 2026-06-06 실측: Markets 200/60item, Economy 200/36item
    # 모두 title+summary+link 파싱 정상(feedparser). 페이월이라 본문은 RSS summary 범위만
    # 사용 — news_chips.summary 는 어차피 LLM 자체 생성이라 법무 게이트 기존 CNBC·한경 동일
    # (RSS 원문 verbatim 노출 0건). FOMC/연준 기사 풀 강화(Economy 피드에 Fed 다수).
    "WSJ": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "WSJ경제": "https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
    # WSJ World News (Q-20260613-165 ③, 대표 12:50 승인) — 알자지라(법무 게이트 BLOCK)
    # 대체 지정학 소스. Dow Jones 공식 RSS 동일 계열(법무 리스크 = 기존 WSJ 동일, RSS
    # summary 범위만·본문 verbatim 노출 0). §11.15 actual fetch 2026-06-13 13:01 KST:
    # HTTP 200 / 72 entries / feedparser title+summary+link 정상 (지정학 다수 — 첫 항목
    # "Trump Says U.S. Killed Venezuelan Tren de Aragua Gang Leader"). 무력분쟁·제재·
    # 공급망 기사로 ④ 지정학 인과 룰 입력 강화.
    "WSJ세계": "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
}

# 한경 글로벌마켓 보조 소스 (대표 지시 2026-06-05 "한경 글로벌마켓").
# 실측 결과(2026-06-05): hankyung.com 에 글로벌마켓 *전용* RSS 부재 — /feed/globalmarket
# 등 5종 후보 모두 HTTP 404. 가장 근접한 공식 RSS = 국제 섹션(/feed/international,
# 200/50 entries, 트럼프·관세·미국 등 미장 연관 기사 포함). 직접 스크레이핑은 법무
# 게이트 미통과 → 채택 금지. 따라서 (a) 국제 섹션 RSS + (b) Naver 공식 검색 API
# (naver_news_search.search_news, query="한국경제 뉴욕증시") 양 공식 경로만 사용.
# 한경 ToS 는 기존 RSS_FEEDS 한경(/feed/all-news)과 동일 매체라 신규 약관 리스크 0.
RSS_FEEDS_HK_GLOBAL = {
    "한경국제": "https://www.hankyung.com/feed/international",
}
# Naver 보강 검색어 (공식 Open API 경로 — 직접 스크레이핑 아님)
HK_GLOBAL_NAVER_QUERY = "한국경제 뉴욕증시"

# Gemini
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_INPUT_TOKEN_CAP = 4000  # input cap (경영지원팀)
GEMINI_DAILY_CALL_LIMIT = 10000  # 안전 상한
GEMINI_MONTHLY_BUDGET_USD = 150.0  # GCP Budget Alert 수준

# API 키 (.env)
GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or os.environ.get("GOOGLE_AI_API_KEY")
)
DART_API_KEY = os.environ.get("DART_API_KEY")
DART_DAILY_CALL_LIMIT = 40_000  # DART API 일일 호출 한도
DART_CALL_WARN_THRESHOLD = 38_000  # 경고 임계값 (95%)


def pipeline_date() -> str:
    """PIPELINE_DATE 환경변수 또는 오늘 날짜 반환 (YYYY-MM-DD)."""
    from datetime import datetime

    return os.environ.get("PIPELINE_DATE") or datetime.now().strftime("%Y-%m-%d")


# 장중 연구수집 차단 경계 (KST). 09:00~15:31 = KRX 정규장 + 프로덕션 폴링 구간.
# 이 구간에 collect_minutebars*.py 등 연구/백필 수집기를 수동 실행하면 프로덕션
# 폴링과 키움 앱키·호출한도(rate limit)를 경합 → 프로덕션 파이프라인이 throttle/
# timeout 으로 멈출 수 있음 (대표 직접 지시 2026-05-26: "절대로 파이프라인이 멈추지
# 않도록 해"). 따라서 연구수집기는 장중 실행 시 즉시 종료.
#
# 마진 단축 (대표 지시 2026-06-05): "30분이면 장이 끝나는데 마진이 너무 길다 1,2분
# 으로 짧게 가져가". 장마감 15:30 + 마진 1분 = :31 fire 통과. 5/26 폴링경합 방지
# 정책은 유지하되 가드 차단 종료를 15:30 까지로 둔다 — 장마감 후 프로덕션 폴링은
# 사실상 정지하므로 15:31 분봉 백필이 가드에 막히지 않게 한다 (6/5 minute-backfill
# 0건 종료 사고 봉쇄). 09:00 시작 경계는 무변경.
#
# 주의: guard 경계(_INTRADAY_BLOCK_END)는 가드 전용. watchdog 의 "정규장 구간"
# 판정(_MARKET_HOURS_END)과 분리한다. watchdog 은 stale 복구를 정규장에만 시도하므로
# 마감 직후 여유(15:35)를 유지해야 하지만, 가드는 백필을 위해 마진을 줄여야 한다.
# 두 의미가 충돌하므로 단일 상수 공유를 끊고 별도 상수로 둔다 (FLR-20260406 모듈화
# 정합 — 의미가 다른 두 경계를 강제 단일화하면 한쪽 의도가 깨짐).
_INTRADAY_BLOCK_START = (9, 0)  # 09:00 KST 포함 (가드 + watchdog 공통 시작 경계)
# 장마감 15:30 직후 :31 분봉 백필이 통과하도록 경계 (15,30) — 15:30 까지 차단,
# 15:31 부터 통과 (마진 1분). plist minute-backfill :31 fire 가 첫 실동작이 된다
# (대표 지시 2026-06-05 "15:31 + 마진 1분" 정합). 2026-06-05 (15,31)→(15,30) 정정.
_INTRADAY_BLOCK_END = (15, 30)  # 가드 차단 종료 (15:30 KST 포함, :31 통과)
_MARKET_HOURS_END = (
    15,
    35,
)  # watchdog 정규장 판정 종료 (15:35 KST, 마감 직후 복구 여유)


def guard_intraday_research(tool_name: str = "연구수집기") -> None:
    """장중(09:00~15:30 KST) 연구/백필 수집기 실행 차단 가드.

    프로덕션 폴링과 키움 앱키·호출한도 경합을 원천 차단한다. 차단 구간에 호출되면
    사유를 출력하고 즉시 sys.exit(0) (정상 종료 — cron/스크립트 cascade 실패 방지).
    환경변수 ``ALLOW_INTRADAY_RESEARCH=1`` 명시 시에만 장중 실행을 허용한다.

    주말·공휴일은 정규장이 없으므로 차단하지 않는다(연구수집 정상 허용).
    마감 후(15:31 이상)·장 시작 전(09:00 미만) 정상 수집은 막지 않는다.
    """
    import sys
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if os.environ.get("ALLOW_INTRADAY_RESEARCH") == "1":
        return

    now = datetime.now(ZoneInfo("Asia/Seoul"))

    # 주말은 정규장 없음 → 차단 안 함.
    if now.weekday() >= 5:  # 5=토, 6=일
        return

    # 공휴일은 정규장 없음 → 차단 안 함.
    if is_market_holiday(now.strftime("%Y-%m-%d")):
        return

    cur = (now.hour, now.minute)
    if _INTRADAY_BLOCK_START <= cur <= _INTRADAY_BLOCK_END:
        print(
            f"[INTRADAY-GUARD] {tool_name} 장중 실행 차단 — "
            f"현재 {now:%Y-%m-%d %H:%M:%S} KST 는 KRX 정규장 구간 "
            f"(09:00~15:30). 프로덕션 폴링과 키움 앱키·호출한도 경합 방지 "
            f"(대표 지시 2026-05-26). 장중 실행이 꼭 필요하면 "
            f"ALLOW_INTRADAY_RESEARCH=1 환경변수로 명시 실행. 즉시 종료.",
            file=sys.stderr,
        )
        sys.exit(0)


def is_market_hours(now=None) -> bool:
    """현재(또는 지정 시각)가 KRX 정규장 구간(평일 09:00~15:35 KST)인지 여부.

    watchdog(카드/집계 stale 감지) 가 장중에만 복구를 시도하도록 쓰는 헬퍼.
    가드 경계(_INTRADAY_BLOCK_END=15:30)와 분리된 _MARKET_HOURS_END(15:35)를 쓴다.
    가드는 백필을 위해 마진을 줄였지만(2026-06-05), watchdog 의 정규장 판정은 마감
    직후 복구 여유(15:35)를 유지해야 하므로 두 경계 의미를 분리한다(start 는 공유).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if now is None:
        now = datetime.now(ZoneInfo("Asia/Seoul"))
    if now.weekday() >= 5:  # 주말
        return False
    if is_market_holiday(now.strftime("%Y-%m-%d")):  # 공휴일
        return False
    cur = (now.hour, now.minute)
    return _INTRADAY_BLOCK_START <= cur <= _MARKET_HOURS_END


# 한국 공휴일 (매년 초 갱신 필요 — 대체공휴일은 수동 추가)
KR_HOLIDAYS_2026 = {
    "2026-01-01",  # 신정
    "2026-01-28",  # 설날 연휴
    "2026-01-29",  # 설날
    "2026-01-30",  # 설날 연휴
    "2026-03-01",  # 삼일절
    "2026-03-02",  # 삼일절 대체공휴일
    "2026-05-01",  # 근로자의 날 (KRX 휴장 — REQ-022 §V 정합 필수)
    "2026-05-05",  # 어린이날
    "2026-05-24",  # 부처님오신날
    "2026-05-25",  # 부처님오신날 대체공휴일
    "2026-06-03",  # 제9회 전국동시지방선거 (KRX 휴장 — holidays.json SoT 정합, 2026-06-03 cycle session 동기화)
    "2026-06-06",  # 현충일
    "2026-08-15",  # 광복절
    "2026-08-17",  # 광복절 대체공휴일
    "2026-09-24",  # 추석 연휴
    "2026-09-25",  # 추석
    "2026-09-26",  # 추석 연휴
    "2026-10-03",  # 개천절
    "2026-10-09",  # 한글날
    "2026-12-25",  # 크리스마스
}


def is_market_holiday(date_str: str = None) -> bool:
    """주식시장 휴장일 여부 판단 (토/일 + 한국 공휴일).

    Returns True if the given date is a non-trading day.
    """
    from datetime import datetime

    target = date_str or pipeline_date()
    dt = datetime.strptime(target, "%Y-%m-%d")
    # 토(5), 일(6)
    if dt.weekday() >= 5:
        return True
    # 공휴일
    if target in KR_HOLIDAYS_2026:
        return True
    return False


def last_trading_date(date_str: str = None) -> str:
    """가장 최근 거래일 반환. 주어진 날짜가 거래일이면 그대로 반환."""
    from datetime import datetime, timedelta

    target = date_str or pipeline_date()
    dt = datetime.strptime(target, "%Y-%m-%d")
    for _ in range(10):  # 최대 10일 역추적 (연휴 대비)
        candidate = dt.strftime("%Y-%m-%d")
        if not is_market_holiday(candidate):
            return candidate
        dt -= timedelta(days=1)
    return target  # 안전 폴백
