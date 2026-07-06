"""
DART corpCode.xml 다운로드 → dart_corp_map 테이블 시드.

사용법:
    python -m scripts.news_pipeline.seed_dart_corp

또는 직접 실행:
    cd ~/company/100m1s && python scripts/news_pipeline/seed_dart_corp.py

엔드포인트: https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=API_KEY
응답: ZIP 파일 안에 CORPCODE.xml (corp_code 8자리 ↔ stock_code 6자리 매핑)
"""

from __future__ import annotations

import io
import logging
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime

logger = logging.getLogger(__name__)

CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"


def _download_corp_xml(api_key: str) -> ET.Element:
    """DART corpCode.xml ZIP 다운로드 후 XML root 반환."""
    url = f"{CORP_CODE_URL}?crtfc_key={api_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "100m1s-pipeline/1.0"})

    logger.info("DART corpCode.xml 다운로드 중...")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()

    # ZIP 해제
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        # 보통 CORPCODE.xml 하나
        xml_name = next((n for n in names if n.lower().endswith(".xml")), names[0])
        xml_bytes = zf.read(xml_name)

    return ET.fromstring(xml_bytes)


def _parse_corps(root: ET.Element) -> list[dict]:
    """XML에서 corp_code, stock_code, corp_name 추출.

    stock_code가 비어있는 항목(비상장)은 제외.
    """
    results = []
    for item in root.iter("list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_name = (item.findtext("corp_name") or "").strip()

        if not corp_code or not stock_code or len(stock_code) < 6:
            continue

        results.append(
            {
                "corp_code": corp_code,
                "stock_code": stock_code,
                "corp_name": corp_name,
            }
        )
    return results


def seed(api_key: str | None = None) -> int:
    """dart_corp_map 테이블에 전체 법인코드 매핑 시드.

    Returns:
        upsert된 레코드 수.
    """
    from .config import DART_API_KEY
    from .db import connect

    key = api_key or DART_API_KEY
    if not key:
        print("DART_API_KEY 미설정 -- .env에 DART_API_KEY=발급키 추가 필요")
        return 0

    root = _download_corp_xml(key)
    corps = _parse_corps(root)

    if not corps:
        print("파싱 결과 0건 -- XML 구조 확인 필요")
        return 0

    now = datetime.now().isoformat()
    with connect() as conn:
        # 스키마 보장 (dart_corp_map 없을 경우)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS dart_corp_map (
                corp_code   TEXT PRIMARY KEY,
                stock_code  TEXT,
                corp_name   TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )"""
        )

        for c in corps:
            conn.execute(
                """INSERT INTO dart_corp_map (corp_code, stock_code, corp_name, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(corp_code) DO UPDATE SET
                     stock_code = excluded.stock_code,
                     corp_name  = excluded.corp_name,
                     updated_at = excluded.updated_at""",
                (c["corp_code"], c["stock_code"], c["corp_name"], now),
            )
        conn.commit()

    print(f"dart_corp_map: {len(corps)}건 upsert 완료 (상장법인만)")
    return len(corps)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 직접 실행 시 상위 패키지 import 문제 우회
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from scripts.news_pipeline.config import DART_API_KEY
    from scripts.news_pipeline.db import connect

    key = os.environ.get("DART_API_KEY") or DART_API_KEY
    if not key:
        print("DART_API_KEY 미설정 -- .env에 DART_API_KEY=발급키 추가 필요")
        sys.exit(1)

    # inline seed (import 경로 문제 우회)
    root = _download_corp_xml(key)
    corps = _parse_corps(root)
    if not corps:
        print("파싱 결과 0건")
        sys.exit(1)

    now = datetime.now().isoformat()
    conn = connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dart_corp_map (
            corp_code   TEXT PRIMARY KEY,
            stock_code  TEXT,
            corp_name   TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )"""
    )
    for c in corps:
        conn.execute(
            """INSERT INTO dart_corp_map (corp_code, stock_code, corp_name, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(corp_code) DO UPDATE SET
                 stock_code = excluded.stock_code,
                 corp_name  = excluded.corp_name,
                 updated_at = excluded.updated_at""",
            (c["corp_code"], c["stock_code"], c["corp_name"], now),
        )
    conn.commit()
    conn.close()
    print(f"dart_corp_map: {len(corps)}건 upsert 완료 (상장법인만)")
