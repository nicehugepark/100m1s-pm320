/* ───── lib/chart-tv/toggle-panel.js — 보조지표 토글 UI chip bar (cycle23 toggle-fibonacci-only) ─────
   REQ DOC-20260521-REQ-001 §5 + 대표 verbatim 2026-05-21 14:57 KST + 2026-05-22 17:21 KST + 2026-05-22 17:58 KST 정합.

   본질 (cycle22 P0-16 verbatim "일목균형표는 도저히 안되겠다 제거해줘"):
   - 대표 verbatim 09:15:50 KST: "그리고 하단 지표인 거래대금 rsi macd는 토글뱌튼 필요없이 기본 출력이야"
   - → tradingValue/macd/rsi chip 제거 (base 영구 ON, expanded-chart.js addTradingValue/addMACD/addRSI 1회 호출)

   cycle23 chart-tv-3changes Spot 2 (2026-05-22 17:22 KST 대표 verbatim
     "배당락 토글이 있는데 아직 한번도 검증되진 않았지만 기본기능으로 판단하고 항상 표시해주는걸로
      한 다음 토글 버튼은 제거해줘"):
   - 배당락 chip 제거 cascade (exDividend 영구 ON)

   cycle23 toggle-fibonacci-only (2026-05-22 17:58 KST 대표 verbatim
     "기능 검증을 위해 피보나치 토글 버튼만 남기고 나머지는 기본으로 항상 표시 시킨 후 토글 버튼을 모두 제거해줘.
      그리고 터글버튼의 글자와 크기가 상대적으로 너무 크다"):
   - **1 chip** (fibonacci 본문 본질만 유지 본문 — MA + 매물대 + 분홍 chip 본문 모두 제거 cascade)
   - ma6 / volumeByDecile / pinkSignal = 영구 ON (expanded-chart.js applyState 본문 force, state 본문 무시)
   - fibonacci = 사용자 toggle 본문 본문 유지 (기능 검증 본질 + drawing tool 본질 visual disruption 본문 옵션 본질)
   - CSS chip 크기/글자 축소 (news.css §11.27 cascade)

   schema (cycle23 toggle-fibonacci-only 정정):
   localStorage key 'm100s.chart.tv.indicators.global' value:
   {
     "fibonacci": true  // 사용자 toggle 가능 본문 본질 (default true)
   }
   ma6/volumeByDecile/pinkSignal/exDividend/tradingValue/macd/rsi = state 본문 외 영구 ON layer (base 본문).

   §16 self-catch (cycle23 toggle-fibonacci-only):
   - chip 1개 = fibonacci 본문만 (MA/매물대/분홍 chip 본문 모두 제거 cascade)
   - ma6/volumeByDecile/pinkSignal 본문 expanded-chart.js applyState 본문 force ON (state 본문 무시)
     → 사용자 localStorage 본문 false 잔존 본문 봉쇄 본문 정합 (배당락 본문 본질 본문 정합)
   - fibonacci chip 본문 본문 유지 = 기능 검증 본문 (대표 verbatim "기능 검증을 위해 피보나치 토글 버튼만 남기고")
   - CSS chip 크기 축소 = 글자 font-size 12→10 / min-width 48→36 / height 28→20 / padding 0 10→0 6 본문 본질
*/

// cycle23 toggle-fibonacci-only 본질 — 1 chip (fibonacci 본문만, 나머지 chip 모두 제거 cascade)
export const INDICATOR_CHIPS = [
  // drawing tool (P0-16 Fix-51: default ON + auto-anchor 본문)
  // cycle23 toggle-fibonacci-only: 사용자 toggle 본문 본질 유지 (기능 검증 본질)
  { key: 'fibonacci', label: '피보', name: '피보나치', category: 'drawing' },
];

/**
 * 토글 panel chip bar 신축.
 * @param {HTMLElement} parent — chart-expanded 상단 wrapper
 * @param {Object} state — 현 indicator state ({ma6: true, ...})
 * @param {Function} onChange — state 변경 callback (newState) => void
 * @returns {HTMLElement} — chip bar element (parent에 이미 append됨)
 */
export function buildTogglePanel(parent, state, onChange) {
  if (!parent) return null;

  const bar = document.createElement('div');
  bar.className = 'cal-chart-tv-toggle-bar';
  bar.setAttribute('role', 'toolbar');
  bar.setAttribute('aria-label', '보조지표 토글');

  INDICATOR_CHIPS.forEach((chip) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cal-chart-tv-chip';
    btn.dataset.key = chip.key;
    btn.dataset.category = chip.category;
    const isOn = state[chip.key] === true;
    btn.setAttribute('aria-pressed', String(isOn));
    btn.classList.toggle('is-on', isOn);
    btn.textContent = chip.label;
    btn.title = chip.name + ' ' + (isOn ? '끄기' : '켜기');

    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const newOn = !(state[chip.key] === true);
      state[chip.key] = newOn;
      btn.setAttribute('aria-pressed', String(newOn));
      btn.classList.toggle('is-on', newOn);
      btn.title = chip.name + ' ' + (newOn ? '끄기' : '켜기');
      if (typeof onChange === 'function') onChange(state);
    });

    bar.appendChild(btn);
  });

  parent.appendChild(bar);
  return bar;
}

if (typeof window !== 'undefined') {
  window.ChartTVTogglePanel = { buildTogglePanel, INDICATOR_CHIPS };
}
