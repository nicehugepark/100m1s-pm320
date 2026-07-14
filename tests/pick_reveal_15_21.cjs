#!/usr/bin/env node
/*
 * PM320 15:21 보류일 화면 확정 회귀 게이트 (2026-07-14 대표 지시 ④).
 *
 * 근원(FLR-20260713-TEC-001 연장선 + project_pm320_1520_screen_promise "15:21 내 노출"):
 *   renderer.js _startPickRevealPoll 의 픽/보류 "도착" 판정이 취약한 마커
 *   summary.backtest_detail.as_of === today 에 의존했다. as_of = last_settled_date(청산일)라
 *   보류일 + 당일 무청산일엔 as_of ≠ today → markerToday 영구 false → 구 _directFallback(15:22)
 *   까지 일자파일 fetch 지연 = 15:21 브랜드 약속 위반(21분 초과). 오늘(2026-07-14)이 보류일이라
 *   대표가 실제 목격.
 *
 * 근원 수정: 일자파일(픽/보류 확정 SoT)을 15:20:00 부터 직접 probe + 견고 신호
 *   (date === today && stocks.length > 0 — 보류일도 후보 stocks>0 확정) 로 판정. 예고배너는
 *   보류확정(pm320NoPick===true)·픽확정(false) 둘 다 억제, 미확정(null)만 표시.
 *
 * 본 게이트는 실제 renderer.js 소스에서 판정 헬퍼 2개(_pm320PickRevealArrived /
 *   _pm320PickBannerPending)를 추출·평가(참조구현 아닌 실코드)해 보류/픽/미확정/stale 케이스를
 *   회귀 감시 + 취약 마커(markerToday) 재발 여부 grep + 배포된 보류일 일자파일 stocks>0 데이터
 *   계약을 검증한다.
 *
 * 실행: node tests/pick_reveal_15_21.cjs   (exit 0 = PASS, 1 = FAIL)
 * 매일 회귀: run_daily_pick_and_push.sh(launchd 15:20 정각) Step2 배포 후 비차단 호출.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const REPO = path.join(__dirname, '..');
const RENDERER = path.join(REPO, 'js', 'renderer.js');
const src = fs.readFileSync(RENDERER, 'utf8');

const cases = [];
function check(desc, got, exp) {
  const ok = got === exp;
  cases.push({ desc, ok });
  if (!ok) console.error(`  FAIL: ${desc} — got ${JSON.stringify(got)}, expected ${JSON.stringify(exp)}`);
}

// 1) 실제 renderer.js 소스에서 판정 헬퍼 2개 본문 추출 → vm 평가 (참조구현 아닌 실코드 검증)
const mA = src.match(/function _pm320PickRevealArrived\s*\([^)]*\)\s*\{[\s\S]*?\n\}/);
const mP = src.match(/function _pm320PickBannerPending\s*\([^)]*\)\s*\{[\s\S]*?\n\}/);
check('헬퍼 _pm320PickRevealArrived 소스 존재', !!mA, true);
check('헬퍼 _pm320PickBannerPending 소스 존재', !!mP, true);
let arrived, pending;
if (mA && mP) {
  const sb = {};
  vm.createContext(sb);
  vm.runInContext(
    `${mA[0]}\n${mP[0]}\nthis.arrived=_pm320PickRevealArrived;this.pending=_pm320PickBannerPending;`,
    sb,
  );
  arrived = sb.arrived;
  pending = sb.pending;
}
const T = '2026-07-14';

if (arrived && pending) {
  // 2) arrived — 견고 신호(픽/보류 공통 stocks>0, stale/미확정 차단)
  check('픽일 stocks>0 → 도착', arrived({ date: T, stocks: [{ code: '005930' }] }, T), true);
  check('보류일 stocks=14 → 도착(15:21 승격 보장)', arrived({ date: T, stocks: new Array(14).fill({ code: 'x' }) }, T), true);
  check('빈 stocks → 미도착', arrived({ date: T, stocks: [] }, T), false);
  check('stale(어제 파일) → 차단', arrived({ date: '2026-07-13', stocks: [{ code: 'x' }] }, T), false);
  check('404(null) → 미도착', arrived(null, T), false);
  check('date 필드 부재 → 차단(stale 방지)', arrived({ stocks: [{ code: 'x' }] }, T), false);

  // 3) pending — 예고("곧 공개") 배너: 보류확정·픽확정 억제 / 미확정만 표시
  check('보류확정(true) → 예고배너 억제', pending({ pm320NoPick: true }), false);
  check('픽확정(false) → 예고배너 억제', pending({ pm320NoPick: false }), false);
  check('미확정(null) → 예고배너 표시', pending({ pm320NoPick: null }), true);
  check('data 부재 → 예고배너 표시', pending(undefined), true);
}

// 4) 소스 회귀 — 취약 마커 재발 부재 + 견고 헬퍼 실사용
//    구 로직의 고유 변수 markerToday(=summary.backtest_detail.as_of===today) 부재로 회귀 감지.
//    (주석 설명 텍스트 오탐 회피 — backtest_detail 자체는 성과카드 등 정당 용도로 잔존 가능.)
check('구 markerToday(as_of 마커) 로직 제거 확인', /\bmarkerToday\b/.test(src), false);
check('_startPickRevealPoll 이 견고 헬퍼 실사용', /arrived\s*=\s*_pm320PickRevealArrived\(d,\s*today\)/.test(src), true);
check('예고배너 판정 헬퍼 실사용', /_pickPending\s*=\s*_pm320PickBannerPending\(data\)/.test(src), true);

// 5) 데이터 계약 — 배포된 보류일(picked_code=null) 일자파일 표본이 실제로 stocks>0
//    (arrived 신호 유효성: 보류일 stocks=0 이면 arrived 영구 false → 15:21 승격 실패)
const HIST = path.join(REPO, 'data', 'pm320_history');
let holdChecked = 0, holdBad = 0;
try {
  for (const f of fs.readdirSync(HIST)) {
    if (!/^20\d\d-\d\d-\d\d\.json$/.test(f)) continue;
    let d;
    try { d = JSON.parse(fs.readFileSync(path.join(HIST, f), 'utf8')); } catch { continue; }
    if (d && d.picked_code == null && Array.isArray(d.stocks)) {
      holdChecked++;
      if (d.stocks.length === 0) { holdBad++; console.error(`  FAIL: 보류일 ${f} stocks=0 (arrived 영구 false 위험)`); }
    }
  }
} catch { /* 디렉토리 부재(격리 테스트 환경) — 데이터 계약 skip */ }
if (holdChecked > 0) check(`보류일 표본 stocks>0 (검사 ${holdChecked}건)`, holdBad, 0);

const failed = cases.filter((c) => !c.ok);
const suffix = holdChecked ? ` · 보류일 표본 ${holdChecked}건 stocks>0` : '';
console.log(`[PM320 15:21 보류일 화면 확정 회귀] ${cases.length - failed.length}/${cases.length} PASS${suffix}`);
process.exit(failed.length ? 1 : 0);
