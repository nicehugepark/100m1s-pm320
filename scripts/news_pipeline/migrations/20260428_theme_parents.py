"""theme_parents 테이블 신설 + 기존 themes.parent_id 데이터 복제.

REQ-076 Phase 1 (DOC-20260428-REQ-005 §3.1, §3.2).

- 신설 테이블: theme_parents (child_id, parent_id, weight, source, created_at).
- 마이그레이션: themes.parent_id IS NOT NULL 행을 1:1 복제 (source='migrated', weight=1.0).
- themes.parent_id 컬럼은 **삭제하지 않음** (Phase 1 범위 — 양 측 병행, 무중단 호환).

검증 후 (4축 §4): migrated_rows == source_rows, 중복 0, 자기참조 0.
"""

from __future__ import annotations

from ..db import _ensure_theme_parents, connect


def migrate() -> None:
    with connect() as conn:
        # 1. 스키마 보장 (db.py에서 정의 — 신규 환경 대응)
        _ensure_theme_parents(conn)

        # 2. 기존 parent_id 복제 (멱등성 — UNIQUE(child_id, parent_id) 제약으로 INSERT OR IGNORE)
        before = conn.execute(
            "SELECT COUNT(*) FROM theme_parents WHERE source='migrated'"
        ).fetchone()[0]
        source_rows = conn.execute(
            "SELECT COUNT(*) FROM themes WHERE parent_id IS NOT NULL"
        ).fetchone()[0]

        conn.execute(
            """INSERT OR IGNORE INTO theme_parents
                  (child_id, parent_id, weight, source, created_at)
               SELECT id, parent_id, 1.0, 'migrated', datetime('now')
               FROM themes
               WHERE parent_id IS NOT NULL"""
        )
        conn.commit()

        after = conn.execute(
            "SELECT COUNT(*) FROM theme_parents WHERE source='migrated'"
        ).fetchone()[0]
        dup = conn.execute(
            "SELECT COUNT(*) FROM (SELECT child_id, parent_id FROM theme_parents "
            "GROUP BY child_id, parent_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        self_ref = conn.execute(
            "SELECT COUNT(*) FROM theme_parents WHERE child_id = parent_id"
        ).fetchone()[0]

        print(
            f"theme_parents migrated: before={before} after={after} source={source_rows}"
        )
        print(f"  duplicates={dup}  self_ref={self_ref}")
        if after != source_rows:
            raise SystemExit(
                f"FAIL: migrated_rows({after}) != source_rows({source_rows})"
            )
        if dup != 0 or self_ref != 0:
            raise SystemExit(f"FAIL: dup={dup} self_ref={self_ref}")
        print("PASS")


if __name__ == "__main__":
    migrate()
