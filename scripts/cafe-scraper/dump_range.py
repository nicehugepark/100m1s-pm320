"""뉴지 debug dump — 파싱 없이 원본 HTML/meta/text만 저장.

사용:
  cd scripts/cafe-scraper && python dump_range.py

입력:
  - .env (NAVER_COOKIES)
  - ../../data/cafe/index.json 의 post_id 리스트 (2026-04-01~2026-04-08 추정 범위)

출력:
  data/cafe/debug/{post_id}.html
  data/cafe/debug/{post_id}.meta.json
  data/cafe/debug/{post_id}.txt

parse_post 호출 금지. fetch_article_html + BeautifulSoup get_text만.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# .env 로드
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# main.py 재사용
sys.path.insert(0, str(Path(__file__).parent))
from main import (
    ARTICLE_URL_TEMPLATE,
    CAFE_ID,
    KST,
    MENU_ID,
    fetch_article_html,
    get_browser_context,
    log,
    naver_login,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "cafe"
DEBUG_DIR = DATA_DIR / "debug"
INDEX_PATH = DATA_DIR / "index.json"


def load_post_ids() -> list[str]:
    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return [p["post_id"] for p in idx.get("posts", [])]


def inject_cookies(context, cookies_json: str) -> bool:
    ss_map = {
        "no_restriction": "None", "norestriction": "None", "none": "None",
        "lax": "Lax", "strict": "Strict", "unspecified": "Lax", "": "Lax",
    }
    try:
        cookie_list = json.loads(cookies_json)
    except Exception as e:
        log(f"쿠키 파싱 실패: {e}")
        return False
    normalized = []
    for c in cookie_list:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        domain = (c.get("domain") or ".naver.com").strip()
        if "naver.com" not in domain:
            continue
        same_site = (c.get("sameSite") or "").strip().lower()
        cookie = {
            "name": str(name), "value": str(value), "domain": domain,
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
            "sameSite": ss_map.get(same_site, "Lax"),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if exp:
            try:
                f = float(exp)
                if f > 0:
                    cookie["expires"] = f
            except (TypeError, ValueError):
                pass
        normalized.append(cookie)
    if not normalized:
        return False
    context.add_cookies(normalized)
    log(f"쿠키 {len(normalized)}개 주입")
    return True


def extract_title(page) -> str:
    for sel in [".ArticleTitle h3", ".title_text", "h3.title_text", "h3"]:
        try:
            el = page.query_selector(sel)
            if el:
                t = (el.inner_text() or "").strip()
                if t:
                    return t
        except Exception:
            pass
    for frame in page.frames:
        for sel in [".ArticleTitle h3", ".title_text", "h3.title_text"]:
            try:
                el = frame.query_selector(sel)
                if el:
                    t = (el.inner_text() or "").strip()
                    if t:
                        return t
            except Exception:
                pass
    return ""


def main() -> int:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    post_ids = load_post_ids()
    log(f"대상 post_id {len(post_ids)}개")

    cookies_json = os.environ.get("NAVER_COOKIES", "").strip()
    naver_id = os.environ.get("NAVER_CAFE_ID", "")
    naver_pw = os.environ.get("NAVER_CAFE_PASSWORD", "")
    if not cookies_json and not (naver_id and naver_pw):
        log("❌ NAVER_COOKIES 없음 + ID/PW 없음")
        return 2

    saved = 0
    failed = []

    with sync_playwright() as p:
        browser, context = get_browser_context(p)
        authed = False
        if cookies_json:
            if inject_cookies(context, cookies_json):
                page = context.new_page()
                try:
                    page.goto("https://www.naver.com", wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
                    logged_in = page.evaluate(
                        "() => { const t = document.body.innerText || ''; return t.includes('로그아웃') || t.includes('MY'); }"
                    )
                    if logged_in:
                        log("✓ 쿠키 인증 성공")
                        authed = True
                    else:
                        log("❌ 쿠키 만료 — 즉시 중단")
                        browser.close()
                        return 5
                except Exception as e:
                    log(f"쿠키 검증 예외: {e}")
                    browser.close()
                    return 6
            else:
                page = context.new_page()
        else:
            page = context.new_page()

        if not authed:
            log("ID/PW 로그인 시도")
            if not naver_login(page, naver_id, naver_pw):
                log("❌ 로그인 실패")
                browser.close()
                return 3

        for i, pid in enumerate(post_ids, 1):
            log(f"[{i}/{len(post_ids)}] {pid} fetch")
            html = fetch_article_html(page, pid)
            if not html:
                failed.append(pid)
                continue

            html_path = DEBUG_DIR / f"{pid}.html"
            meta_path = DEBUG_DIR / f"{pid}.meta.json"
            txt_path = DEBUG_DIR / f"{pid}.txt"

            html_path.write_text(html, encoding="utf-8")

            try:
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text("\n", strip=True)
            except Exception as e:
                text = f"[BS4 실패: {e}]"
            txt_path.write_text(text, encoding="utf-8")

            title = extract_title(page)
            meta = {
                "post_id": pid,
                "post_url": ARTICLE_URL_TEMPLATE.format(article_id=pid),
                "fetched_at": datetime.now(KST).isoformat(timespec="seconds"),
                "board": f"cafe {CAFE_ID} menu {MENU_ID}",
                "title": title,
                "html_bytes": len(html),
                "text_chars": len(text),
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            saved += 1
            time.sleep(1.5)

        browser.close()

    log(f"완료: {saved}/{len(post_ids)} 저장, 실패 {len(failed)}: {failed}")
    return 0 if saved > 0 else 4


if __name__ == "__main__":
    sys.exit(main())
