#!/usr/bin/env python3
"""공식 기관 wire/RSS 속보 lane collector — 조니 확정 spec ②.

소스 7종 (2026-06-12 + 2026-06-15 probe 실측 확정, §11.15 외부 spec 사전 검증 통과):
  - SEC press releases       https://www.sec.gov/news/pressreleases.rss        (RSS2.0, pubDate RFC822 -0400)
  - Fed press releases       https://www.federalreserve.gov/feeds/press_all.xml (RSS2.0, pubDate CDATA RFC822 GMT)
  - White House briefings    https://www.whitehouse.gov/briefings-statements/feed/ (WordPress RSS2.0, pubDate +0000)
  - CNBC US Top News         https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114 (RSS2.0, pubDate RFC822 GMT)
    * 이시카와 GO·ToS 검증. 대표 catch "미국발 신선도 낮음" 대응 핵심(고빈도 시장속보, probe 최신 0.1h)
  - BEA(경제분석국)           https://apps.bea.gov/rss/rss.xml                  (RSS2.0 변형, pubDate RFC822 EDT)
    * 미 정부저작물(저작권 free). GDP·무역수지 등 1급 거시지표. 발표빈도 월/분기 → 48h 윈도우 대부분 0건
  - 연합뉴스 경제             https://www.yna.co.kr/rss/economy.xml             (RSS2.0, pubDate +0900)
    * 속보 전용 feed(breakingnews.xml)는 404 실측 — 경제 섹션 채택 (PM320 도메인 정합)
  - 금융위원회 보도자료        http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111  (RSS2.0, dc:date "Y-m-d H:M:S" KST 일 단위)
    * 기획재정부 RSS는 HTML Error 페이지 실측 FAIL — 금융위로 대체 (lead spec 대체 순위)

저장 필드: 제목+출처+직링크+발행시각(KST)만 — 본문 비저장 (저작권 안전권), LLM 재해석 0.
산출: $M1S_HOMEPAGE/pm320/data/wire_news.json — 48h 윈도우, 상한 36건, dedup=URL, 멱등
      (volatile 필드 0 — 2연속 실행 diff 0. 변경 없으면 파일 write 자체 생략).
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import (
    Path,  # autoflake 제거 재발 방지 (DOC-20260524-FLR-001) — L168 Path(__file__) 사용
)

# 한국어 인과 해석 레이어 (Q-20260612-154) — 같은 디렉토리 모듈 (sys.path[0] = 스크립트 dir)
import collect_truthsocial  # noqa: E402  트럼프 lane (대표 GO·법무 조건부 GO DOC-20260614-LEGAL-002)
import interpret_wire

# collect_truthsocial: FEEDS(공식 RSS)와 별개 경로(3자 API)이나 동일 wire 항목 shape 산출 →
# collect()가 fetched 에 합산 (단일 운반체 wire_news.json). graceful: 키부재/실패 = 빈 리스트.

KST = timezone(timedelta(hours=9))
# 런타임 python3.9 호환 — datetime.UTC(3.11+) 미사용 (launchd 시스템 python3 = 3.9.6 실측)
UTC = timezone(timedelta(hours=0))
UA = "Mozilla/5.0 (compatible; 100m1s-wire/1.0; nicehugepark@gmail.com)"
# WINDOW_HOURS=48 유지 (라이브 실측 2026-06-15 — 줄이면 미국 커버리지 사망 근거):
#   미국 기관은 발표 빈도가 낮다 — probe 실측 SEC 최신 ≈48h 경계 / Fed 최신 >48h (주말 0건).
#   24h 로 줄이면 주말·야간에 SEC/Fed 미국 기관 항목이 0 → "야간 미국 속보 노출" 목표와 정반대.
#   "stale 섞임" 우려는 윈도우가 아니라 발표 빈도 문제 (실측 산출물 전건 12h 이내, stale 없음).
WINDOW_HOURS = 48
# MAX_ITEMS 30→36 (라이브 실측 + 트럼프 lane 신설 반영, 보수적 상향):
#   소스 8종(SEC/Fed/WH/CNBC/BEA/연합/금융위/트럼프) × PER_SOURCE_CAP 10. 평일 피크 CAP 합계가
#   30 초과 → 시각순 절단 시 *오래된* 항목(=발표 텀 긴 미국 SEC/Fed)이 먼저 잘려 미국 커버리지 손실.
#   🔴 CNBC(고빈도) 추가로 총량 절단 압력이 커졌으나 PER_SOURCE_CAP 10 이 소스별 하한을 보장 —
#     CNBC가 아무리 많아도 10건까지만 점유, SEC/Fed(발표 시 48h 내 1~3건)는 cap 미달이라 cap
#     단계에서 안 잘린다. 총량 절단(36)은 최신순이라 오래된 정부기관이 밀릴 수 있으나, frontend
#     가 국가별 열(US/KR)로 분리 렌더 → US 풀에 미국 8소스가 함께 담겨 US 열 15건 채움.
#   36 근거(격리 dry-run 실측 2026-06-15 10:30 KST): 최종 산출 34건 = CNBC 10·연합 10·트럼프 10·
#     금융위 3·WH 1 / SEC 0·Fed 0·BEA 0. SEC·Fed·BEA 의 0건은 *절단이 아니라 48h 윈도우 밖*
#     (발표빈도 낮음: SEC 최신 57h·Fed 72h·BEA 108h 前 = cutoff 탈락). 즉 총량 34/36 으로 절단
#     압력 자체가 아직 없음(미국 정부기관이 윈도우 안에 들어와도 cap 미달 + 총량 여유로 생존).
#     CNBC 10건이 정부기관 희소 시간대의 "미국발 시장 속보" 공백을 메움 = 대표 catch 직접 대응.
MAX_ITEMS = 36

# Q-167 후속 (대표 catch 2026-06-13: "한국 뉴스요약 필터링이나 중복은?") — 연합뉴스 경제 RSS는
# 시장 무관 코너·연재성 기사를 섞어 보낸다(라이브 실측: 10건 중 [이 시각 헤드라인]·[금주핫템]·
# [신상잇슈]·[부동산캘린더] 등 4건 = 잡다). 제목 [코너명] 접두 토큰 denylist 로 수집 단계에서 배제
# → wire_news.json 에 애초에 안 실림(모든 소비자 공통, SSOT). interpret_wire.py 와 무관(KR 미해석).
#
# Q-167 후속2 (대표 직접 결정 2026-06-14 "잡다한 뉴스... 빼라") — 직전 fix 가 "시장 관련성"
# 사유로 유지했던 연재/칼럼성 prefix([다음주 경제]·[바이오사이언스] 류)도 대표가 배제 지시 →
# denylist 확대. 라이브 RSS 120건 전수 grep 으로 코너/연재성 prefix만 정확 추가(아래 빈도 주석).
#
# denylist 만 정의(allowlist 아님) — 정상 시장 하드뉴스([속보]·[특징주]·[외환]·[표]·[그래픽])는
# 기본 통과. FLR-AGT-002 정합: 개별 종목·시장지표 직결 prefix 는 절대 배제 안 함(과배제로 하드뉴스
# 손실 금지). 매칭 = 제목이 정확히 "[<코너명>]" 으로 시작하는 항목만(부분 문자열 오탐 회피,
# FLR-20260409-TEC-001) → 다른 지역소식([성남소식] 등)·미수집 prefix 는 실측 시 추가.
_WIRE_CORNER_PREFIXES = frozenset(
    {
        "이 시각 헤드라인",  # 시각별 헤드라인 묶음 (개별 시장 사건 아님)
        "연합뉴스 이 시각 헤드라인",  # 라이브 3x — 동일 (정확 매칭용 별도 등재)
        "금주핫템",  # 쇼핑·신상품 코너
        "신상잇슈",  # 신제품 소개 코너
        "부동산캘린더",  # 분양·공급 일정 (개별 종목·시장지표 무관)
        "포토",  # 사진 기사
        "그래픽뉴스",  # (마켓 그래픽 [그래픽] 과 구분 — 일반 카드뉴스성)
        "카드뉴스",
        "오늘의 운세",
        "주간 날씨",
        "인사",  # 인사·동정
        "부고",
        # ── Q-167 후속2 확대분 (대표 "빼라", 라이브 RSS 120건 실측 빈도) ──
        "다음주 경제",  # 1x — 주간 경제일정 연재 칼럼 (대표 명시 배제)
        "바이오사이언스",  # 1x — 바이오 기획 연재 칼럼 (대표 명시 배제, 예: 유한양행 100년사)
        "게시판",  # 9x — 인사·협의회·출시 공지 (최다 잡다 소스, 개별 시장 사건 아님)
        "동정",  # 2x — 인물 동정 (회장 방문 등, [인사]·[부고] 동류)
        "월드컵",  # 2x — 스포츠 마케팅 기획 (식품업계 응원전 등, 시장지표 무관)
        "동포의 창",  # 2x — 재외동포 연재 칼럼
        "르포",  # 1x — 현장 르포 연재 (성수동 농심 세계관 등)
        "고양소식",  # 1x — 지자체 지역소식 (정확 매칭 — 타 지역 prefix 는 실측 시 추가)
    }
)
# 제목에서 선두 "[...]" 코너 토큰 추출 (없으면 None). 공백 정규화는 호출 전 완료 가정.
_WIRE_BRACKET_RE = re.compile(r"^\[([^\[\]]{1,20})\]")


def _is_corner_title(title):
    """제목이 시장 무관 코너/연재 접두 토큰으로 시작하면 True (denylist 정확 매칭)."""
    m = _WIRE_BRACKET_RE.match(title)
    return bool(m) and m.group(1).strip() in _WIRE_CORNER_PREFIXES


# 연합뉴스(120건/피드)가 전역 상한 30을 독식해 SEC/Fed/WH 노출 0건이 되는 것 방지.
PER_SOURCE_CAP = 10

FEEDS = [
    {
        "source": "SEC",
        "url": "https://www.sec.gov/news/pressreleases.rss",
        "assume_tz": UTC,
    },
    {
        "source": "Federal Reserve",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "assume_tz": UTC,
    },
    {
        "source": "White House",
        "url": "https://www.whitehouse.gov/briefings-statements/feed/",
        "assume_tz": UTC,
    },
    # ── 이시카와 큐레이션 GO 소스 (2026-06-15 추가, ToS·저작권 검증 통과) ──
    # CNBC US Top News — 대표 catch "미국발 뉴스 신선도 낮음" 직접 대응의 핵심 소스.
    #   정부 RSS(SEC/Fed/WH)는 발표 빈도가 낮아 48h 윈도우에서도 미국 항목이 희소했다
    #   (라이브 실측 2026-06-15: SEC 0·Fed 0·WH 1건). CNBC US Top News 는 시장 직결 속보를
    #   고빈도로 발행 → probe 실측 최신 0.1h 前 / 30건 보유 = 야간 미국증시 적시성 확보.
    #   feed shape = 표준 RSS2.0 (root rss / item / link·title·pubDate RFC822 GMT) — parse_feed
    #   기존 로직 그대로 파싱 검증 PASS (siteContentMetadata 네임스페이스 자식은 무시).
    #   ⚠ CNBC Economy(id=20910258)는 news_pipeline us_digest 기사 소스와 중복 가능 → 본 lane
    #     에는 US Top News(id=100003114)만 채택. dedup=URL 이라 동일 기사 유입돼도 1건으로 병합.
    {
        "source": "CNBC",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "assume_tz": UTC,  # pubDate RFC822 GMT 명시(naive 시 GMT 가정) — 실측 전건 tz 포함
    },
    # BEA(미 경제분석국) — GDP·무역수지·개인소득 등 1급 거시지표 1차 발표처(미 정부저작물,
    #   저작권 free). ⚠ 발표 빈도가 월/분기 단위로 극히 낮아 48h 윈도우 대부분 0건이다
    #   (probe 실측 2026-06-15: 최신 항목 108h 前 → 현재 윈도우 0건 유입). 그럼에도 추가 유지:
    #   (1) GDP/무역수지 발표일엔 시장 직결 최상위 신호 — 그날 미국 커버리지를 놓치면 안 됨,
    #   (2) 미 정부저작물이라 법무 리스크 0 + 발표빈도 낮음 = 과수집 위험 0(월 몇 건).
    #   feed shape = RSS2.0 변형(item 내 BEA 고유 태그 다수) but link·title·pubDate(RFC822 EDT)
    #   표준 위치 → parse_feed 파싱 검증 PASS.
    {
        "source": "BEA",
        "url": "https://apps.bea.gov/rss/rss.xml",
        "assume_tz": UTC,  # pubDate RFC822(EDT 등 오프셋 포함) 실측 — naive 폴백만 UTC 가정
    },
    {
        "source": "연합뉴스",
        "url": "https://www.yna.co.kr/rss/economy.xml",
        "assume_tz": KST,
    },
    {
        "source": "금융위원회",
        "url": "http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111",
        "assume_tz": KST,
    },
]

# S5 자립화 (DOC-20260707-REQ-001): 옛 homepage 절대경로 fallback → pm320 레포 루트(parents[2]).
HOMEPAGE = os.environ.get("M1S_HOMEPAGE", str(Path(__file__).resolve().parents[2]))
OUT_PATH = os.path.join(HOMEPAGE, "pm320", "data", "wire_news.json")


def fetch(url):
    # S310 suppress 사유: url은 FEEDS 상수(공식 기관 http/https)만 — 사용자 입력 경로 0.
    req = urllib.request.Request(url, headers={"User-Agent": UA})  # noqa: S310
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return resp.status, resp.read()


def parse_date(text, assume_tz):
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(KST)


def item_date_text(item):
    text = item.findtext("pubDate")
    if text:
        return text
    for child in item:
        if child.tag.lower().endswith("date") and child.text:
            return child.text
    return None


def parse_feed(raw, source, assume_tz):
    """RSS XML bytes → [{title, source, url, published_at}] (발행시각 파싱 실패 항목은 제외 — 시각 조작 금지)."""
    # S314 suppress 사유: 공식 기관 고정 feed 5종만 파싱, defusedxml 미설치 환경 — 신규 의존성 회피.
    root = ET.fromstring(raw)  # noqa: S314
    out = []
    for item in root.iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        dt = parse_date(item_date_text(item), assume_tz)
        if not title or not link.startswith("http") or dt is None:
            continue
        # Q-167 후속 — 시장 무관 코너/연재 항목 수집 제외 (denylist 정확 매칭, 위 정의 참조).
        if _is_corner_title(title):
            continue
        out.append(
            {
                "title": title,
                "source": source,
                "url": link,
                "published_at": dt.isoformat(timespec="seconds"),
            }
        )
    return out


def collect(probe=False):
    now = datetime.now(KST)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    fetched = []
    ok_feeds = 0
    for feed in FEEDS:
        label = feed["source"]
        try:
            status, raw = fetch(feed["url"])
            items = parse_feed(raw, label, feed["assume_tz"])
            ok_feeds += 1
            latest = max((i["published_at"] for i in items), default="-")
            print(
                f"[probe] {label}: HTTP={status} items={len(items)} latest_kst={latest}"
            )
            fetched.extend(items)
        except Exception as e:  # 한 feed 실패가 전체 run을 죽이면 안 됨 — 격리
            print(f"[probe] {label}: FAIL {type(e).__name__}: {e}", file=sys.stderr)

    # 트루스 소셜 트럼프 lane (법무 조건부 GO DOC-20260614-LEGAL-002) — FEEDS 와 별개 3자 API
    # 경로이나 동일 wire 항목 shape. collect_truthsocial.collect() 가 graceful (키부재/HTTP실패/
    # 예외 = 빈 리스트, 법무 §5) — 트럼프 lane 단절이 wire 전체를 죽이지 않는다(정부 RSS 무중단).
    # ok_feeds 증가는 ≥1건 수집 시만 — 0건(키부재 등)을 "성공 feed"로 세어 전 feed 실패 가드를
    # 오인하게 하지 않는다(빈 결과로도 기존 산출물 merge 정상 진행).
    ts_items = collect_truthsocial.collect()
    if ts_items:
        ok_feeds += 1
        fetched.extend(ts_items)

    if probe:
        return 0 if ok_feeds else 2

    if ok_feeds == 0:
        print("[wire] 전 feed 실패 — 기존 산출물 유지, write 생략", file=sys.stderr)
        return 2

    # 기존 산출물 merge (feed에서 밀려났지만 아직 48h 이내인 항목 보존)
    merged = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                for it in json.load(f).get("items", []):
                    merged[it["url"]] = it
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[wire] 기존 JSON 손상 — 신규 수집분만 사용: {e}", file=sys.stderr)
    for it in fetched:  # dedup=URL, 신규 fetch가 기존을 덮음
        prev = merged.get(it["url"])
        if prev is not None and prev.get("title") == it["title"]:
            # 한국어 해석 필드 carry-over (Q-20260612-154) — 멱등 유지 (무변경 시 diff 0).
            # title drift 시 비-carry → interpret_wire가 재해석.
            for k in interpret_wire.KO_FIELDS:
                if k in prev:
                    it[k] = prev[k]
        merged[it["url"]] = it

    # 윈도우(48h) + 코너 필터를 merge 산출에도 적용. 수집 단계(parse_feed) denylist 는 신규
    # 유입만 막으므로, 기존 산출물에 이미 실린 코너 항목은 carry-over 로 잔존한다(48h 까지).
    # denylist 확대(Q-167-followup2) 직후 기존 [다음주 경제]·[바이오사이언스] 등 자연 소거 위해
    # 동일 _is_corner_title 을 merge 산출에도 재적용(멱등 — 무변경 run diff 0 유지).
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    items = [
        it
        for it in merged.values()
        if it["published_at"] >= cutoff_iso and not _is_corner_title(it["title"])
    ]
    items.sort(key=lambda it: (it["published_at"], it["url"]), reverse=True)
    capped, seen = [], {}
    for it in items:
        n = seen.get(it["source"], 0)
        if n >= PER_SOURCE_CAP:
            continue
        seen[it["source"]] = n + 1
        capped.append(it)
        if len(capped) >= MAX_ITEMS:
            break

    payload = {
        "schema": "wire_news.v1",
        "window_hours": WINDOW_HOURS,
        "max_items": MAX_ITEMS,
        "items": capped,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    skip_write = False
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            if f.read() == text:
                print(f"[wire] 변경 0건 — write 생략 (items={len(capped)})")
                skip_write = True
    if not skip_write:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        tmp = OUT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, OUT_PATH)
        print(
            f"[wire] write OK → {OUT_PATH} (items={len(capped)}, feeds_ok={ok_feeds}/{len(FEEDS)})"
        )

    # ── 한국어 인과 해석 레이어 (Q-20260612-154) ──
    # write 생략 run에도 실행 (직전 run 해석 실패분 자연 retry). 예외 = 수집 블로킹 금지
    # — 실패 시 영문 원본 유지 + stderr 로그만 (run_wire.sh rc=0 → deploy 정상 진행).
    try:
        st = interpret_wire.enrich(OUT_PATH)
        print(
            f"[wire-ko] candidates={st['candidates']} cache_hit={st['cache_hit']} "
            f"interpreted={st['interpreted']} fetch_fail={st['fetch_fail']} "
            f"fail={st['fail']} deferred={st['deferred']} pruned={st['pruned']} "
            f"wire_updated={st['wire_updated']}"
        )
    except Exception as e:
        print(
            f"[wire-ko] 해석 단계 FAIL — 영문 원본 유지: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(collect(probe="--probe" in sys.argv[1:]))
