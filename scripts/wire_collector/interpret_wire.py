#!/usr/bin/env python3
"""미장 wire 뉴스 한국어 인과 해석 레이어 — Q-20260612-154 (대표 2026-06-12 23:14 직접 지시).

대상: wire_news.json 중 미국 3기관 (SEC / Federal Reserve / White House) 항목.
동작: 기사 본문 fetch (3,000자 truncate) → LLM 해석 (한국어 타이틀 + 인과 요약
      + 인과 사슬 + 영향 태그 + 방향) → wire_news_ko.v1 schema validation →
      PASS만 ko 캐시 (wire_news_ko.json) 저장 → wire_news.json 항목에 병합.

원칙:
  - 노출 운반체 = wire_news.json 단일 (R46 P0-1 "같은 데이터 2운반체" 교훈).
    wire_news_ko.json 은 중간 산출(증분 캐시)일 뿐 — frontend 비참조, 배포 비대상
    (wire_deploy.sh 화이트리스트 = wire_news.json 1파일).
  - 원문 본문은 LLM 입력만 — 산출 JSON 비저장 (collect_wire.py 저작권 정책 유지).
  - LLM 호출 = news_pipeline.llm_client.call_model_cached 경유 의무
    (`--setting-sources user` 격리 + sqlite 캐시 + persona marker 차단 기구현 재사용,
    FLR-20260511-DAT-002. 본 도메인 "wire_news_ko"는 llm_client._JSON_REQUIRED_DOMAINS
    등재 — 비-JSON/persona 응답의 캐시 오염 차단).
  - 캐시 add = validation PASS 후만 — fetch fail/LLM None/validation FAIL 시 add 0건
    → 다음 fire 자연 retry (feedback_cache_mark_success_only_semantic).
  - validation FAIL 1건이라도 = 해당 항목 캐시·저장 차단 (영문 원본 유지).
  - 해석 예외 = 수집 블로킹 금지 — collect_wire.collect() 말미 try/except 격리.

증분: ko 캐시에 없는 신규 url만 LLM 처리, title_en 불일치 시만 재해석.
prune: 현 wire_news.json에서 사라진 url은 ko 캐시 동반 제거 (collector 멱등 정합 —
       재등장 시 llm_client sqlite 캐시가 input_hash HIT → 재호출 비용 0).

본문 fetch 컨테이너 (2026-06-12 이시카와 3기관 실측 — HTTP 200·robots 허용·연방저작물):
  - White House: main.wp-block-group.site-content 내 <p>
  - Federal Reserve: div#article 내 <p>
  - SEC: div.field--name-body (Drupal) 내 <p>
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

KST = timezone(timedelta(hours=9))
# collect_wire.UA 와 동일 값 (순환 import 회피 위해 상수 중복 — 변경 시 양쪽 동기)
UA = "Mozilla/5.0 (compatible; 100m1s-wire/1.0; nicehugepark@gmail.com)"

KO_SCHEMA = "wire_news_ko.v1"
# 트루스 소셜 라벨 — collect_truthsocial.SOURCE_LABEL 과 동일 값 (순환 import 회피 상수 중복).
TRUTHSOCIAL_SOURCE = "트럼프(트루스 소셜)"
KO_SOURCES = frozenset({"SEC", "Federal Reserve", "White House", TRUTHSOCIAL_SOURCE})
# wire_news.json 항목에 병합되는 해석 필드 (collect_wire merge carry-over도 본 튜플 참조)
KO_FIELDS = (
    "ko_title",
    "causal_summary",
    "causal_chain",
    "impact_tags",
    "direction",
    "body_fetched",
)

WIRE_KO_MODEL = os.environ.get("WIRE_KO_MODEL", "haiku")
BODY_TRUNCATE = 3000
FETCH_TIMEOUT = 15
FETCH_SLEEP_SEC = 1.0
# 1 run 당 LLM 해석 상한 — launchd lock 점유 시간 bound (잔여분 다음 fire 자연 처리)
MAX_INTERPRET_PER_RUN = 6

NO_BODY_MARKER = "(본문 없음 — 타이틀만으로 해석)"

DIRECTIONS = frozenset({"호재", "악재", "중립", "불확실"})
# 프롬프트 권장 어휘 — 어휘 밖 태그는 한국어(가-힣·) 2~8자만 허용
TAG_WHITELIST = frozenset(
    {
        "금리",
        "달러",
        "국채",
        "환율",
        "반도체",
        "AI",
        "2차전지",
        "바이오",
        "방산",
        "조선",
        "에너지",
        "원자재",
        "가상자산",
        "금융규제",
        "증시제도",
        "관세·무역",
    }
)

# 이시카와 spec verbatim (Q-20260612-154) — placeholder 치환은 .replace (본문 내
# JSON 중괄호와 .format 충돌 회피). 의례성 가드 1줄은 검증 (c) 실증 후 보강분.
PROMPT_TEMPLATE = """당신은 한국 주식시장 데일리 브리핑의 미국발 뉴스 해설 작성자다. 아래 미국 공식 기관 발표 1건을 한국 투자자 관점에서 해석하라.

[입력]
- 발표 기관: {source}  (Federal Reserve=미 연준·통화정책 / SEC=미 증권거래위원회·증권규제 / White House=백악관·행정부 정책)
- 발행 시각(KST): {published_at}
- 원문 타이틀: {title}
- 본문: {body_text}

[작성 규칙]
1. ko_title: 한국어 타이틀 1줄(12~40자). 영어 직역 금지 — 기관·정책 맥락을 반영한 본질 타이틀로. 예: "SEC Proposes Rescission of Regulation NMS Rules 611 and 610(e)" → "SEC, 주식 주문보호 규정(NMS 611) 폐지 추진". 고유명사(Fed·SEC·AI·ETF 등)는 관용 표기 허용.
2. causal_summary: 정확히 2~3문장. ① 무슨 일이 있었나(사실) ② 왜 시장에 중요한가(메커니즘) ③ 어느 섹터/자산에 어떤 방향 영향(전망). 평서형 뉴스체("~했다", "~전망이다"). 마지막 문장에 인과 흐름이 드러나야 한다.
3. causal_chain: "A → B → C" 형식 1줄(각 마디 15자 이내 명사구, 2~4마디). 예: "중동 지정학 안정화 → 에너지·인프라 개발 활성화 → 건설·LNG 에너지 섹터 기회 확대"
4. impact_tags: 영향 자산군·테마 1~2개, 한국어 단어만. 권장 어휘: 금리/달러/국채/환율/반도체/AI/2차전지/바이오/방산/조선/에너지/원자재/가상자산/금융규제/증시제도/관세·무역. 어휘 밖 단어는 한국어 2~8자만 허용. 영문·숫자 혼합 조어 금지(예: "금융IT" 불가 — "금융규제" 사용). 시장 영향이 없는 의례성·기념성 발표는 빈 배열 [].
5. direction: "호재"/"악재"/"중립"/"불확실" 중 1개 — 한국 증시 관점 단일 값.
6. 모든 출력 필드는 한국어로만 작성한다. 인명·직함·프로그램명도 한국어 표기로 옮긴다(예: "First Lady Melania Trump" → "영부인 멜라니아 트럼프", "foster care" → "위탁보호"). 영문은 규정·기관 약어(Fed·SEC·FOMC·NMS·AI·ETF 등)만 허용하고, 규정명은 한국어+약어로 옮긴다(예: "Regulation NMS Rule 611" → "NMS 규정 611조").

[환각 가드 — 위반 시 출력 무효]
- 원문에 없는 수치·날짜·기업명·인명 생성 절대 금지. 수치는 원문에 명시된 것만 인용.
- 한국 개별 종목명 연결 금지 — 섹터/자산군 수준까지만.
- 본문이 "(본문 없음 — 타이틀만으로 해석)"이면 타이틀 범위 내 보수 해석만: 단정 대신 "~가능성", "~주시" 표현, direction은 "중립" 또는 "불확실"만 허용.
- 영향이 불분명하면 억지 인과를 만들지 말 것 — direction "불확실" + 보수 서술.
- 의례·기념·축전·인사 등 시장 메커니즘이 없는 발표는 억지 해석 금지 — direction "중립" 고정.
- 자가 신뢰도·확률(%) 출력 금지.

[출력 — 아래 JSON 1개만, 다른 텍스트·마크다운·코드펜스 금지]
{"ko_title": "...", "causal_summary": "...", "causal_chain": "...", "impact_tags": ["..."], "direction": "..."}"""

# 트루스 소셜 트럼프 게시물 전용 프롬프트 (법무 조건부 GO DOC-20260614-LEGAL-002 §III 직접 집행).
# 공식 기관 발표와 달리 (a) 개인 발화 → 발화 주체 명확 귀속 + (b) 미검증 주장 가능 → 왜곡·증폭 0 +
# (c) 제3자(기업·인물) 명예 발언 → 단정 헤드라인 회피. 환각 가드를 기관 프롬프트보다 강화.
PROMPT_TEMPLATE_TRUTHSOCIAL = """당신은 한국 주식시장 데일리 브리핑의 미국발 뉴스 해설 작성자다. 아래는 도널드 트럼프 미국 대통령이 본인 소셜미디어(트루스 소셜)에 직접 올린 게시물 1건이다. 이를 한국 투자자 관점에서 해석하라.

[입력]
- 발화 주체: 도널드 트럼프 (본인 트루스 소셜 게시물 — 공식 기관 발표가 아닌 개인 발언)
- 발행 시각(KST): {published_at}
- 게시물 발췌: {body_text}

[작성 규칙]
1. ko_title: 한국어 타이틀 1줄(12~40자). 반드시 발화 주체를 "트럼프"로 명시하고 인용 형태로. 예: "트럼프, 연준에 금리 인하 압박 발언". 영어 직역 금지. 고유명사(Fed·SEC·AI·ETF 등)는 관용 표기 허용.
2. causal_summary: 정확히 2~3문장. ① 트럼프가 무엇을 말했나(발언 사실 — "트럼프는 ~라고 밝혔다/주장했다") ② 왜 시장에 중요한가(메커니즘) ③ 어느 섹터/자산에 어떤 방향 영향 가능성(전망). 발언임을 분명히 하는 평서형("~라고 말했다", "~전망이다"). wire 자신의 주장이 아니라 트럼프의 발언을 인용·논평하는 형식.
3. causal_chain: "A → B → C" 형식 1줄(각 마디 15자 이내 명사구, 2~4마디). 예: "트럼프 관세 위협 → 무역 긴장 고조 → 수출주·환율 변동성 확대"
4. impact_tags: 영향 자산군·테마 1~2개, 한국어 단어만. 권장 어휘: 금리/달러/국채/환율/반도체/AI/2차전지/바이오/방산/조선/에너지/원자재/가상자산/금융규제/증시제도/관세·무역. 어휘 밖 단어는 한국어 2~8자만 허용. 영문·숫자 혼합 조어 금지. 시장 영향이 없는 일상·정치 수사·인사성 게시물은 빈 배열 [].
5. direction: "호재"/"악재"/"중립"/"불확실" 중 1개 — 한국 증시 관점 단일 값.
6. 모든 출력 필드는 한국어로만 작성한다. 인명·직함도 한국어 표기로 옮긴다. 영문은 약어(Fed·SEC·AI·ETF 등)만 허용.

[환각·법적 가드 — 위반 시 출력 무효]
- 게시물 발췌에 없는 수치·날짜·기업명·인명·정책 생성 절대 금지. 트럼프가 실제로 말하지 않은 내용을 만들지 말 것.
- 발언 취지를 비틀거나 과장하지 말 것 — 발췌 내용을 그대로 전달(왜곡 0). 짧은 발췌라 맥락이 불완전하면 단정 금지.
- 트럼프 발언이 특정 기업·인물을 비난·언급하더라도, 그 주장을 사실인 것처럼 단정하는 헤드라인 금지 — "트럼프가 ~라고 주장" 형태로만 귀속하고, 한국 개별 종목명 연결 금지(섹터/자산군 수준까지만).
- 정치적 수사·일상 게시물 등 시장 메커니즘이 불분명하면 억지 인과 금지 — direction "중립" 또는 "불확실" + 보수 서술("~가능성", "~주시").
- 자가 신뢰도·확률(%) 출력 금지.

[출력 — 아래 JSON 1개만, 다른 텍스트·마크다운·코드펜스 금지]
{"ko_title": "...", "causal_summary": "...", "causal_chain": "...", "impact_tags": ["..."], "direction": "..."}"""

# HTML void 요소 — 종료 태그가 없어 depth 추적에서 제외 (미제외 시 depth 누수로 과수집)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


def _is_container(source, tag, attrs):
    """기관별 본문 컨테이너 판정 (이시카와 실측 selector)."""
    classes = (attrs.get("class") or "").split()
    if source == "White House":
        return (
            tag == "main" and "wp-block-group" in classes and "site-content" in classes
        )
    if source == "Federal Reserve":
        return tag == "div" and attrs.get("id") == "article"
    if source == "SEC":
        return tag == "div" and "field--name-body" in classes
    return False


class _ContainerTextParser(HTMLParser):
    """지정 컨테이너 내부 <p> 텍스트만 추출 (stdlib-only — bs4 신규 의존성 회피)."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self._source = source
        self._depth = 0  # 컨테이너 내부 중첩 깊이 (0 = 컨테이너 외부)
        self._in_p = False
        self._buf = []
        self.paragraphs = []

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            return
        if self._depth:
            self._depth += 1
            if tag == "p":
                self._in_p = True
        elif _is_container(self._source, tag, dict(attrs)):
            self._depth = 1

    def handle_endtag(self, tag):
        if not self._depth or tag in _VOID_TAGS:
            return
        if tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join("".join(self._buf).split())
            if text:
                self.paragraphs.append(text)
            self._buf = []
        self._depth -= 1
        if not self._depth:
            self._in_p = False

    def handle_data(self, data):
        if self._depth and self._in_p:
            self._buf.append(data)


def fetch_body(source, url):
    """기사 본문 추출 → (text, fetched). 실패(비200/timeout/추출 0자) 시 ("", False)."""
    try:
        # S310 suppress 사유: url은 공식 기관 RSS(FEEDS 상수 유래) 직링크만 — 사용자 입력 0.
        req = urllib.request.Request(url, headers={"User-Agent": UA})  # noqa: S310
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310
            if resp.status != 200:
                print(
                    f"[wire-ko] body fetch HTTP {resp.status}: {url}", file=sys.stderr
                )
                return "", False
            raw = resp.read().decode("utf-8", errors="replace")
    except (
        Exception
    ) as e:  # 폴백 = 타이틀만 보수 해석 — fetch 실패가 해석 자체를 막지 않음
        print(
            f"[wire-ko] body fetch FAIL {type(e).__name__}: {e} ({url})",
            file=sys.stderr,
        )
        return "", False
    parser = _ContainerTextParser(source)
    try:
        parser.feed(raw)
        parser.close()
    except Exception as e:
        print(
            f"[wire-ko] HTML parse FAIL {type(e).__name__}: {e} ({url})",
            file=sys.stderr,
        )
        return "", False
    text = "\n".join(parser.paragraphs).strip()
    if not text:
        return "", False
    return text[:BODY_TRUNCATE], True


def _parse_llm_json(response):
    """LLM 응답 → dict | None (JSON 1개 의무 — 관용 파싱은 외곽 중괄호 추출 1단계만)."""
    s = response.strip()
    try:
        obj = json.loads(s)
    except ValueError:
        a, b = s.find("{"), s.rfind("}")
        if a == -1 or b <= a:
            return None
        try:
            obj = json.loads(s[a : b + 1])
        except ValueError:
            return None
    return obj if isinstance(obj, dict) else None


# causal_summary 길이 — 단문 발표(WH 단신 등)는 1문장으로 충분히 해석되므로 하한을
# 60→25자로 완화(과거 하한이 단문 정당 항목을 drop → "미국발 신선도 낮음" 일조).
# 상한은 노이즈/장황 방지로 유지. 환각 방지(억지 2문장 강요)와 누락 방지의 균형점.
_SUMMARY_MIN = 25
_SUMMARY_MAX = 300


def validate_ko(payload, body_fetched):
    """wire_news_ko.v1 schema 검증 → (fatal_errs, soft_fields).

    완화(graceful degrade) — 2등급 분리:
      - fatal_errs (치명): 비어있으면 항목 자체를 ko 미부여 처리(영문 원본 유지).
        대상 = ko_title(있음+한글 우세) + direction(enum + 환각 보수 가드).
        이 둘이 카드의 최소 한국어 정체성·영향 단정 안전선이라 hard gate 유지.
      - soft_fields (보강): 위반한 *그 필드만* 비우고 항목은 살린다(drop 대신).
        대상 = causal_summary / causal_chain / impact_tags.
        → 단문·'→' 없는 항목도 ko_title+direction만 유효하면 한국어 카드로 노출,
          causal 정보가 유효하면 그대로 보존(누락 0). 무효 필드만 graceful degrade.

    🔴 과완화 노이즈 방지(근거): direction 보수 강제(본문 없음/태그 0개 → 중립·불확실)는
    fatal 로 유지 — 영향 "단정"을 막는 환각 가드라 완화하면 호재/악재 오노출 위험. 무효
    태그·장황 summary 는 노출 안 되게 비우되(soft), 항목 골격(제목·방향)은 살린다.
    """
    fatal = []
    soft = []
    if not isinstance(payload, dict):
        return (["payload not dict"], soft)

    # ── 치명: ko_title (한국어 카드 정체성) ──
    ko_title = payload.get("ko_title")
    if not isinstance(ko_title, str) or not (12 <= len(ko_title.strip()) <= 40):
        fatal.append(f"ko_title 길이 위반 (12~40자): {ko_title!r}")
    else:
        # 한글 우세 판정 — 글자(한글 vs 라틴)만 비교. 숫자·괄호·중점은 언어 중립이라
        # 분모 제외 (실측: spec 예시 동형 "SEC, 주문보호 규정(NMS 611·610e) 폐지 추진"이
        # 전체 문자 기준 50%에 미달 — 규정 번호가 분모를 독식).
        t = ko_title.strip()
        hangul = len(re.findall(r"[가-힣]", t))
        latin = len(re.findall(r"[A-Za-z]", t))
        if hangul < 4 or hangul < latin:
            fatal.append(
                f"ko_title 한글 우세 위반 (한글 {hangul} vs 라틴 {latin}): {t!r}"
            )

    # ── 치명: direction (enum + 환각 보수 가드) ──
    direction = payload.get("direction")
    tags = payload.get("impact_tags")
    if direction not in DIRECTIONS:
        fatal.append(f"direction enum 위반: {direction!r}")
    elif direction not in ("중립", "불확실"):
        # 영향 "단정"(호재/악재)의 안전선 — 본문 근거 부재 or 태그 0개면 단정 금지.
        if not body_fetched:
            fatal.append(
                f"본문 없음 보수 해석 위반 — direction {direction!r} (중립/불확실만 허용)"
            )
        if isinstance(tags, list) and len(tags) == 0:
            fatal.append(
                f"태그 0개인데 direction {direction!r} — 영향 단정 모순 (중립/불확실만 허용)"
            )

    # ── 보강(soft): causal_summary — 무효 시 필드만 비움 ──
    if not isinstance(summary := payload.get("causal_summary"), str):
        soft.append(f"causal_summary 타입 위반: {type(summary).__name__}")
    else:
        s = summary.strip()
        if not (_SUMMARY_MIN <= len(s) <= _SUMMARY_MAX):
            soft.append(
                f"causal_summary 길이 위반 ({_SUMMARY_MIN}~{_SUMMARY_MAX}자): {len(s)}자"
            )
        # 문장 수는 완화 — 1~3문장 허용(단문 단신 1문장 정직 응답 허용). 4문장+ 만 장황으로 차단.
        elif len(re.findall(r"[.!?](?=\s|$)", s)) > 3:
            soft.append("causal_summary 문장 수 위반 (1~3 권장, 4문장+ 장황)")

    # ── 보강(soft): causal_chain — None/누락/'→' 부재 모두 허용(비움), 있으면 형식 검증 ──
    chain = payload.get("causal_chain")
    if chain is not None and (not isinstance(chain, str) or "→" not in chain):
        soft.append(f"causal_chain '→' 누락: {chain!r}")

    # ── 보강(soft): impact_tags — 무효 시 빈 배열로 ──
    # 0~2개 — 빈 배열은 시장 메커니즘 없는 의례성 발표의 정직한 응답 (실측: 가톨릭 축전·
    # 영부인 저축계좌 건 모델 [] 반환). 억지 태그 강요 = 환각 가드 "억지 인과 금지" 모순.
    if not isinstance(tags, list) or len(tags) > 2:
        soft.append(f"impact_tags 개수 위반 (0~2): {tags!r}")
    else:
        for tag in tags:
            if not isinstance(tag, str) or (
                tag not in TAG_WHITELIST and not re.fullmatch(r"[가-힣·]{2,8}", tag)
            ):
                soft.append(
                    f"impact_tags 어휘 위반 (whitelist 또는 한국어 2~8자): {tag!r}"
                )
                break

    return (fatal, soft)


def _llm():
    """news_pipeline.llm_client lazy import (collect_wire 수집 경로와 의존 격리)."""
    scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from news_pipeline import llm_client

    return llm_client


def interpret_one(item, body_text, body_fetched):
    """1건 해석 → validation PASS 캐시 entry dict | None (FAIL/호출 실패 시 None).

    validation FAIL 시 ignore_cache=True 1회 재시도 — 불량 응답이 llm sqlite 캐시에
    고착되어 같은 항목이 영구 FAIL loop에 빠지는 것 방지 (신규 샘플로 캐시 덮어씀).
    """
    llm = _llm()
    if item["source"] == TRUTHSOCIAL_SOURCE:
        # 트럼프 게시물 전용 프롬프트 (법무 §3 발화 주체 귀속·왜곡 0). 발췌가 본문(=title)이므로
        # title/source placeholder 없음 — body_text(발췌) 단일 치환.
        prompt = PROMPT_TEMPLATE_TRUTHSOCIAL.replace(
            "{published_at}", item["published_at"]
        ).replace("{body_text}", body_text if body_fetched else NO_BODY_MARKER)
    else:
        prompt = (
            PROMPT_TEMPLATE.replace("{source}", item["source"])
            .replace("{published_at}", item["published_at"])
            .replace("{title}", item["title"])
            .replace("{body_text}", body_text if body_fetched else NO_BODY_MARKER)
        )
    payload = None
    for ignore_cache in (False, True):
        response = llm.call_model_cached(
            prompt,
            WIRE_KO_MODEL,
            domain="wire_news_ko",
            target_id=item["url"],
            input_hash=llm.hash_input(prompt),
            agent="wire-ko",
            timeout=60,
            max_retries=1,
            ignore_cache=ignore_cache,
        )
        if response is None:
            print(f"[wire-ko] LLM 호출 실패: {item['url']}", file=sys.stderr)
            return None
        # persona 응답 차단 (FLR-20260511-DAT-002) — 캐시 layer 차단과 별개 출력 layer 가드
        head = response[:300]
        persona = next(
            (m for m in getattr(llm, "_PERSONA_MARKERS", ()) if m in head), None
        )
        if persona is not None:
            print(
                f"[wire-ko] persona marker 차단 {persona!r}: {item['url']}",
                file=sys.stderr,
            )
            continue  # 신규 샘플 재시도
        payload = _parse_llm_json(response)
        if payload is None:
            print(f"[wire-ko] JSON parse 실패: {item['url']}", file=sys.stderr)
            continue
        fatal, soft = validate_ko(payload, body_fetched)
        if not fatal:
            # 치명 통과 → 채택. soft 위반 필드만 비우고(graceful degrade) 항목은 살린다.
            if soft:
                print(
                    f"[wire-ko] soft degrade {item['url']}: {'; '.join(soft)}",
                    file=sys.stderr,
                )
            break
        # 치명 FAIL → ignore_cache 재시도 1회 (불량 응답 캐시 고착 방지). 둘 다 fatal 이면 None.
        print(
            f"[wire-ko] validation FATAL (retry={ignore_cache}) {item['url']}: "
            f"{'; '.join(fatal)}",
            file=sys.stderr,
        )
        payload = None
    if payload is None:
        return None
    # soft 위반 필드는 비워서 노이즈 노출 차단(치명 아님 — 항목은 ko_title+direction 으로 생존).
    soft_blanked = {s.split()[0] for s in soft}
    summary = payload.get("causal_summary")
    chain = payload.get("causal_chain")
    tags = payload.get("impact_tags")
    return {
        "url": item["url"],
        "title_en": item["title"],
        "ko_title": payload["ko_title"].strip(),
        "causal_summary": (
            "" if "causal_summary" in soft_blanked else (summary or "").strip()
        ),
        # '→' 없거나 None/누락 → 빈 문자열(carry-over·KeyError 방지). 유효 시만 보존.
        "causal_chain": "" if "causal_chain" in soft_blanked else (chain or ""),
        "impact_tags": [] if "impact_tags" in soft_blanked else (tags or []),
        "direction": payload["direction"],
        "body_fetched": body_fetched,
        "model": WIRE_KO_MODEL,
        "interpreted_at": datetime.now(KST).isoformat(timespec="seconds"),
    }


def _atomic_write_json(path, payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def enrich(out_path):
    """wire_news.json 미국 3기관 항목에 한국어 해석 병합 (단일 운반체). 통계 dict 반환."""
    stats = {
        "candidates": 0,
        "cache_hit": 0,
        "interpreted": 0,
        "fetch_fail": 0,
        "fail": 0,
        "deferred": 0,
        "pruned": 0,
        "wire_updated": False,
    }
    if not os.path.exists(out_path):
        return stats
    with open(out_path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    us_items = [it for it in items if it.get("source") in KO_SOURCES]

    ko_path = os.path.join(os.path.dirname(out_path), "wire_news_ko.json")
    cache_items = {}
    if os.path.exists(ko_path):
        try:
            with open(ko_path, encoding="utf-8") as f:
                cache_items = json.load(f).get("items", {})
        except (json.JSONDecodeError, AttributeError) as e:
            # 캐시 손상 → 빈 캐시로 재구축 (llm sqlite 캐시 HIT → 재해석 비용 0)
            print(f"[wire-ko] ko 캐시 손상 — 재구축: {e}", file=sys.stderr)
            cache_items = {}
    cache_changed = False

    # 1) prune — 현 wire_news.json(48h 윈도우·cap 기적용)에 없는 url 동반 제거 (멱등 정합)
    live_urls = {it["url"] for it in us_items}
    for url in list(cache_items):
        if url not in live_urls:
            del cache_items[url]
            stats["pruned"] += 1
            cache_changed = True

    # 2) 증분 해석 — 신규 url + title_en drift 만. add는 validation PASS 후만.
    interpret_count = 0
    for it in us_items:
        stats["candidates"] += 1
        entry = cache_items.get(it["url"])
        if entry and entry.get("title_en") == it["title"]:
            stats["cache_hit"] += 1
            continue
        if interpret_count >= MAX_INTERPRET_PER_RUN:
            stats["deferred"] += 1
            continue
        interpret_count += 1
        if it["source"] == TRUTHSOCIAL_SOURCE:
            # 트루스 소셜: 게시물 발췌가 이미 it["title"](collect_truthsocial 발췌 ≤500자)에 담겨
            # 있다 — 별도 HTML fetch 불가(기관 selector 없음)·법무 §1 전문 재요청 불요. 발췌를 본문으로
            # 직접 사용해 보수 해석(중립 강제)에 갇히지 않게 한다(트럼프 항목 전량 FAIL→hide 문제 해소).
            # body_fetched=True 라도 interpret 프롬프트가 "트럼프 발언" 귀속·왜곡 0 가드(법무 §3) 적용.
            body_text, body_fetched = it["title"], True
        else:
            body_text, body_fetched = fetch_body(it["source"], it["url"])
            time.sleep(FETCH_SLEEP_SEC)  # 기관 서버 요청 간격 (이시카와 spec 1s)
        if not body_fetched:
            stats["fetch_fail"] += 1  # 폴백: 타이틀만 보수 해석으로 계속 진행
        new_entry = interpret_one(it, body_text, body_fetched)
        if new_entry is None:
            stats["fail"] += 1  # 캐시 add 0건 = 다음 fire 자연 retry
            continue
        cache_items[it["url"]] = new_entry
        cache_changed = True
        stats["interpreted"] += 1

    # 3) ko 캐시 저장 (변경 시만 — generated_at volatile churn 방지)
    if cache_changed:
        _atomic_write_json(
            ko_path,
            {
                "schema": KO_SCHEMA,
                "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
                "items": cache_items,
            },
        )

    # 4) wire_news.json 병합 — 해석 PASS 항목에 KO_FIELDS 부여, 미해석/drift는 잔존 제거
    changed = False
    for it in us_items:
        entry = cache_items.get(it["url"])
        if entry and entry.get("title_en") == it["title"]:
            for k in KO_FIELDS:
                if it.get(k) != entry[k]:
                    it[k] = entry[k]
                    changed = True
        else:
            for k in KO_FIELDS:
                if k in it:
                    del it[k]  # 재해석 실패 시 옛 해석 잔존 노출 금지
                    changed = True
    if changed:
        # collect_wire 산출과 동일 직렬화 (다음 run의 무변경 diff-skip 정합)
        _atomic_write_json(out_path, data)
        stats["wire_updated"] = True
    return stats


if __name__ == "__main__":
    homepage = os.environ.get(
        "M1S_HOMEPAGE", "/Users/seongjinpark/company/100m1s-homepage"
    )
    result = enrich(os.path.join(homepage, "pm320", "data", "wire_news.json"))
    print(f"[wire-ko] {json.dumps(result, ensure_ascii=False)}")
    sys.exit(0)
