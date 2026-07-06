"""disclosures.source_url에서 rcept_no → rcpNo 일괄 교체.

DART가 rcept_no 파라미터를 거부해 발생한 링크 404 이슈 복구용.
실행: python3 -m scripts.news_pipeline.migrations.20260415_fix_dart_url
"""

from __future__ import annotations

from scripts.news_pipeline.db import connect


def run() -> None:
    with connect() as c:
        cur = c.execute(
            "SELECT COUNT(*) FROM disclosures WHERE source_url LIKE '%?rcept_no=%'"
        )
        before = cur.fetchone()[0]
        c.execute(
            "UPDATE disclosures SET source_url = "
            "REPLACE(source_url, '?rcept_no=', '?rcpNo=') "
            "WHERE source_url LIKE '%?rcept_no=%'"
        )
        c.commit()
        print(f"migrated {before} rows: rcept_no → rcpNo")


if __name__ == "__main__":
    run()
