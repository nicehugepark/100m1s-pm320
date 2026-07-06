"""
KIND viewer 본문 Playwright 헤드리스 fetch.

viewer.js AJAX 동적 주입 구조라 requests.get으로는 래퍼 메타만 수집됨
(FLR-AGT-001: "정정요구" 환각 원인). headless Chromium으로 AJAX 완료 후
document.body.innerText를 추출.

법무 가드레일 (REQ-20260415-REQ-001):
- User-Agent에 contact 명시
- 일일 50건 이내, 직렬(동시 1커넥션), 건당 sleep ~2.5s
- robots.txt Disallow 인지하에 내부 모니터링 목적만
- 본문 원문은 메모리에서만 사용, DB 저장 금지
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

UA = "100m1s-bot/1.0 (contact: nicehugepark@gmail.com)"
KIND_VIEWER = "https://kind.krx.co.kr/common/disclsviewer.do?method=search&acptno={}"

_META_PATTERNS = (
    re.compile(
        r"본\s*공지사항은\s*공시내용\s*기재\s*불충분\s*등의\s*사유로"
        r"\s*한국거래소\s*정정요구를\s*받은\s*사항입니다[^.]*\."
    ),
    re.compile(r"동\s*공시에\s*대한\s*정정공시가\s*이루어지는\s*경우[^.]*\."),
    re.compile(r"본\s*문서는\s*최종문서가\s*아니므로[^.]*확인하시기\s*바랍니다\.?"),
    re.compile(r"문서\s*목차\s*문서\s*목차"),
    re.compile(r"첨부서류\s*첨부문서선택"),
    re.compile(r"기공시\s*기공시선택"),
    re.compile(r"조회\s*본\s*문\s*본문선택"),
)


def _strip_meta(text: str) -> str:
    for pat in _META_PATTERNS:
        text = pat.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_kind_batch(
    rcept_nos: list[str],
    max_chars: int = 3000,
    sleep_sec: float = 2.5,
    page_timeout_ms: int = 15000,
    settle_ms: int = 800,
) -> dict[str, str]:
    """rcept_no 리스트 → {rcept_no: body_text} 매핑.

    단일 브라우저 인스턴스 재사용. iframe 본문을 우선 추출하고
    없으면 body.innerText로 폴백. 메타 스트리핑 후 200자 미만이면
    빈 문자열(NO_BODY 신호).
    """
    out: dict[str, str] = {}
    if not rcept_nos:
        return out

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1024, "height": 800}
        )
        for i, rcp in enumerate(rcept_nos):
            page = ctx.new_page()
            text = ""
            try:
                page.goto(
                    KIND_VIEWER.format(rcp),
                    wait_until="networkidle",
                    timeout=page_timeout_ms,
                )
                page.wait_for_timeout(settle_ms)

                # iframe 우선 — KIND 뷰어는 본문을 iframe에 AJAX 주입
                frame_text = ""
                for fr in page.frames:
                    if fr == page.main_frame:
                        continue
                    try:
                        t = fr.evaluate(
                            "() => document.body && document.body.innerText || ''"
                        )
                        if t and len(t) > len(frame_text):
                            frame_text = t
                    except Exception:
                        continue

                main_text = page.evaluate("() => document.body.innerText || ''")
                text = frame_text if len(frame_text) > len(main_text) else main_text
                text = _strip_meta(text or "")
                if len(text) < 200:
                    text = ""
                else:
                    text = text[:max_chars]
            except Exception as e:
                logger.warning("kind_fetcher FAIL rcp=%s: %s", rcp, e)
                text = ""
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            out[rcp] = text
            if i < len(rcept_nos) - 1:
                time.sleep(sleep_sec)

        try:
            ctx.close()
        finally:
            browser.close()

    return out


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    rcps = sys.argv[1:]
    if not rcps:
        print("usage: python -m scripts.news_pipeline.kind_fetcher <rcept_no> [...]")
        sys.exit(1)
    res = fetch_kind_batch(rcps)
    for k, v in res.items():
        print(f"[{k}] len={len(v)} head={v[:120]!r}")
