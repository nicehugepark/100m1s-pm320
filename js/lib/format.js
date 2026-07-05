/* ───── lib/format.js — 순수 포맷·이스케이프·sanitize (utils.js:3-45 추출) ─────
   분리 commit: REQ-001 §3 Phase 1. DSN: DOC-20260430-DSN-001-arch-frontend §3.1.
   IIFE + window 전역 등록 (SW cache 호환). 동작 100% 동일 보존.
*/
(function (root) {
  'use strict';

  function fmtTradeAmount(won) {
    if (won == null) return '—';
    if (won >= 1_000_000_000_000) return (won / 1_000_000_000_000).toFixed(1) + '조';
    if (won >= 100_000_000) return Math.round(won / 100_000_000).toLocaleString() + '억';
    if (won >= 10_000) return Math.round(won / 10_000).toLocaleString() + '만';
    return won.toLocaleString();
  }

  function fmtNum(n) {
    if (n === null || n === undefined) return '—';
    return n.toLocaleString('ko-KR');
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }

  // 내부 에이전트 이름·고유명을 사용자 화면에서 제거 (상품 톤 유지)
  function sanitize(s) {
    if (s == null) return '';
    return String(s)
      .replace(/박성진\s*매매\s*가치관(?:상)?/g, '내부 거래 기준')
      .replace(/박성진\s*(?:스타일|매매스타일)/g, '내부 거래 스타일')
      .replace(/박성진/g, '내부 기준')
      // legacy: DB에 남은 과거 텍스트 방어 — 토구사(legacy: 주주), 이시카와(legacy: 뉴지) 잔재 제거
      .replace(/주주\s*이견[:：]?/g, '추가 관점:')
      .replace(/주주\s*Top\s*Pick/gi, '엄선 종목')
      .replace(/주주\s*검증/g, '재검증')
      .replace(/주주가\s*/g, '')
      .replace(/주주\s*/g, '')
      .replace(/뉴지\s*미처리/g, '분석 대기')
      .replace(/뉴지\s*선별/g, '선별')
      .replace(/뉴지가\s*/g, '')
      .replace(/뉴지\s*/g, '')
      .replace(/\s{2,}/g, ' ')
      .trim();
  }

  function pad2(n) { return String(n).padStart(2, '0'); }
  function ymd(y, m, d) { return `${y}-${pad2(m)}-${pad2(d)}`; }

  // 전역 등록 (utils.js 잔존 호출부 호환)
  root.fmtTradeAmount = fmtTradeAmount;
  root.fmtNum = fmtNum;
  root.escapeHtml = escapeHtml;
  root.sanitize = sanitize;
  root.pad2 = pad2;
  root.ymd = ymd;
})(typeof window !== 'undefined' ? window : this);
