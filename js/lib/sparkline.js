/* ───── lib/sparkline.js — 당일 분봉 sparkline SVG (renderer.js:14-37 추출) ─────
   분리 commit: REQ-001 §3 Phase 1. DSN: DOC-20260430-DSN-001-arch-frontend §3.1.
   IIFE + window 전역 등록 (SW cache 호환). 동작 100% 동일 보존.
*/
(function (root) {
  'use strict';

  // 당일 분봉 sparkline SVG (open 기준선 + 라인 + 하단 그라데이션)
  function buildSparkline(prices, base, dir) {
    if (!prices || prices.length < 2) return '';
    const W = 60, H = 32, PAD = 2;
    const min = Math.min(...prices, base);
    const max = Math.max(...prices, base);
    const span = max - min || 1;
    const x = i => PAD + (W - 2*PAD) * i / (prices.length - 1);
    const y = p => PAD + (H - 2*PAD) * (1 - (p - min) / span);
    const d = prices.map((p, i) => (i === 0 ? 'M' : 'L') + x(i).toFixed(1) + ' ' + y(p).toFixed(1)).join(' ');
    const color = dir === 'up' ? '#C53939' : dir === 'down' ? '#1958C7' : '#888';
    const gradId = 'g' + Math.random().toString(36).slice(2, 8);
    const fillD = d + ` L${x(prices.length-1).toFixed(1)} ${H-PAD} L${x(0).toFixed(1)} ${H-PAD} Z`;
    const baseY = y(base).toFixed(1);
    return `<svg viewBox="0 0 ${W} ${H}">
    <defs><linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${color}" stop-opacity="0.35"/>
      <stop offset="1" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    <path d="${fillD}" fill="url(#${gradId})"/>
    <line x1="${PAD}" y1="${baseY}" x2="${W-PAD}" y2="${baseY}" stroke="#888" stroke-width="0.8" stroke-dasharray="2,2" opacity="0.5"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.3"/>
  </svg>`;
  }

  root.buildSparkline = buildSparkline;
})(typeof window !== 'undefined' ? window : this);
