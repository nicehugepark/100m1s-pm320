"""
PM320 뉴스 페이지 날짜별 OG 이미지 + 딥링크 HTML 생성기.

사용:
  python -m scripts.news_pipeline.generate_og [YYYY-MM-DD]
  python -m scripts.news_pipeline.generate_og --all   # DB 전체 날짜

출력:
  ~/company/100m1s-homepage/og/{date}.png        (1200x630)
  ~/company/100m1s-homepage/og/og-news.png       (최신 날짜 사본)
  ~/company/100m1s-homepage/pm320/{date}.html     (OG 메타 + redirect, Q-20260605-105)
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── 경로 설정 ──────────────────────────────────────────────
# HOMEPAGE_DIR SoT — config.py 의 HOMEPAGE (env M1S_HOMEPAGE override 가능).
# cron worktree 격리 (lead-meta §11.32) 정합 — M1S_HOMEPAGE=~/company/100m1s-homepage-cron 환경에서 cron pipeline 실행 시 자동 cron worktree 대상.
from .config import HOMEPAGE as HOMEPAGE_DIR

SCRIPT_DIR = Path(__file__).resolve().parent
FONT_DIR = SCRIPT_DIR / "fonts"
# Q-20260606 — PM320 워드마크 (코워크 로고). 날짜별 OG 푸터 우측 코너 합성. 부재 시 graceful skip.
PM320_WORDMARK_PATH = SCRIPT_DIR / "assets" / "pm320_wordmark_light.png"
DB_PATH = HOMEPAGE_DIR / "data" / "stocks.db"
# Q-20260606-119 — 날짜별 OG URL 에서 stock 세그먼트 제거 (/pm320/stock/{date}.html → /pm320/{date}.html).
OG_DIR = HOMEPAGE_DIR / "og" / "pm320"
NEWS_DIR = HOMEPAGE_DIR / "pm320"

# ── 색상 ────────────────────────────────────────────────────
BG_TOP = (0x1A, 0x1D, 0x26)
BG_BOT = (0x0D, 0x0F, 0x14)
WHITE = (0xFF, 0xFF, 0xFF)
GOLD = (0xE8, 0xC0, 0x63)
CHIP_BG = (0x2A, 0x2D, 0x36)
CHIP_TEXT = (0xC4, 0x99, 0x30)
MUTED = (0x6B, 0x7A, 0x99)

# ── 이미지 크기 ─────────────────────────────────────────────
W, H = 1200, 630

# ── 요일 한글 ───────────────────────────────────────────────
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """fonts/ 디렉토리에서 폰트 로드."""
    path = FONT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"폰트 없음: {path}")
    return ImageFont.truetype(str(path), size)


def _gradient_bg() -> Image.Image:
    """수직 그라디언트 배경 이미지 생성."""
    img = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        ratio = y / H
        r = int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * ratio)
        g = int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * ratio)
        b = int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * ratio)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    return img


def _draw_rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int],
) -> None:
    """모서리 둥근 사각형."""
    x0, y0, x1, y1 = xy
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.pieslice([x0, y0, x0 + 2 * radius, y0 + 2 * radius], 180, 270, fill=fill)
    draw.pieslice([x1 - 2 * radius, y0, x1, y0 + 2 * radius], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2 * radius, x0 + 2 * radius, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2 * radius, y1 - 2 * radius, x1, y1], 0, 90, fill=fill)


def _format_date(date_str: str) -> str:
    """'2026-04-10' → '2026.04.10 (금)'"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    wd = WEEKDAY_KR[dt.weekday()]
    return f"{dt.year}.{dt.month:02d}.{dt.day:02d} ({wd})"


def _format_date_ko(date_str: str) -> str:
    """'2026-04-10' → '4월 10일 (금)'"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    wd = WEEKDAY_KR[dt.weekday()]
    return f"{dt.month}월 {dt.day}일 ({wd})"


def _query_data(date_str: str) -> tuple[int, int, list[str]]:
    """kiwoom JSON + DB에서 종목수, 연속선정수, 뉴스 키워드 조회.

    프론트와 동일한 소스(kiwoom JSON)를 사용하여 수치 일치 보장.
    """
    import json as _json

    # 종목 수: kiwoom JSON 기준 (프론트와 동일)
    kiwoom_json = HOMEPAGE_DIR / "data" / "kiwoom" / f"{date_str}.json"
    today_tickers: set[str] = set()
    if kiwoom_json.exists():
        with open(kiwoom_json) as f:
            kdata = _json.load(f)
        today_tickers = {s["ticker"] for s in kdata.get("daily_top", [])}
    stock_count = len(today_tickers)

    # 연속선정: 전일 kiwoom JSON과 교집합
    # 전일 날짜 찾기
    prev_tickers: set[str] = set()
    prev_candidates = sorted(
        [
            p.stem
            for p in (HOMEPAGE_DIR / "data" / "kiwoom").glob("*.json")
            if p.stem < date_str and p.stem != "index" and p.stem != "latest"
        ],
        reverse=True,
    )
    if prev_candidates:
        prev_json = HOMEPAGE_DIR / "data" / "kiwoom" / f"{prev_candidates[0]}.json"
        if prev_json.exists():
            with open(prev_json) as f:
                pdata = _json.load(f)
            prev_tickers = {s["ticker"] for s in pdata.get("daily_top", [])}
    streak_count = len(today_tickers & prev_tickers)

    # 뉴스 요약 키워드: DB에서 조회
    conn = sqlite3.connect(str(DB_PATH))
    try:
        macros = conn.execute(
            """
            SELECT keyword FROM macro_events
            WHERE date=? AND source IN ('interpret', 'llm')
            ORDER BY
              CASE source WHEN 'interpret' THEN 0 ELSE 1 END,
              id
            LIMIT 3
            """,
            (date_str,),
        ).fetchall()
        keywords = [row[0] for row in macros]

        return stock_count, streak_count, keywords
    finally:
        conn.close()


def _truncate(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """텍스트가 max_width를 초과하면 말줄임."""
    bbox = font.getbbox(text)
    if bbox[2] - bbox[0] <= max_width:
        return text
    for i in range(len(text), 0, -1):
        candidate = text[:i] + "..."
        bbox = font.getbbox(candidate)
        if bbox[2] - bbox[0] <= max_width:
            return candidate
    return "..."


def generate(date_str: str) -> Path:
    """OG 이미지 생성 후 파일 경로 반환."""
    OG_DIR.mkdir(parents=True, exist_ok=True)

    stock_count, streak_count, keywords = _query_data(date_str)

    # 폰트 로드
    bold_40 = _load_font("NotoSansKR-Bold.ttf", 40)
    bold_28 = _load_font("NotoSansKR-Bold.ttf", 28)
    regular_22 = _load_font("NotoSansKR-Regular.ttf", 22)
    regular_18 = _load_font("NotoSansKR-Regular.ttf", 18)

    # 배경
    img = _gradient_bg()
    draw = ImageDraw.Draw(img)

    # ── 상단 (y:40~90) ──────────────────────────────────────
    # 좌: 텍스트 로고
    draw.text((60, 48), "100M1S", fill=MUTED, font=bold_28)
    # 우: 날짜
    date_display = _format_date(date_str)
    date_bbox = bold_28.getbbox(date_display)
    date_w = date_bbox[2] - date_bbox[0]
    draw.text((W - 60 - date_w, 48), date_display, fill=WHITE, font=bold_28)

    # ── 구분선 ───────────────────────────────────────────────
    draw.line([(60, 100), (W - 60, 100)], fill=(0x2A, 0x2D, 0x36), width=1)

    # ── 중앙 (y:200~300) — 핵심 지표 ───────────────────────
    main_text = f"오늘의 종목 {stock_count}개"
    if streak_count > 0:
        main_text += f" · 연속선정 {streak_count}종"

    # 텍스트 중앙 정렬
    main_bbox = bold_40.getbbox(main_text)
    main_w = main_bbox[2] - main_bbox[0]
    main_x = (W - main_w) // 2
    main_y = 230

    # 숫자 부분은 골드, 나머지는 흰색 — 단순 구현: 전체를 흰색으로 그린 뒤
    # 숫자+단위만 골드 오버레이
    draw.text((main_x, main_y), main_text, fill=WHITE, font=bold_40)

    # 골드 하이라이트: 종목 수
    prefix = "오늘의 종목 "
    prefix_w = bold_40.getbbox(prefix)[2] - bold_40.getbbox(prefix)[0]
    count_str = f"{stock_count}개"
    draw.text((main_x + prefix_w, main_y), count_str, fill=GOLD, font=bold_40)

    # 골드 하이라이트: 연속선정 수
    if streak_count > 0:
        streak_prefix = f"오늘의 종목 {stock_count}개 · 연속선정 "
        sp_w = bold_40.getbbox(streak_prefix)[2] - bold_40.getbbox(streak_prefix)[0]
        streak_str = f"{streak_count}종"
        draw.text((main_x + sp_w, main_y), streak_str, fill=GOLD, font=bold_40)

    # ── 하단 (y:380~450) — 뉴스 칩 ─────────────────────────
    if keywords:
        chip_y = 390
        chip_h = 42
        chip_pad_x = 20
        chip_gap = 16
        chip_radius = 12

        # 칩 너비 계산
        chip_specs: list[tuple[str, int]] = []
        for kw in keywords:
            text = _truncate(kw, regular_22, 320)
            tw = regular_22.getbbox(text)[2] - regular_22.getbbox(text)[0]
            chip_specs.append((text, tw + chip_pad_x * 2))

        total_w = sum(s[1] for s in chip_specs) + chip_gap * (len(chip_specs) - 1)
        start_x = (W - total_w) // 2

        cx = start_x
        for text, cw in chip_specs:
            _draw_rounded_rect(
                draw, (cx, chip_y, cx + cw, chip_y + chip_h), chip_radius, CHIP_BG
            )
            # 텍스트 수직 중앙
            text_bbox = regular_22.getbbox(text)
            th = text_bbox[3] - text_bbox[1]
            ty = chip_y + (chip_h - th) // 2 - text_bbox[1]
            draw.text((cx + chip_pad_x, ty), text, fill=CHIP_TEXT, font=regular_22)
            cx += cw + chip_gap

    # ── 풋터 (y:560~600) ────────────────────────────────────
    footer = "100M1S News · 100m1s.com"
    footer_bbox = regular_18.getbbox(footer)
    footer_w = footer_bbox[2] - footer_bbox[0]
    draw.text(((W - footer_w) // 2, 570), footer, fill=MUTED, font=regular_18)

    # ── PM320 워드마크 (Q-20260606 코워크 로고) 우측 코너 합성. 부재 시 graceful skip (로고 없이 생성). ──
    if PM320_WORDMARK_PATH.exists():
        try:
            _mark = Image.open(PM320_WORDMARK_PATH).convert("RGBA")
            _th = 32
            _tw = max(1, round(_mark.width * (_th / _mark.height)))
            _mark = _mark.resize((_tw, _th), Image.LANCZOS)
            img.paste(_mark, (W - _tw - 48, 566), _mark)
        except (OSError, ValueError):
            pass

    # 저장
    out_path = OG_DIR / f"{date_str}.png"
    img.save(str(out_path), "PNG", optimize=True)
    print(
        f"[OG] {out_path}  ({stock_count}종목, {streak_count}연속, {len(keywords)}칩)"
    )

    # 폐기 (2026-05-08): /news.html og:image = 회사 og-image.png 고정 (특정 날짜 종속 X, 대표 catch P0)
    # 종목카드 og(/og/news/stock/{date}.png) + 날짜별 og({date}.png)는 유지
    # root_og = HOMEPAGE_DIR / "og" / "og-news.png"  # 폐기
    # shutil.copy2(str(out_path), str(root_og))      # 폐기

    # 날짜별 딥링크 HTML 생성
    _generate_date_html(date_str, stock_count, keywords)

    return out_path


def _generate_date_html(date_str: str, stock_count: int, keywords: list[str]) -> Path:
    """날짜별 OG 메타 + redirect HTML 생성."""
    NEWS_DIR.mkdir(parents=True, exist_ok=True)

    date_ko = _format_date_ko(date_str)
    macro_summary = ", ".join(keywords) if keywords else "뉴스 요약"

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{date_ko} 뉴스 — 100M1S</title>
<meta property="og:title" content="{date_ko} · 종목 {stock_count}개">
<meta property="og:description" content="{macro_summary}">
<meta property="og:image" content="https://100m1s.com/og/pm320/{date_str}.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="https://100m1s.com/pm320/{date_str}.html">
<meta property="og:type" content="article">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://100m1s.com/og/pm320/{date_str}.png">
<meta name="robots" content="noindex">
<script>window.location.replace("/pm320.html?date={date_str}" + (window.location.hash || ""));</script>
</head>
<body>
<p>리다이렉트 중... <a href="/pm320.html#{date_str}">{date_ko} 종목 보기</a></p>
</body>
</html>"""

    out_path = NEWS_DIR / f"{date_str}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"[OG] {out_path}  (HTML deeplink)")
    return out_path


def generate_all() -> None:
    """DB에 있는 모든 날짜에 대해 OG 이미지 + HTML 생성."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_picks ORDER BY date"
        ).fetchall()
    finally:
        conn.close()
    print(f"[OG] 전체 날짜 생성: {len(rows)}건")
    for (date_str,) in rows:
        generate(date_str)


def main() -> None:
    arg = os.environ.get("PIPELINE_DATE") or (
        sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    )
    if arg == "--all":
        generate_all()
    else:
        generate(arg)


if __name__ == "__main__":
    main()
