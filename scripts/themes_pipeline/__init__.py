"""themes_pipeline — 테마뉴스 분리 파이프라인 (REQ-20260420-REQ-004 / Phase 5).

메인 stocks.db와 분리된 themes.db에 테마 분석 산출물을 적재한다.
메인은 ATTACH read-only로 themes_db.theme_map / themes_db.macro_summary만 참조.

테이블 구성 (themes.db):
- themes_raw       : 이시카와 1차 분석 (RSS+카페 통합 분석 raw)
- themes_verified  : 토구사 검증 결과
- theme_map        : 날짜별 테마 트리 (JSON)
- macro_summary    : 날짜별 매크로 요약 (best-of)
- cafe_*           : 카페 자리만 확보 (구현 보류)
- llm_cache        : Phase 3 동일 스키마. domain in (theme_extract, theme_verify, cafe_extract)
"""
