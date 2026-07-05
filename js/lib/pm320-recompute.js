/* ───── lib/pm320-recompute.js — PM320 매매 기준가 recompute 단일 SSOT ─────
   R44 #2 (조니 2심 확정, 2026-06-12) — renderer.js 2곳(_pm320DetailRows / _buildPm320TodayRecCard)에
   중복돼 있던 정정일(authClose ≠ entry_price) 재계산 식을 단일 함수로 통합.
   종전 `(P0 + 2*P0*0.936)/3` 은 물타기 2배 가정 하드코딩 — 라이브 데이터(물타기 1배,
   watering_weight "첫 매수와 동일 수량(1배)")와 모순 (검산: (138,200+129,355)/2×1.032=138,058).
   본 식 = build_card_history.py L986-989 verbatim:
     avg_after_watering = (P0 + W·P0·0.936) / (1+W) / tpAfter = round(avg × 1.032)
   W(물타기 비중)는 데이터 pk.watering_weight 라벨("…N배")에서 파싱 — 하드코딩 0 (FLR-AGT-002).
   IIFE + window 전역 등록 (format.js 동형) + CommonJS export (node 회귀 테스트용).
*/
(function (root) {
  'use strict';

  // send_kakao_message.py / build_card_history.py 와 동일 비율 (전략 상수 이관은 별건 — 유료화 트랙)
  var WATERING_RATIO = 0.936;
  var TAKE_PROFIT_RATIO = 1.032;

  // 물타기 비중 수치 — 데이터 라벨("첫 매수와 동일 수량(1배)" / "첫 매수의 2배")에서 "N배" 파싱.
  // 라벨 부재/비파싱 시 1 (현행 프로파일 디폴트 = build_card_history.py L33 동일).
  function wateringWeightNum(pk) {
    var m = String((pk && pk.watering_weight) || '').match(/(\d+(?:\.\d+)?)\s*배/);
    var w = m ? parseFloat(m[1]) : NaN;
    return (isFinite(w) && w > 0) ? w : 1;
  }

  // 물타기 비중 표기 라벨 — 데이터 verbatim 우선, 부재 시 디폴트(1배) 라벨.
  // 종전 폴백 '첫 매수의 2배' 하드코딩은 라이브 1배 데이터와 모순이라 폐기 (R44 #2).
  function wateringWeightLabel(pk) {
    if (pk && pk.watering_weight) return pk.watering_weight;
    var w = wateringWeightNum(pk);
    return w === 1 ? '첫 매수와 동일 수량(1배)' : ('첫 매수의 ' + w + '배');
  }

  // 매매 기준가 3종 산출 — Q-20260606-111 SSOT 우선순위 그대로:
  //   authClose(카드 마감 종가) 유효 + entry_price 와 다름(정정일) → authClose 기반 재계산.
  //   그 외 → 저장값 passthrough (graceful, 추정 0).
  function targets(pk, authClose) {
    if (!pk) return { p0: null, watering: null, tp: null, tpAfter: null, recomputed: false };
    var hasAuth = (typeof authClose === 'number' && authClose > 0);
    var p0 = hasAuth ? authClose : pk.entry_price;
    var recomputed = hasAuth && authClose !== pk.entry_price;
    if (!recomputed) {
      return {
        p0: p0,
        watering: pk.watering_target_price,
        tp: pk.take_profit_target_price,
        tpAfter: pk.take_profit_after_watering_price,
        recomputed: false
      };
    }
    var w = wateringWeightNum(pk);
    return {
      p0: p0,
      watering: Math.round(p0 * WATERING_RATIO),
      tp: Math.round(p0 * TAKE_PROFIT_RATIO),
      // build_card_history.py L986-989 verbatim — 가중 산술평균 (물타기 W배, 총 1+W 유닛)
      tpAfter: Math.round(((p0 + w * p0 * WATERING_RATIO) / (1 + w)) * TAKE_PROFIT_RATIO),
      recomputed: true
    };
  }

  var api = {
    WATERING_RATIO: WATERING_RATIO,
    TAKE_PROFIT_RATIO: TAKE_PROFIT_RATIO,
    wateringWeightNum: wateringWeightNum,
    wateringWeightLabel: wateringWeightLabel,
    targets: targets
  };

  root.pm320Recompute = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
