"""
종목코드별 OG 이미지 + 딥링크 HTML 생성기.

외부 공유(카톡/트위터) 시 종목 카드 썸네일 + 종목명 노출을 위해
`/pm320/{date}/{code}.html` (메타 + JS redirect) 와
`/og/pm320/{date}/{code}.png` (1200x630) 생성. (Q-20260606-119 stock 세그먼트 제거)

뉴스 게시판 구조 규칙:
  뉴스 → [주식/경매/정책] → 날짜 → [종목/사건/정책ID]

사용:
  python -m scripts.news_pipeline.generate_stock_og            # 오늘 date의 화면 카드 universe
  python -m scripts.news_pipeline.generate_stock_og 2026-04-22 # 특정 날짜

출력:
  ~/company/100m1s-homepage/og/pm320/{date}/{code}.png   (1200x630, Pillow 템플릿)
  ~/company/100m1s-homepage/pm320/{date}/{code}.html     (OG 메타 + JS redirect)

설계 결정 (FLR-20260408-TEC-001 외부 API 사전 검증, 유지보수 부담 고려):
  - Playwright 라이브 DOM 캡처 대신 Pillow 템플릿 방식 채택
  - 사유: (1) cron 환경 headless 브라우저 경로·로컬 서버 의존성 제거
          (2) 종목 40개 × Playwright ~3초 = 120초 vs Pillow ~0.3초 = 12초
          (3) `generate_og.py` 코드 재사용으로 디자인 일관성 + 코드 복잡도 감소
  - 카드 DOM 캡처가 정말 필요하면 후속 REQ에서 별도 병렬 pipeline 추가

2026-05-27 최종 스펙 (대표 결정, 카톡 미리보기 개선 — 누적 변경 통합):
  레이아웃 (1200x630, 라이트모드) 위→아래:
    [상단] 종목명(한글, 큰 타이포) + 가격(종가) + 등락률(색). code·날짜·회사명 미노출.
    [하단] 일봉캔들(좌, 20영업일 dailybars) | 당일 분봉 sparkline(우) 나란히.
    [푸터] 날짜(좌측정렬). 회사명/도메인 없음.
  - 캔들/등락률 색: 양봉 #C53939 / 음봉 #1958C7 / 동가 #94A3B8 (mini-candle.js L32 정합).
  - sparkline: 카드와 동일 소스 (/data/interpreted/stock-{date}.json → intraday.prices,
    sparkline.js + renderer.js:581 방향색 = open 대비 현재가). 데이터 없으면 graceful 생략.
  - 라이트모드 (다크모드 제거 정책 정합): 배경 #FAFBFE→#F2F4F8, 텍스트 다크.
    홈페이지 news.css :root 팔레트 verbatim (--bg/--bg2/--bd/--tx/--tx2, 추측 0).
    secondary 텍스트는 --dm(#8B95A8, AA 미달) 대신 --tx2(#3D4351, BG 9.57 AA+) 채택.
  - og:title = 종목명만 (code·"· 100M1S" 접미사 제거). URL/landing 경로는 code 유지.
  - redirect target: `?stock={code}&date={date}` query (Phase 2c-1 single-card 정합,
    renderer.js L1574~1586). 공유버튼이 본 landing URL 복사 → 카톡 OG 미리보기 노출.
  - 거래대금 순위/금액 OG 미노출 (검색식 선정 노출 → 공개 OG 제외).
  - 회사명 헤더/풋터/도메인 "일단" 임시 제거 (SHOW_BRANDING 플래그, 복원 1줄).
  - 일봉/sparkline 각각 독립 graceful 생략.

2026-05-28 OG 대상 정합 (대표 5/28 13:38 KST "당연하지"):
  종래 OG 대상 = daily_picks(영웅식 SC 전체) → 화면 카드보다 종목 과다 (화면에
  안 뜨는 종목까지 OG 생성 = SSOT 불일치). 신규 = 화면 노출 카드 universe
  (/data/interpreted/stock-{date}.json stocks[]) 만 순회. build_daily 가
  latest_stocks/daily_top base ∪ 상한가 union 을 stocks[] 에 박제하므로
  (build_daily.py:1881 + L2946~3025), renderer.js:279/305 가 읽는 카드 set 과 1:1.
  → OG PNG = landing HTML = 공유버튼 target 모두 화면 카드와 동일 universe.
  구 일자(interpreted JSON 부재)는 daily_picks 폴백으로 복원 (_carded_codes_from_daily_picks).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ── 경로 설정 ──────────────────────────────────────────────
# HOMEPAGE_DIR SoT — config.py 의 HOMEPAGE (env M1S_HOMEPAGE override 가능).
# cron worktree 격리 (lead-meta §11.32) 정합 — M1S_HOMEPAGE=~/company/100m1s-homepage-cron 환경에서 cron pipeline 실행 시 자동 cron worktree 대상.
from .config import HOMEPAGE as HOMEPAGE_DIR

SCRIPT_DIR = Path(__file__).resolve().parent
FONT_DIR = SCRIPT_DIR / "fonts"
# Q-20260606 — PM320 워드마크 (코워크 제작, wordmark_light.png 번들). OG 카드 푸터 우측 코너 합성.
#   라이트 카드 배경 정합(#1a1d26 텍스트 + #96741f 골드). 부재 시 graceful skip(로고 없이 생성).
ASSETS_DIR = SCRIPT_DIR / "assets"
PM320_WORDMARK_PATH = ASSETS_DIR / "pm320_wordmark_light.png"
DB_PATH = HOMEPAGE_DIR / "data" / "stocks.db"
# 당일 분봉 sparkline 소스 = 카드와 동일 (data-loader.js:52 / renderer.js:969-970):
#   /data/interpreted/stock-{date}.json → stocks[].intraday {open, base, prices}.
INTERPRETED_DIR = HOMEPAGE_DIR / "data" / "interpreted"
# 뉴스 게시판 구조: 뉴스 → [주식/경매/정책] → 날짜 → [종목/사건/정책ID]
# Q-20260605-105 (대표 2026-06-05 21:52) — 상세 URL /news/stock → /pm320/stock 이전.
#   신규 생성분은 pm320 체계로만 출력 (대표 21:55 "양방향 불필요" — 옛 경로 stub 동시 생성 안 함).
#   기존 1320장은 1회성 migrate_stock_url.py 로 옛 경로 stub 치환 완료.
# Q-20260606-119 (대표 2026-06-06 09:56) — "pm320 자체가 주식" → URL 에서 stock 세그먼트 제거.
#   /pm320/stock/{date}/{code}.html → /pm320/{date}/{code}.html (단방향, 구경로 stub 불요 — 대표
#   "이미 공유된 링크는 신경쓰지마"). OG PNG 도 동형 정렬 og/pm320/stock → og/pm320.
# PNG: og/pm320/{date}/{code}.png
# HTML: pm320/{date}/{code}.html
OG_BASE_DIR = HOMEPAGE_DIR / "og" / "pm320"
STOCK_HTML_BASE_DIR = HOMEPAGE_DIR / "pm320"

# ── 색상 — 라이트모드 (2026-05-27 대표 catch, 다크모드 제거 정책 정합) ──
#   홈페이지 news.css :root 팔레트 verbatim (추측 색 0건):
#     --bg #FAFBFE / --bg2 #F2F4F8 / --sf #FFFFFF / --bd #E8ECF2
#     --tx #1A1D26 (primary) / --tx2 #3D4351 / --dm #8B95A8 (muted) / --am2 #E8C063 (gold)
BG = (0xFA, 0xFB, 0xFE)  # --bg 캔버스 배경
BG2 = (0xF2, 0xF4, 0xF8)  # --bg2 (차트 패널 배경 = PANEL)
CARD_FILL = (0xFF, 0xFF, 0xFF)  # --sf 카드면 (흰색)
BORDER = (0xE8, 0xEC, 0xF2)  # --bd 구분선/카드 테두리
TX = (0x1A, 0x1D, 0x26)  # --tx 종목명/종가 primary (BG 대비 16.27 AAA)
# code/날짜/업종/패널라벨 = --tx2 #3D4351 채택 (BG 9.57 / BG2 9.00 AA+).
#   주의: 홈페이지 --dm #8B95A8 은 BG 대비 2.92 < AA 3.0 → 대표 "대비 AA 이상" 지시 위반 →
#   secondary 텍스트는 --dm 대신 --tx2 verbatim 토큰 사용 (추측 색 0건).
MUTED = (0x3D, 0x43, 0x51)  # --tx2 secondary 텍스트 (날짜)
GOLD = (0xE8, 0xC0, 0x63)  # --am2 (브랜드 헤더 — SHOW_BRANDING 시만)
# 캔들/등락률 — js/lib/mini-candle.js L32 정합 (한국 증시 관습), 라이트 배경 대비 OK.
#   양봉(close>open) #C53939, 음봉 #1958C7, 동가 #94A3B8.
CANDLE_UP = (0xC5, 0x39, 0x39)  # 양봉/상승
CANDLE_DOWN = (0x19, 0x58, 0xC7)  # 음봉/하락
CANDLE_FLAT = (0x94, 0xA3, 0xB8)  # 동가
PANEL_BG = BG2  # 하단 차트 패널 배경 (PANEL #F2F4F8, 흰 카드면과 대비) — 불변
# 캔버스 배경 그라데이션 (2026-05-27 대표 D 밋밋함 해소 B — 배경만 진하게 → 순백 카드
#   부유감). 카드면(CARD_FILL #FFFFFF)·패널(PANEL_BG #F2F4F8)은 불변, 캔버스만 진하게.
CANVAS_TOP = (0xEE, 0xF1, 0xF6)  # 상단 #EEF1F6 (구 --bg #FAFBFE 보다 진함)
CANVAS_BOTTOM = (0xE4, 0xE9, 0xF1)  # 하단 #E4E9F1 (구 --bg2 #F2F4F8 보다 진함)
# 등락률 pill 칩 배경 — news.css verbatim. 한국 증시 관습(상승=빨강) 매핑:
#   상승칩 = #FFF0F0 (붉은 톤, news.css L143 .section-title.up background) /
#   하락칩 = #EAF1FF (연파랑, news.css L144 .section-title.down background 2026-05-27 대표
#            catch — 기존 --pos-bg 녹색 #E8F5EC → 하락=파랑 톤으로 정정).
CHIP_UP_BG = (0xFF, 0xF0, 0xF0)  # 상승칩 배경 (news.css L143)
CHIP_DOWN_BG = (0xEA, 0xF1, 0xFF)  # 하락칩 배경 (news.css L144 #EAF1FF 연파랑)
CHIP_FLAT_BG = (0xF2, 0xF4, 0xF8)  # 동가칩 배경 (--bg2)
# 카드 그림자 — SHADOW rgb(26,29,38), alpha 0.10 (2026-05-27 대표 catch — 0.06 너무
#   옅어 썸네일 미인지 → 0.10 상향). 알파 합성 후 GaussianBlur.
SHADOW_RGBA = (0x1A, 0x1D, 0x26, 0x1A)  # 알파 0x1A=26/255≈0.102 ≈ 0.10

W, H = 1200, 630

# 회사명/도메인 노출 토글 (2026-05-27 대표 지시 "일단" = 임시 제거).
#   False → 좌상단 "100M1S" 헤더 / 하단 "100M1S News · 100m1s.com" 풋터 /
#           og:title "· 100M1S" 접미사 0건 노출. 복원 시 True 1줄만 변경.
SHOW_BRANDING = False

# ── 레이아웃 좌표 (2026-05-27 디자인팀 design-og-polish 스펙 verbatim) ──────
#   애플/토스 폴리시: 흰 카드 컨테이너 + 그림자 → 작은 썸네일에서도 카드로 인지.
#   카드 내부: [상단 종목명·가격·등락칩] / [차트 2패널 일봉|분봉] / [푸터 날짜].
# 흰색 카드: x 60→1140 (w1080), y 56→574 (h518), radius 28, border 1px BORDER.
CARD_X0, CARD_Y0, CARD_X1, CARD_Y1 = 60, 56, 1140, 574
CARD_RADIUS = 28
CARD_PAD = 48  # 카드 내부 padding
# 카드 그림자 offset +6~8 / blur ~24 (작은 썸네일에서도 카드로 인지되게 과감히).
SHADOW_OFFSET = 7
SHADOW_BLUR = 24
# 카드 내부 좌우 경계 (콘텐츠 정렬 기준).
CONTENT_X0 = CARD_X0 + CARD_PAD  # 108
CONTENT_X1 = CARD_X1 - CARD_PAD  # 1092
# 상단 행 baseline y≈104: 종목명(좌) ···· 가격 + 등락칩(우).
TOP_Y = 104
# 차트 2패널: 각 w=474 (카드 내부 984, gap 36 → (984-36)/2=474).
#   좌 일봉 x=108→582 / 우 분봉 x=618→1092.
#   세로 확장 (2026-05-27 대표): y0=210, y1=500 (날짜 y520 직전까지) → h290.
#   차트↔날짜 빈 여백 축소. 폭 불변, 세로만 확장.
CHART_GAP = 36
CHART_Y0 = 210
CHART_Y1 = 500
CHART_H = CHART_Y1 - CHART_Y0  # 290
_chart_w = (CONTENT_X1 - CONTENT_X0 - CHART_GAP) // 2  # (984-36)/2 = 474
CANDLE_X0 = CONTENT_X0  # 108
CANDLE_X1 = CANDLE_X0 + _chart_w  # 582
SPARK_X0 = CANDLE_X1 + CHART_GAP  # 618
SPARK_X1 = SPARK_X0 + _chart_w  # 1092
PANEL_RADIUS = 20  # 차트 패널 radius
PANEL_PAD = 18  # 패널 내부 pad
# 푸터 날짜: y=520, 좌 x=108.
FOOTER_Y = 520


# 한글 폰트 fallback 체인 (2026-05-27 — 번들 NotoSansKR-Bold.ttf == Regular 동일 파일
#   (MD5 동일, weight 'Thin') = 가짜 bold 발견 → 실제 bold weight 폰트 필요).
#   AppleSDGothicNeo.ttc (macOS 시스템, cron 호스트 항상 존재): index 6 = 진짜 Bold,
#   index 0 = Regular. (face index 검증: python3 ImageFont.truetype index 0~11 열거 PASS.)
_APPLE_GOTHIC_TTC = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
_BOLD_CANDIDATES: list[tuple[Path, int]] = [
    (_APPLE_GOTHIC_TTC, 6),  # Apple SD Gothic Neo / Bold (진짜 굵은 weight)
    (FONT_DIR / "NotoSansKR-Bold.ttf", 0),  # 번들 (현재 가짜지만 교체 시 자동 우선)
]
_REGULAR_CANDIDATES: list[tuple[Path, int]] = [
    (FONT_DIR / "NotoSansKR-Regular.ttf", 0),
    (_APPLE_GOTHIC_TTC, 0),  # Apple SD Gothic Neo / Regular
]


def _resolve_font(
    candidates: list[tuple[Path, int]], size: int
) -> ImageFont.FreeTypeFont:
    for path, index in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size, index=index)
            except OSError:
                continue
    raise FileNotFoundError(
        f"폰트 없음 — 후보 전부 실패: {[str(p) for p, _ in candidates]}"
    )


def _bold_font(size: int) -> ImageFont.FreeTypeFont:
    """실제 bold weight 한글 폰트 (AppleSDGothicNeo Bold 우선)."""
    return _resolve_font(_BOLD_CANDIDATES, size)


def _regular_font(size: int) -> ImageFont.FreeTypeFont:
    return _resolve_font(_REGULAR_CANDIDATES, size)


def _gradient_bg() -> Image.Image:
    """캔버스 배경 — 상단 CANVAS_TOP(#EEF1F6) → 하단 CANVAS_BOTTOM(#E4E9F1) 그라데이션.

    배경을 카드면(#FFFFFF)보다 진하게 하여 순백 카드가 부유하는 명도 분리 (B 해소안).
    """
    img = Image.new("RGB", (W, H), CANVAS_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        ratio = y / H
        r = int(CANVAS_TOP[0] + (CANVAS_BOTTOM[0] - CANVAS_TOP[0]) * ratio)
        g = int(CANVAS_TOP[1] + (CANVAS_BOTTOM[1] - CANVAS_TOP[1]) * ratio)
        b = int(CANVAS_TOP[2] + (CANVAS_BOTTOM[2] - CANVAS_TOP[2]) * ratio)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    return img


def _draw_rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int],
) -> None:
    x0, y0, x1, y1 = xy
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.pieslice([x0, y0, x0 + 2 * radius, y0 + 2 * radius], 180, 270, fill=fill)
    draw.pieslice([x1 - 2 * radius, y0, x1, y0 + 2 * radius], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2 * radius, x0 + 2 * radius, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2 * radius, y1 - 2 * radius, x1, y1], 0, 90, fill=fill)


def _composite_card(img: Image.Image) -> None:
    """흰 카드 컨테이너 + 그림자를 배경 위에 합성 (애플/토스 폴리시, in-place).

    그림자: 별도 RGBA 레이어에 SHADOW_RGBA 라운드 사각형(offset +SHADOW_OFFSET)을
      그리고 GaussianBlur(SHADOW_BLUR) → 배경에 alpha_composite. 작은 썸네일에서도
      카드 면적이 분리돼 보이도록 과감히. 이후 흰 카드면(--sf) + 1px BORDER 테두리.
    Pillow 11 ImageDraw.rounded_rectangle 사용 (antialias 라운드, pieslice gap 없음).
    """
    # 1) 그림자 레이어 (offset 적용, blur 여유 위해 캔버스 전체 크기)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        [
            CARD_X0 + SHADOW_OFFSET,
            CARD_Y0 + SHADOW_OFFSET,
            CARD_X1 + SHADOW_OFFSET,
            CARD_Y1 + SHADOW_OFFSET,
        ],
        radius=CARD_RADIUS,
        fill=SHADOW_RGBA,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
    img.paste(Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB"), (0, 0))

    # 2) 흰 카드면 + 1px 테두리 (메인 draw 컨텍스트)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [CARD_X0, CARD_Y0, CARD_X1, CARD_Y1],
        radius=CARD_RADIUS,
        fill=CARD_FILL,
        outline=BORDER,
        width=1,
    )


def _draw_change_chip(
    draw: ImageDraw.ImageDraw,
    text: str,
    text_color: tuple[int, int, int],
    chip_bg: tuple[int, int, int],
    right: int,
    center_y: int,
    font: ImageFont.FreeTypeFont,
) -> int:
    """등락률 pill 칩 (radius 999, pad 좌우16·상하8). 우측 끝=right 기준 우정렬.

    반환: 칩 좌측 x (종목명 말줄임 폭 계산에 사용 — 칩 폭 반영 의무).
    """
    pad_x, pad_y = 16, 8
    tb = font.getbbox(text)
    tw = tb[2] - tb[0]
    th = tb[3] - tb[1]
    chip_w = tw + pad_x * 2
    chip_h = th + pad_y * 2
    chip_x1 = right
    chip_x0 = chip_x1 - chip_w
    chip_y0 = center_y - chip_h // 2
    chip_y1 = chip_y0 + chip_h
    draw.rounded_rectangle(
        [chip_x0, chip_y0, chip_x1, chip_y1], radius=999, fill=chip_bg
    )
    # 텍스트는 bbox 오프셋 보정하여 칩 중앙에 배치
    draw.text(
        (chip_x0 + pad_x - tb[0], chip_y0 + pad_y - tb[1]),
        text,
        fill=text_color,
        font=font,
    )
    return chip_x0


def _truncate(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    bbox = font.getbbox(text)
    if bbox[2] - bbox[0] <= max_width:
        return text
    for i in range(len(text), 0, -1):
        candidate = text[:i] + "..."
        bbox = font.getbbox(candidate)
        if bbox[2] - bbox[0] <= max_width:
            return candidate
    return "..."


def _format_price(price: int | float | None) -> str:
    if price is None:
        return "-"
    return f"{int(price):,}원"


def _format_change(
    pct: float | None,
) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    """등락률 → (텍스트, 텍스트색, 칩배경). 색=전일 종가 기준 (한국 증시 관습)."""
    if pct is None:
        return "-", MUTED, CHIP_FLAT_BG
    if pct > 0:
        return f"+{pct:.2f}%", CANDLE_UP, CHIP_UP_BG
    if pct < 0:
        return f"{pct:.2f}%", CANDLE_DOWN, CHIP_DOWN_BG
    return "0.00%", CANDLE_FLAT, CHIP_FLAT_BG


def _load_intraday_map(date_str: str) -> dict[str, dict]:
    """당일 분봉 intraday를 code별로 로드 (카드 sparkline 동일 소스).

    /data/interpreted/stock-{date}.json → stocks[].intraday {open, base, prices}.
    파일/필드 부재 시 빈 dict (sparkline graceful 생략). 추측 0 — 실제 카드가 읽는
    경로·필드를 grep 확인 후 사용 (data-loader.js:52 + renderer.js:969-970).
    """
    path = INTERPRETED_DIR / f"stock-{date_str}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    stocks = data.get("stocks") if isinstance(data, dict) else None
    if not isinstance(stocks, list):
        return {}
    out: dict[str, dict] = {}
    for st in stocks:
        code = st.get("code")
        intr = st.get("intraday")
        if not code or not isinstance(intr, dict):
            continue
        prices = intr.get("prices")
        if not isinstance(prices, list) or len(prices) < 2:
            continue
        out[code] = {
            "prices": [p for p in prices if isinstance(p, (int, float)) and p > 0],
            "base": intr.get("base") if intr.get("base") else intr.get("open"),
            "open": intr.get("open"),
        }
    return out


def _load_interpreted_change_map(date_str: str) -> dict[str, dict]:
    """카드와 동일 소스 (/data/interpreted/stock-{date}.json) 에서 code별 change_pct·close_price 로드.

    cycle25 Path 1 fallback chain 2순위 (renderer.js L349-466 parity):
      stocks[].code → {change_pct, close_price}.
    파일/필드 부재 시 빈 dict (fallback 3·4순위로 cascade).
    """
    path = INTERPRETED_DIR / f"stock-{date_str}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    stocks = data.get("stocks") if isinstance(data, dict) else None
    if not isinstance(stocks, list):
        return {}
    out: dict[str, dict] = {}
    for st in stocks:
        code = st.get("code")
        if not code:
            continue
        out[code] = {
            "change_pct": st.get("change_pct"),
            "close_price": st.get("close_price"),
        }
    return out


def _load_carded_universe(date_str: str) -> list[dict]:
    """OG 생성 대상 SSOT = 화면 노출 종목 카드 universe (/data/interpreted/stock-{date}.json stocks[]).

    OG 생성 대상을 화면 노출 카드와 정합 (대표 5/28 13:38 KST "당연하지").
    배경: 종래 OG 는 daily_picks(영웅식 SC 전체)를 순회 → 화면 카드(latest_stocks
      snapshot ∪ 상한가 union)보다 종목 과다 (화면에 안 뜨는 종목 OG 생성 = SSOT 불일치).

    카드 universe SSOT = build_daily 가 쓰는 stocks[] (build_daily.py:1881
      latest_stocks||daily_top||stocks base + L2946~3025 상한가 union → interpreted
      JSON stocks[] 박제). renderer.js:279/305 가 읽는 카드 base 와 동일 set.
      → OG 가 동일 파일 stocks[] 를 순회하면 화면 카드 = OG = landing 1:1 정합.

    반환: [{code, name, change_pct, close_price}] (stocks[] 등장 순서 = 카드 정렬 순서).
      파일/필드 부재 시 빈 list → 호출측이 daily_picks 폴백 (구 일자 호환).
    """
    path = INTERPRETED_DIR / f"stock-{date_str}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    stocks = data.get("stocks") if isinstance(data, dict) else None
    if not isinstance(stocks, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for st in stocks:
        if not isinstance(st, dict):
            continue
        code = st.get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(
            {
                "code": code,
                "name": st.get("name") or code,
                "change_pct": st.get("change_pct"),
                "close_price": st.get("close_price"),
            }
        )
    return out


# 한국 증시 상하한가 ±30% 제도적 fact. ±35% 초과 = ka10017 source 결함 가능성 (renderer.js
# L408-414 anomaly guard verbatim parity). cap 위반 시 해당 layer skip → 다음 fallback cascade.
_FLU_RT_CAP = 35.0


def _fetch_flu_rt_from_badges(
    conn: sqlite3.Connection, code: str, date_str: str
) -> float | None:
    """1순위 — stock_status_badges payload_json.flu_rt 본문 (limit-up/down 우선).

    renderer.js L402-419 _extractLimitEffect parity:
      effect_badges[].effect IN ('limit-up','limit-down') AND |flu_rt| <= 35.
    cap 위반 시 None 반환 → cascade.
    """
    rows = conn.execute(
        """
        SELECT payload_json
        FROM stock_status_badges
        WHERE date = ? AND stock_code = ?
        """,
        (date_str, code),
    ).fetchall()
    if not rows:
        return None
    for row in rows:
        raw = row["payload_json"] if isinstance(row, sqlite3.Row) else row[0]
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        effect_badges = (
            payload.get("effect_badges") if isinstance(payload, dict) else None
        )
        flu_rt = payload.get("flu_rt") if isinstance(payload, dict) else None
        if flu_rt is None or not isinstance(effect_badges, list):
            continue
        is_limit = any(
            isinstance(eb, dict) and eb.get("effect") in ("limit-up", "limit-down")
            for eb in effect_badges
        )
        if not is_limit:
            continue
        try:
            flu_val = float(flu_rt)
        except (TypeError, ValueError):
            continue
        if abs(flu_val) > _FLU_RT_CAP:
            continue
        return flu_val
    return None


def _calc_change_pct_from_dailybars(
    conn: sqlite3.Connection, code: str, date_str: str
) -> float | None:
    """4순위 마지막 안전판 — dailybars (close - prev_close) / prev_close × 100 직접 재계산.

    date=date_str close + date<date_str MAX close 본문 산식 (raw kiwoom 결함 회피).
    bar 부재·prev_close 0 시 None. |결과| > _FLU_RT_CAP 시 None (dailybars 본문에도
    raw kiwoom 결함이 전파된 케이스 — graceful "-" 표시 우선, 잘못된 값 표시 회피).
    """
    cur = conn.execute(
        "SELECT close FROM dailybars WHERE code = ? AND date = ?",
        (code, date_str),
    ).fetchone()
    if not cur or cur["close"] is None:
        return None
    prev = conn.execute(
        """
        SELECT close FROM dailybars
        WHERE code = ? AND date < ? AND close IS NOT NULL
        ORDER BY date DESC LIMIT 1
        """,
        (code, date_str),
    ).fetchone()
    if not prev or prev["close"] is None or prev["close"] == 0:
        return None
    try:
        cur_v = float(cur["close"])
        prev_v = float(prev["close"])
    except (TypeError, ValueError):
        return None
    if prev_v == 0:
        return None
    result = (cur_v - prev_v) / prev_v * 100.0
    # cap guard — dailybars layer 본문에도 raw 결함 전파 시 None → graceful "-" 표시
    if abs(result) > _FLU_RT_CAP:
        return None
    return result


def _get_change_pct(
    conn: sqlite3.Connection,
    code: str,
    date_str: str,
    daily_picks_pct: float | None,
    interpreted_map: dict[str, dict],
) -> float | None:
    """cycle25 Path 1 — OG 카드 등락률 source chain 통일 (renderer.js L349-466 parity).

    fallback cascade:
      1) stock_status_badges payload_json.flu_rt (limit-up/down + |≤35|)
      2) interpreted/stock-{date}.json [code].change_pct
      3) daily_picks.change_pct (|≤35| cap — raw kiwoom 결함 회피)
      4) dailybars (close-prev_close)/prev_close × 100 직접 재계산

    대표 catch verbatim (5/27 22:48 KST): "특정 종목에 대한 조치 혹은 하드코딩은 안된다.
    전체로직을 점검하도록" — 전 종목 일관 처리, 하드코딩 0건.
    Q-20260527-OG-MISMATCH (052710 +29.82 vs 실제 +0.84 misclassification 본문).
    """
    # 1순위
    pct = _fetch_flu_rt_from_badges(conn, code, date_str)
    if pct is not None:
        return pct
    # 2순위
    interp_entry = interpreted_map.get(code)
    if isinstance(interp_entry, dict):
        interp_pct = interp_entry.get("change_pct")
        if interp_pct is not None:
            try:
                return float(interp_pct)
            except (TypeError, ValueError):
                pass
    # 3순위 — daily_picks (cap)
    if daily_picks_pct is not None:
        try:
            dp_val = float(daily_picks_pct)
            if abs(dp_val) <= _FLU_RT_CAP:
                return dp_val
        except (TypeError, ValueError):
            pass
    # 4순위 — dailybars 직접 재계산
    return _calc_change_pct_from_dailybars(conn, code, date_str)


def _carded_codes_from_daily_picks(
    conn: sqlite3.Connection, date_str: str
) -> list[dict]:
    """폴백 — interpreted stocks[] 부재(구 일자) 시 daily_picks 에서 카드 universe 복원.

    SoT: heroshik_strict_<m_d> source 우선, 없으면 'kiwoom'. rank 순.
    interpreted JSON 이 존재하는 일자는 본 폴백 미사용 (정상 path = _load_carded_universe).
    """
    parts = date_str.split("-")
    m_d_tag = "_".join(str(int(p)) for p in parts[1:])
    strict_source = f"heroshik_strict_{m_d_tag}"
    strict_count = conn.execute(
        "SELECT COUNT(*) FROM daily_picks WHERE date=? AND source=?",
        (date_str, strict_source),
    ).fetchone()[0]
    source_filter_value = strict_source if strict_count > 0 else "kiwoom"
    rows = conn.execute(
        """
        SELECT dp.stock_code AS code, dp.change_pct AS change_pct, dp.price AS price
        FROM daily_picks dp
        WHERE dp.date = ? AND dp.source = ?
        ORDER BY dp.rank
        """,
        (date_str, source_filter_value),
    ).fetchall()
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        code = r["code"]
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(
            {
                "code": code,
                "name": None,
                "change_pct": r["change_pct"],
                "close_price": r["price"],
            }
        )
    return out


def _query_stocks_for_date(date_str: str) -> list[dict]:
    """OG 생성 대상 = 화면 노출 종목 카드 universe (interpreted stocks[]) + DB 보강.

    OG 대상 SSOT 정합 (대표 5/28 13:38 KST "당연하지"):
      종래 OG 는 daily_picks(영웅식 SC 전체)를 순회 → 화면 카드보다 종목 과다
      (화면에 안 뜨는 종목까지 OG 생성). 화면 카드 universe =
      build_daily latest_stocks/daily_top base ∪ 상한가 union (interpreted JSON
      stocks[] 박제, renderer.js:279/305 와 동일 set).
      → OG 가 동일 stocks[] 만 순회 = 화면 카드 1:1 정합 (OG PNG = landing HTML =
        공유버튼 target 모두 동일 universe).

    구 일자(interpreted JSON 부재)는 daily_picks 폴백으로 복원 (_carded_codes_from_daily_picks).

    각 종목 보강 (driving set = 카드 universe, 필드는 DB join 으로 유지):
      - name/industry: stocks 마스터 (interpreted name 우선, 폴백 stocks.name).
      - price: interpreted close_price 우선, 폴백 daily_picks.price.
      - change_pct: _get_change_pct cascade (badges → interpreted → daily_picks → dailybars).
      - news_title: 당일 발행 최신 1건.
      - daily_20: dailybars 20영업일 OHLC (좌 일봉). 0건 → 일봉 생략.
      - intraday: interpreted 분봉 (우 sparkline). 0건 → sparkline 생략.
    거래대금 순위/금액(rank/trade_amount)은 OG 미노출 (검색식 선정 노출 제외) → 조회 제외.
    """
    intraday_map = _load_intraday_map(date_str)
    # cycle25 Path 1 — fallback chain 2순위 source (renderer.js L349-466 parity)
    interpreted_change_map = _load_interpreted_change_map(date_str)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        # 1순위 driving set = 화면 카드 universe (interpreted stocks[]).
        carded = _load_carded_universe(date_str)
        # 폴백 (구 일자 — interpreted JSON 부재): daily_picks 에서 복원.
        if not carded:
            carded = _carded_codes_from_daily_picks(conn, date_str)

        # daily_picks 보강 lookup (price/change_pct 폴백 + name 보조). source 무관 전건.
        dp_by_code: dict[str, dict] = {}
        for dp in conn.execute(
            "SELECT stock_code AS code, change_pct, price FROM daily_picks WHERE date=?",
            (date_str,),
        ).fetchall():
            dp_by_code.setdefault(
                dp["code"], {"change_pct": dp["change_pct"], "price": dp["price"]}
            )

        results: list[dict] = []
        for card in carded:
            code = card["code"]
            # name/industry: stocks 마스터 (interpreted name 우선).
            srow = conn.execute(
                "SELECT name, industry FROM stocks WHERE code = ?",
                (code,),
            ).fetchone()
            name = card.get("name") or (srow["name"] if srow else None) or code
            industry = (srow["industry"] if srow else "") or ""

            news_row = conn.execute(
                """
                SELECT title FROM news
                WHERE stock_code = ? AND date(published_at) = ?
                  AND COALESCE(is_robot, 0) = 0
                ORDER BY published_at DESC
                LIMIT 1
                """,
                (code, date_str),
            ).fetchone()

            # 20영업일 OHLC (date_str 이하 최근 20거래일, 정시 정렬 ASC).
            # mini-candle.js daily_20 입력 포맷과 동일: [{date, o, h, l, c}].
            bar_rows = conn.execute(
                """
                SELECT date, open, high, low, close
                FROM dailybars
                WHERE code = ? AND date <= ?
                ORDER BY date DESC
                LIMIT 20
                """,
                (code, date_str),
            ).fetchall()
            daily_20 = [
                {
                    "date": b["date"],
                    "o": b["open"],
                    "h": b["high"],
                    "l": b["low"],
                    "c": b["close"],
                }
                for b in reversed(bar_rows)
                if b["open"] is not None
                and b["high"] is not None
                and b["low"] is not None
                and b["close"] is not None
            ]

            # price: interpreted close_price 우선, 폴백 daily_picks.price.
            dp_entry = dp_by_code.get(code) or {}
            price = card.get("close_price")
            if price is None:
                price = dp_entry.get("price")

            # change_pct cascade (renderer.js parity, 하드코딩 0):
            #   badges → interpreted → daily_picks → dailybars.
            change_pct_final = _get_change_pct(
                conn,
                code,
                date_str,
                dp_entry.get("change_pct"),
                interpreted_change_map,
            )
            results.append(
                {
                    "code": code,
                    "name": name,
                    "industry": industry,
                    "change_pct": change_pct_final,
                    "price": price,
                    "news_title": news_row["title"] if news_row else "",
                    "daily_20": daily_20,
                    "intraday": intraday_map.get(code),
                }
            )
        return results
    finally:
        conn.close()


def _draw_candle_panel(
    draw: ImageDraw.ImageDraw,
    daily_20: list[dict],
    bounds: tuple[int, int, int, int],
) -> None:
    """20영업일 미니 일봉캔들 패널 (하단 좌). 텍스트 레이블 없음 (2026-05-27 제거).

    bounds = (x0, y0, x1, y1) 라운드 박스. geometry: js/lib/mini-candle.js 정합
      (slot, bodyW 0.62비율, y매핑=가격↑위, isFlat=동가 가로선, 점상 min height).
    캔들 색 = 시가(open) 기준: close>open 양봉 #C53939 / close<open 음봉 #1958C7 /
      close==open 동가 #94A3B8 (전일 종가 기준 아님 — mini-candle.js L30-32 정합).
    daily_20 비어 있으면 호출부에서 미호출 (graceful 생략).
    """
    n = len(daily_20)
    if n == 0:
        return
    x0, y0, x1, y1 = bounds
    pad = PANEL_PAD
    inner_x0 = x0 + pad
    inner_x1 = x1 - pad
    inner_y0 = y0 + pad  # 레이블 제거 → 상단 여백 회수
    inner_y1 = y1 - pad
    inner_w = inner_x1 - inner_x0
    inner_h = inner_y1 - inner_y0

    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=PANEL_RADIUS,
        fill=PANEL_BG,
        outline=BORDER,
        width=1,
    )

    lows = [d["l"] for d in daily_20 if d["l"] and d["l"] > 0]
    highs = [d["h"] for d in daily_20 if d["h"] and d["h"] > 0]
    if not lows or not highs:
        return
    lo = min(lows)
    hi = max(highs)
    span = (hi - lo) or 1

    slot = inner_w / n
    body_w = max(1.5, slot * 0.7)  # 캔들 body=slot*0.7 (스펙)

    def y_of(price: float) -> float:
        return inner_y0 + inner_h * (1 - (price - lo) / span)

    for i, d in enumerate(daily_20):
        xc = inner_x0 + slot * (i + 0.5)
        x_body = xc - body_w / 2
        is_up = d["c"] > d["o"]
        is_flat = d["c"] == d["o"]
        color = CANDLE_FLAT if is_flat else (CANDLE_UP if is_up else CANDLE_DOWN)
        y_hi, y_lo = y_of(d["h"]), y_of(d["l"])
        y_open, y_close = y_of(d["o"]), y_of(d["c"])
        draw.line([(xc, y_hi), (xc, y_lo)], fill=color, width=2)
        if is_flat:
            draw.line(
                [(x_body, y_open), (x_body + body_w, y_open)], fill=color, width=2
            )
        else:
            y_body_top = min(y_open, y_close)
            body_h = max(1.5, abs(y_close - y_open))
            draw.rectangle(
                [x_body, y_body_top, x_body + body_w, y_body_top + body_h], fill=color
            )


def _draw_sparkline_panel(
    draw: ImageDraw.ImageDraw,
    intraday: dict,
    bounds: tuple[int, int, int, int],
) -> None:
    """당일 분봉 sparkline 패널 (하단 우) — js/lib/sparkline.js 정합. 레이블 없음.

    bounds = (x0, y0, x1, y1). base 기준선(점선) + 라인. 색: open(=base) 대비 현재가
      방향 (renderer.js:581 candleDir = price >= open ? up : down 정합).
    prices < 2 또는 intraday None 이면 호출부에서 미호출 (graceful 생략).
    """
    if not intraday:
        return
    prices = intraday.get("prices") or []
    if len(prices) < 2:
        return
    base = intraday.get("base") or intraday.get("open") or prices[0]

    x0, y0, x1, y1 = bounds
    pad = PANEL_PAD
    inner_x0 = x0 + pad
    inner_x1 = x1 - pad
    inner_y0 = y0 + pad  # 레이블 제거 → 상단 여백 회수
    inner_y1 = y1 - pad
    inner_w = inner_x1 - inner_x0
    inner_h = inner_y1 - inner_y0

    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=PANEL_RADIUS,
        fill=PANEL_BG,
        outline=BORDER,
        width=1,
    )

    lo = min(min(prices), base)
    hi = max(max(prices), base)
    span = (hi - lo) or 1

    # 방향색 (open 대비 현재가) — sparkline.js / renderer.js:581 정합.
    last = prices[-1]
    if last > base:
        color = CANDLE_UP
    elif last < base:
        color = CANDLE_DOWN
    else:
        color = CANDLE_FLAT

    def x_of(i: int) -> float:
        return inner_x0 + inner_w * i / (len(prices) - 1)

    def y_of(p: float) -> float:
        return inner_y0 + inner_h * (1 - (p - lo) / span)

    # base 기준선 (점선 효과 = 짧은 세그먼트 반복)
    base_y = y_of(base)
    seg = 8
    xx = inner_x0
    while xx < inner_x1:
        draw.line([(xx, base_y), (min(xx + 4, inner_x1), base_y)], fill=MUTED, width=1)
        xx += seg

    # 분봉 라인
    pts = [(x_of(i), y_of(p)) for i, p in enumerate(prices)]
    draw.line(pts, fill=color, width=3, joint="curve")


def _draw_card(stock: dict, date_str: str) -> Image.Image:
    # 디자인 스펙 타이포: 종목명 bold 52 / 가격 40 / 등락칩 bold 36 / 날짜 32.
    #   "가격 medium 40" — 2-face fallback 체인에 medium weight 없음 → regular 40 채택
    #   (synthetic 합성 금지, 추측 0). 등락칩은 bold 36 (강조).
    #   날짜 26→32 (2026-05-27 대표 catch — 키움).
    name_font = _bold_font(52)  # 종목명 = bold weight (진짜 굵게)
    price_font = _regular_font(
        40
    )  # 가격 (medium 부재 → regular 40, 대표 regular 유지 지시)
    chip_font = _bold_font(36)  # 등락률 칩 (bold 36)
    date_font = _regular_font(32)  # 푸터 날짜 (26→32 키움)
    _regular_font(18)

    img = _gradient_bg()
    # 흰 카드 컨테이너 + 그림자 합성 (배경 위, 콘텐츠 그리기 전).
    _composite_card(img)
    draw = ImageDraw.Draw(img)

    daily_20 = stock.get("daily_20") or []
    intraday = stock.get("intraday")
    has_candle = len(daily_20) > 0
    has_spark = bool(intraday and len(intraday.get("prices") or []) >= 2)

    # 카드 내부 콘텐츠 경계.
    content_right = CONTENT_X1  # 1092

    # ── 상단 행 y≈104: [종목명(bold 52·좌)] ···· [가격 + 등락칩(우)] ──
    # 방향색 단일 소스 (등락칩·세로 액센트 바 동일 change_color — 모순 0).
    change_text, change_color, chip_bg = _format_change(stock["change_pct"])
    price_text = _format_price(stock["price"])

    # ── A: 방향 세로 액센트 바 (카드 좌측 안쪽, 색=등락 방향 change_color) ──
    #   x0=CARD_X0+24, w=4, y=TOP_Y~TOP_Y+64, rounded r2. 종목명은 +20 밀어 간섭 회피.
    accent_x0 = CARD_X0 + 24
    accent_w = 4
    draw.rounded_rectangle(
        [accent_x0, TOP_Y, accent_x0 + accent_w, TOP_Y + 64],
        radius=2,
        fill=change_color,
    )
    content_left = CONTENT_X0  # 108 — 가격/날짜 정렬 좌측 (불변)
    name_left = CONTENT_X0 + 20  # 종목명·골드언더바 +20 (A 액센트 바 간섭 회피)

    # 등락 pill 칩 (우측 끝 content_right 우정렬). 텍스트 중앙 = 종목명 baseline 정렬.
    # 종목명 시각 중심에 칩 중심 맞추기 위해 name_font 높이 기준 center 계산.
    name_bbox = name_font.getbbox(stock["name"] or "")
    name_h = name_bbox[3] - name_bbox[1]
    row_center_y = TOP_Y + name_h // 2
    chip_x0 = _draw_change_chip(
        draw, change_text, change_color, chip_bg, content_right, row_center_y, chip_font
    )

    # 가격 (칩 좌측, 우정렬). 가격 baseline 도 row_center 정렬.
    price_gap = 20
    price_w = price_font.getbbox(price_text)[2] - price_font.getbbox(price_text)[0]
    price_bbox = price_font.getbbox(price_text)
    price_h = price_bbox[3] - price_bbox[1]
    price_x = chip_x0 - price_gap - price_w
    draw.text(
        (price_x, row_center_y - price_h // 2 - price_bbox[1]),
        price_text,
        fill=TX,
        font=price_font,
    )

    # 종목명 (bold·좌, name_left=+20). 말줄임 한계 = 가격 블록 좌측까지 (칩 폭 반영됨).
    name_limit = price_x - price_gap - name_left
    name_text = _truncate(stock["name"], name_font, name_limit)
    draw.text((name_left, TOP_Y), name_text, fill=TX, font=name_font)

    # ── C: 종목명 골드 언더바 (--am2 #E8C063) ──
    #   종목명 baseline 하단 +12px, h4 r2. x0=name_left, x1=+min(name_w*0.4, 120).
    name_w = name_font.getbbox(name_text)[2] - name_font.getbbox(name_text)[0]
    underbar_y0 = TOP_Y + (name_bbox[3] - name_bbox[1]) + 12
    underbar_x1 = name_left + int(min(name_w * 0.4, 120))
    draw.rounded_rectangle(
        [name_left, underbar_y0, underbar_x1, underbar_y0 + 4],
        radius=2,
        fill=GOLD,
    )

    # ── 차트 2패널: 일봉(좌) | 분봉(우), 레이블 없음, 각 데이터 없으면 graceful 생략 ──
    if has_candle:
        _draw_candle_panel(draw, daily_20, (CANDLE_X0, CHART_Y0, CANDLE_X1, CHART_Y1))
    if has_spark:
        _draw_sparkline_panel(draw, intraday, (SPARK_X0, CHART_Y0, SPARK_X1, CHART_Y1))

    # ── 푸터: 날짜 (좌 x=108, y=520) ──
    draw.text((content_left, FOOTER_Y), date_str, fill=MUTED, font=date_font)

    # ── 푸터 우측: PM320 워드마크 로고 (Q-20260606 코워크 로고). 기존 레이아웃 존중 — 날짜와 같은 행, 우 정렬. ──
    #   부재 시 graceful skip (로고 없이 생성, 거짓 합성 0). 회사명 텍스트 풋터(SHOW_BRANDING)는 로고로 대체.
    _paste_wordmark(img, content_right, FOOTER_Y)

    return img


def _paste_wordmark(img: Image.Image, right_x: int, base_y: int) -> None:
    """PM320 워드마크를 푸터 우측에 텍스트로 렌더 — 'PM'=잉크(TX), '320'=골드(GOLD).

    2026-06-15 대표 catch: 옛 PNG 에셋(pm320_wordmark_light.png)이 'PM 3.20'(공백+소수점,
    옛 표기) → 헤더(pm320.html aria-label="PM320") 정본 브랜드 'PM320'으로 통일(공백/소수점 0).
    텍스트 draw 라 별도 에셋 불요·항상 렌더(거짓 합성 0·PNG 부재 graceful 불요).
    """
    draw = ImageDraw.Draw(img)
    font = _bold_font(36)
    pm_w = draw.textlength("PM", font=font)
    num_w = draw.textlength("320", font=font)
    x = right_x - (pm_w + num_w)
    draw.text((x, base_y), "PM", fill=TX, font=font)
    draw.text((x + pm_w, base_y), "320", fill=GOLD, font=font)


def _generate_html(stock: dict, date_str: str) -> Path:
    """종목별 메타 HTML — JS redirect (fragment 보존, FLR 0229f59 경험 반영).

    출력: ~/company/100m1s-homepage/pm320/{date}/{code}.html

    2026-05-27: redirect target = `?stock={code}&date={date}` query
      (Phase 2c-1 single-card mode 정합, renderer.js L1574~1586).
      URL 경로/파라미터엔 한글 X (code 6자리만, feedback_share_url_ticker_only.md 정합).
      한글 종목명은 OG title/표시 텍스트에만 노출 (인코딩·앵커 무관).
    """
    html_dir = STOCK_HTML_BASE_DIR / date_str
    html_dir.mkdir(parents=True, exist_ok=True)

    code = stock["code"]
    name = stock["name"]
    # og:title = 종목명만 (2026-05-27 최종 스펙). SHOW_BRANDING 시만 "· 100M1S" 접미사.
    #   URL/landing 경로는 code 유지 (내부 식별자), 표시 title에서만 code 제거.
    title = f"{name} · 100M1S" if SHOW_BRANDING else name

    if stock["news_title"]:
        desc = stock["news_title"]
    elif stock["industry"]:
        desc = f"{stock['industry']} · 오늘의 주목 종목"
    else:
        desc = f"{name} 오늘의 종목 정보"

    # HTML escape (최소한: < > " & → 치환)
    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    # OG image cache-bust (2026-06-01 대표 catch, FLR-AGT-002 meta 변종 — 메신저 OG scraper PNG 캐시 stale 봉쇄):
    #   PNG 재생성 시 mtime 갱신 → ?v={mtime} query 변경 → 메신저(카톡 등)가 새 URL로 강제 fetch.
    #   _generate_html은 generate_one에서 PNG save 직후 호출 (L1059~1063) → mtime 본문 신선.
    #   PNG 부재 fallback = date_str compact (예 "20260601"). 결손 시 graceful (메신저 평소 동작).
    _png_path = OG_BASE_DIR / date_str / f"{code}.png"
    try:
        _cache_token = str(int(_png_path.stat().st_mtime))
    except (OSError, FileNotFoundError):
        _cache_token = date_str.replace("-", "")
    og_image_url = f"https://100m1s.com/og/pm320/{date_str}/{code}.png?v={_cache_token}"
    page_url = f"https://100m1s.com/pm320/{date_str}/{code}.html"

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{esc(title)}</title>
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:image" content="{og_image_url}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="article">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{og_image_url}">
<meta name="robots" content="noindex">
<script>
(function() {{
  var params = new URLSearchParams(location.search);
  var date = params.get('date') || '{date_str}';
  var code = '{code}';
  // Phase 2c-1 single-card mode 정합 (renderer.js L1574~1586): redirect target =
  //   `?stock={{code}}&date={{date}}` query (구 `#stock-{{code}}` hash anchor 폐기).
  //   본 종목 1개만 풍부 카드 render. URL엔 한글 X (code 6자리만).
  location.replace(
    '/pm320.html?stock=' + encodeURIComponent(code) +
    '&date=' + encodeURIComponent(date)
  );
}})();
</script>
</head>
<body>
<p>리다이렉트 중... <a href="/pm320.html?stock={code}&date={date_str}">{esc(name)} ({code}) 종목 보기</a></p>
</body>
</html>"""

    out_path = html_dir / f"{code}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ── sparkline incomplete 마킹 (FLR-20260605-TEC-001 OG sparkline 재생성) ───
# 배경 (대표 2026-06-05 09:48 catch): 장 초반(분봉 0건) 에 생성된 OG 는 sparkline 이
#   graceful 생략된 채 영구 박제 (477850 6/5 사례). 분봉이 늦게 들어와도 재생성 안 됨.
# fix: sparkline 미완(분봉 < 2점) OG 는 PNG 옆에 `{code}.incomplete` 마커 박제 →
#   다음 fire 에서 (a) 분봉 입수 시 재생성 + 마커 제거, (b) 장마감 후엔 graceful 확정
#   (마커 제거 = 더 이상 재생성 대상 아님, 무한 재생성 루프 방지).
# 추정 금지 (FLR-AGT-002): 분봉 결측 시 가짜 sparkline 그리지 않음 — graceful 생략 유지.


def _has_sparkline(stock: dict) -> bool:
    """OG sparkline 완결 여부 — _draw_card 의 has_spark 와 동일 판정 (분봉 ≥ 2점)."""
    intraday = stock.get("intraday")
    return bool(intraday and len(intraday.get("prices") or []) >= 2)


def _incomplete_marker_path(date_str: str, code: str) -> Path:
    return OG_BASE_DIR / date_str / f"{code}.incomplete"


def _is_today_intraday_window(date_str: str) -> bool:
    """date_str 가 오늘이고 장마감(15:30) 전이면 True — 분봉 추가 입수 가능 구간.

    오늘이 아니거나(과거 일자) 15:30 이후면 분봉이 더 들어오지 않으므로
    sparkline 미완은 graceful 확정 (재생성 불요).
    """
    now = datetime.now()
    if date_str != now.strftime("%Y-%m-%d"):
        return False
    # 15:30 마감 — 마감 후엔 당일 분봉 frozen (collect_intraday 도 마감 후 재fetch 무의미).
    return (now.hour, now.minute) < (15, 30)


def generate_one(stock: dict, date_str: str) -> tuple[Path, Path]:
    og_dir = OG_BASE_DIR / date_str
    og_dir.mkdir(parents=True, exist_ok=True)
    img = _draw_card(stock, date_str)
    png_path = og_dir / f"{stock['code']}.png"
    img.save(str(png_path), "PNG", optimize=True)
    html_path = _generate_html(stock, date_str)

    # sparkline incomplete 마킹 — 분봉 미입수 + 당일 장중이면 재생성 대상 박제,
    # 그 외(완결 / 마감 후 / 과거 일자)는 마커 제거하여 graceful 확정.
    marker = _incomplete_marker_path(date_str, stock["code"])
    if _has_sparkline(stock):
        marker.unlink(missing_ok=True)  # 분봉 입수 → 완결, 마커 해제
    elif _is_today_intraday_window(date_str):
        marker.write_text(  # 미완 + 장중 → 다음 fire 재생성 대상
            datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
        )
    else:
        marker.unlink(
            missing_ok=True
        )  # 마감 후/과거 → graceful 확정 (무한 재생성 방지)
    return png_path, html_path


def generate_for_date(date_str: str, only_codes: set[str] | None = None) -> int:
    """OG 생성. only_codes 지정 시 해당 종목 코드만 재생성 (P1-1/P1-3 누락분 한정).

    only_codes (FLR-20260605-TEC-001 P1-1/P1-3): 배포/생성 완결성 게이트가 검출한
      누락 코드만 재생성. None 이면 카드 universe 전건 생성 (정규 cron 경로).
    """
    stocks = _query_stocks_for_date(date_str)
    if not stocks:
        print(f"[stock-OG] {date_str}: daily_picks 없음, 스킵")
        return 0

    if only_codes is not None:
        stocks = [s for s in stocks if s["code"] in only_codes]
        if not stocks:
            print(
                f"[stock-OG] {date_str}: only_codes 매칭 0건 (요청 {len(only_codes)}종)"
            )
            return 0
        print(f"[stock-OG] {date_str}: {len(stocks)}종목 재생성 (누락분 한정)")
    else:
        print(f"[stock-OG] {date_str}: {len(stocks)}종목 생성 시작")
    ok = 0
    for st in stocks:
        try:
            generate_one(st, date_str)
            ok += 1
        except Exception as e:
            # 1종목 실패가 전체 파이프라인 멈추지 않게 continue (FLR-20260408-TEC-001 교훈)
            print(f"[stock-OG][ERR] {st['code']} {st['name']}: {e}", file=sys.stderr)
    print(f"[stock-OG] 완료: {ok}/{len(stocks)} 성공")
    return ok


def main() -> None:
    # 누락코드 한정 재생성 인자 (FLR-20260605-TEC-001 P1-1/P1-3):
    #   --codes CODE1,CODE2  또는 env M1S_OG_ONLY_CODES=CODE1,CODE2
    # 배포/생성 완결성 게이트(kiwoom_cron.sh / pipeline.sh)가 누락 코드만 재생성 호출.
    argv = sys.argv[1:]
    only_codes: set[str] | None = None
    date_arg: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--codes" and i + 1 < len(argv):
            only_codes = {c.strip() for c in argv[i + 1].split(",") if c.strip()}
            i += 2
        elif not argv[i].startswith("--"):
            date_arg = argv[i]
            i += 1
        else:
            i += 1
    if only_codes is None:
        env_codes = os.environ.get("M1S_OG_ONLY_CODES", "").strip()
        if env_codes:
            only_codes = {c.strip() for c in env_codes.split(",") if c.strip()}

    arg = (
        os.environ.get("PIPELINE_DATE")
        or date_arg
        or datetime.now().strftime("%Y-%m-%d")
    )
    generate_for_date(arg, only_codes=only_codes)


if __name__ == "__main__":
    main()
