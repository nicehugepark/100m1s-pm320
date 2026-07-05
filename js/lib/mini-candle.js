/* ───── lib/mini-candle.js — 20영업일 미니 캔들 SVG (renderer.js:42-76 추출) ─────
   분리 commit: REQ-001 §3 Phase 1. DSN: DOC-20260430-DSN-001-arch-frontend §3.1.
   IIFE + window 전역 등록 (SW cache 호환). 동작 100% 동일 보존.
*/
(function (root) {
  'use strict';

  // REQ-pm320-ux-cycle #3 — 20영업일 일봉 캔들 SVG.
  // 양봉(close>open) #C53939, 음봉 #1958C7, 동가 #94A3B8 (한국 증시 관습).
  // 데스크탑 110×32, 모바일은 CSS viewBox preserveAspectRatio로 78×26 자동 축소.
  // daily_20 = [{date, o, h, l, c}] 정시 정렬(ASC).
  // Q-MOBILE-CANDLES-FIX-A (2026-05-12) — 신규 상장 1건 합성 일봉 허용 위해 하한 < 5 → < 1.
  // renderer.js가 range_240d + intraday.open으로 1건 합성을 전달. 정상 5건+ 종목은 동작 동일.
  function buildCandles20(daily20) {
    if (!Array.isArray(daily20) || daily20.length < 1) return '';
    const N = daily20.length;
    const W = 110, H = 32, PAD_X = 4, PAD_Y = 2;
    const slot = (W - 2 * PAD_X) / N;
    const bodyW = Math.max(1.5, slot * 0.7);
    const lows = daily20.map(d => d.l).filter(v => v > 0);
    const highs = daily20.map(d => d.h).filter(v => v > 0);
    if (!lows.length || !highs.length) return '';
    const lo = Math.min(...lows);
    const hi = Math.max(...highs);
    const span = hi - lo || 1;
    const y = p => PAD_Y + (H - 2 * PAD_Y) * (1 - (p - lo) / span);
    const parts = daily20.map((d, i) => {
      const xc = PAD_X + slot * (i + 0.5);
      const xBody = xc - bodyW / 2;
      const isUp = d.c > d.o;
      const isFlat = d.c === d.o;
      const color = isFlat ? '#94A3B8' : (isUp ? '#C53939' : '#1958C7');
      const yHi = y(d.h), yLo = y(d.l);
      const yOpen = y(d.o), yClose = y(d.c);
      const yBodyTop = Math.min(yOpen, yClose);
      const bodyH = Math.max(1.5, Math.abs(yClose - yOpen));  // min 1.5px — 0.8px는 비Retina에서 안 보임 (design ss_9358kzog8)
      const wick = `<line x1="${xc.toFixed(1)}" y1="${yHi.toFixed(1)}" x2="${xc.toFixed(1)}" y2="${yLo.toFixed(1)}" stroke="${color}" stroke-width="1"/>`;
      const body = isFlat
        ? `<line x1="${xBody.toFixed(1)}" y1="${yOpen.toFixed(1)}" x2="${(xBody + bodyW).toFixed(1)}" y2="${yOpen.toFixed(1)}" stroke="${color}" stroke-width="1.5"/>`
        : `<rect x="${xBody.toFixed(1)}" y="${yBodyTop.toFixed(1)}" width="${bodyW.toFixed(1)}" height="${bodyH.toFixed(1)}" fill="${color}"/>`;
      const pct = d.o > 0 ? (((d.c - d.o) / d.o) * 100).toFixed(1) : '0.0';
      const tip = `${d.date}\n시 ${d.o.toLocaleString()} / 고 ${d.h.toLocaleString()}\n저 ${d.l.toLocaleString()} / 종 ${d.c.toLocaleString()} (${pct >= 0 ? '+' : ''}${pct}%)`;
      const hit = `<rect x="${(xc - slot/2).toFixed(1)}" y="0" width="${slot.toFixed(1)}" height="${H}" fill="transparent"><title>${tip}</title></rect>`;
      return wick + body + hit;
    }).join('');
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${parts}</svg>`;
  }

  root.buildCandles20 = buildCandles20;
})(typeof window !== 'undefined' ? window : this);
