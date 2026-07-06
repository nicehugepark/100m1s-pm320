"""
단위 테스트: live tail balance 연속성 + sanity assertion (d) 검증
FLR-20260629-TEC-001 / DOC-20260626-FLR-001 동형 변종 구조적 재발 차단.

테스트 케이스:
  - PASS: live 7건 저장 후 8번째 live append 시 마지막 live 행 기준 누적 정상
  - FAIL: backtest 마지막 기준으로 오염된 balance 가 들어오면 assertion(d) 가 FAIL
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import unittest
from unittest.mock import patch

import build_card_history as bch

# ---------------------------------------------------------------------------
# 테스트용 최소 픽 fixture
# ---------------------------------------------------------------------------

_DISPLAY_START = 10_000_000.0
_WEIGHT = 0.1  # per-pick weight (투입비중)

# backtest 마지막 행 balance_after (원익IPS)
_BACKTEST_LAST_BAL = 10_954_305.86

# live 7건의 정상 balance chain (알테오젠이 마지막)
_LIVE_ROWS = [
    {
        "code": "098460",
        "name": "고영",
        "date": "2026-06-12",
        "exit_date": "2026-06-15",
        "ret_pct": 3.2,
        "pnl": 29_211.49,
        "balance_after": 10_983_517.35,
        "watered": False,
        "settlement_order": 1,
        "_live": True,
    },
    {
        "code": "475150",
        "name": "SK이터닉스",
        "date": "2026-06-16",
        "exit_date": "2026-06-17",
        "ret_pct": 3.2,
        "pnl": 58_578.77,
        "balance_after": 11_042_096.12,
        "watered": True,
        "settlement_order": 2,
        "_live": True,
    },
    {
        "code": "347850",
        "name": "디앤디파마텍",
        "date": "2026-06-17",
        "exit_date": "2026-06-18",
        "ret_pct": 3.2,
        "pnl": 29_445.59,
        "balance_after": 11_071_541.71,
        "watered": False,
        "settlement_order": 3,
        "_live": True,
    },
    {
        "code": "067290",
        "name": "JW신약",
        "date": "2026-06-19",
        "exit_date": "2026-06-22",
        "ret_pct": 3.2,
        "pnl": 29_524.11,
        "balance_after": 11_101_065.82,
        "watered": False,
        "settlement_order": 4,
        "_live": True,
    },
    {
        "code": "290690",
        "name": "소룩스",
        "date": "2026-06-24",
        "exit_date": "2026-06-25",
        "ret_pct": 3.2,
        "pnl": 29_602.85,
        "balance_after": 11_130_668.67,
        "watered": False,
        "settlement_order": 5,
        "_live": True,
    },
    {
        "code": "001820",
        "name": "삼화콘덴서",
        "date": "2026-06-18",
        "exit_date": "2026-06-26",
        "ret_pct": -26.72,
        "pnl": -495_685.84,
        "balance_after": 10_634_982.83,
        "watered": True,
        "settlement_order": 6,
        "_live": True,
    },
    {
        "code": "196170",
        "name": "알테오젠",
        "date": "2026-06-25",
        "exit_date": "2026-06-26",
        "ret_pct": 3.2,
        "pnl": 28_359.96,
        "balance_after": 10_663_342.79,
        "watered": False,
        "settlement_order": 7,
        "_live": True,
    },
]

# backtest 마지막 행 (원익IPS, settlement_order 없음 = None)
_BACKTEST_LAST_ROW = {
    "code": "240810",
    "name": "원익IPS",
    "date": "2026-06-11",
    "exit_date": "2026-06-11",
    "ret_pct": 3.2,
    "pnl": 29_133.79,
    "balance_after": _BACKTEST_LAST_BAL,
    "watered": False,
    "_settle_seq": 81,
    # settlement_order 없음 (None) — backtest 행 특성
}


def _make_merged_with_live(live_rows, extra_live=None):
    """live 행들을 담은 merged dict 생성 (sanity_check_merged 입력용)."""
    all_live = list(live_rows)
    if extra_live:
        all_live.append(extra_live)
    table = all_live + [_BACKTEST_LAST_ROW]
    return {
        "backtest_detail": {
            "table": table,
            "equity_curve": [],
        }
    }


class TestBalanceContinuity(unittest.TestCase):
    """live tail balance 연속성 단위 테스트."""

    # ------------------------------------------------------------------
    # PASS 케이스: 정상 live chain — assertion(d) 오류 0건
    # ------------------------------------------------------------------
    def test_normal_live_chain_passes_assertion_d(self):
        """live 7건이 정상 연속일 때 sanity assertion(d) 가 FAIL 없이 PASS."""
        merged = _make_merged_with_live(_LIVE_ROWS)
        errors = bch._sanity_check_merged(merged, _WEIGHT, _DISPLAY_START)
        d_errors = [e for e in errors if e.startswith("(d)")]
        self.assertEqual(
            d_errors,
            [],
            f"정상 live chain 에서 (d) 오류가 나오면 안 됨: {d_errors}",
        )

    # ------------------------------------------------------------------
    # PASS 케이스: 8번째 live 정상 append — balance 기준이 so=7(알테오젠) 기준
    # ------------------------------------------------------------------
    def test_eighth_live_correct_base_no_assertion_error(self):
        """8번째 live 행이 so=7(알테오젠 10,663,342.79) 기준으로 누적될 때 PASS."""
        # so=7 알테오젠 balance_after=10,663,342.79 기준에서 pnl=28,000 추가
        eighth = {
            "code": "089970",
            "name": "브이엠",
            "date": "2026-06-26",
            "exit_date": "2026-06-27",
            "ret_pct": 3.2,
            "pnl": 28_435.58,
            "balance_after": round(10_663_342.79 + 28_435.58, 2),  # 10,691,778.37
            "watered": False,
            "settlement_order": 8,
            "_live": True,
        }
        merged = _make_merged_with_live(_LIVE_ROWS, extra_live=eighth)
        errors = bch._sanity_check_merged(merged, _WEIGHT, _DISPLAY_START)
        d_errors = [e for e in errors if e.startswith("(d)")]
        self.assertEqual(
            d_errors,
            [],
            f"정상 8번째 live append 에서 (d) 오류가 나오면 안 됨: {d_errors}",
        )
        # balance_after 값 자체도 검증
        self.assertAlmostEqual(
            eighth["balance_after"],
            10_691_778.37,
            places=1,
            msg="8번째 balance_after 가 알테오젠 기준 누적값과 불일치",
        )

    # ------------------------------------------------------------------
    # FAIL 케이스: backtest 마지막 기준으로 오염된 balance 가 들어오면 assertion(d) FAIL
    # ------------------------------------------------------------------
    def test_corrupted_base_triggers_assertion_d(self):
        """backtest 마지막 행(원익IPS 10,954,305.86) 기준으로 계산된 8번째 live 행은
        assertion(d) 가 FAIL 해야 한다.
        이번 버그의 재발을 구조적으로 탐지하는 핵심 가드."""
        # 버그 상태: backtest 마지막(10,954,305.86) 기준으로 pnl 산출
        pnl_corrupted = round(10_954_305.86 * _WEIGHT * (3.2 / 100.0), 2)  # ≈ 35,053.58
        corrupted_eighth = {
            "code": "089970",
            "name": "브이엠",
            "date": "2026-06-26",
            "exit_date": "2026-06-27",
            "ret_pct": 3.2,
            "pnl": pnl_corrupted,
            "balance_after": round(10_954_305.86 + pnl_corrupted, 2),
            "watered": False,
            "settlement_order": 8,
            "_live": True,
        }
        merged = _make_merged_with_live(_LIVE_ROWS, extra_live=corrupted_eighth)
        errors = bch._sanity_check_merged(merged, _WEIGHT, _DISPLAY_START)
        d_errors = [e for e in errors if e.startswith("(d)")]
        self.assertGreater(
            len(d_errors),
            0,
            "오염된 balance 에서 (d) 오류가 나와야 하는데 PASS 됨 — assertion 가드 실패",
        )

    # ------------------------------------------------------------------
    # _merge_live_tail 내 balance 초기값 fix 검증:
    # live 행이 있을 때 마지막 live 행(so 최대값) 기준 사용
    # ------------------------------------------------------------------
    def test_balance_initial_value_uses_last_live_row(self):
        """_merge_live_tail 의 balance 초기값이 live 마지막 행(so=7, 알테오젠) 기준임을 검증.

        live 7건 이미 저장 + 8번째 신규 live 청산 시뮬레이션.
        seen_keys 로 1~7건이 skip 되고 8번째만 추가될 때
        balance 가 알테오젠(10,663,342.79) 기준으로 산출되는지 확인.

        NOTE: 실제 summary.json 에 의존하지 않고 _LIVE_ROWS(live 7건) fixture 로
        동일 시나리오를 재현(파일 상태 독립성 확보).
        """
        # live 7건(알테오젠 so=7 마지막) + backtest 1건으로 구성된 fixture base
        fixture_table = list(_LIVE_ROWS) + [_BACKTEST_LAST_ROW]
        fixture_curve = [
            {
                "date": r.get("exit_date") or r.get("date"),
                "balance": r["balance_after"],
                "name": r["name"],
            }
            for r in _LIVE_ROWS
        ]
        fixture_summary = {
            "generated_at": "2026-06-26T15:20:00+09:00",
            "since": "2026-04-08",
            "first_pick_date": "2026-04-08",
            "last_settled_date": "2026-06-26",
            "total_picks": 48,
            "settled": 48,
            "running": 0,
            "take_profit": 46,
            "expired_loss": 2,
            "expired_gain": 0,
            "win_rate": 95.8,
            "start_balance": 10_000_000,
            "source_start_balance": 12_000_000.0,
            "balance_scale": 0.83333333,
            "final_balance": 10_663_342.79,
            "total_pnl": 663_342.79,
            "total_return_pct": 6.6334,
            "_live_tail_appended": 7,
            "_backtest_baseline_running": 0,
            "backtest_detail": {
                "source": "backtest_ssot",
                "as_of": "2026-06-26",
                "table": fixture_table,
                "equity_curve": fixture_curve,
            },
        }

        # 8번째 신규 live 픽(브이엠) — fixture 내 089970 exit_date=2026-06-26 이 없으므로 미중복
        new_live_pick = {
            "state": "taken_profit",
            "date": "2026-06-26",
            "exit_date": "2026-06-29",
            "name": "브이엠",
            "code": "089970",
            "pick": {
                "entry_price": 107_600,
                "result": {
                    "final_pnl_pct": 3.2,
                    "watered": False,
                    "same_day_afterhours": False,
                },
            },
        }

        def mock_live_settled(cutoff, seen_keys):
            # 브이엠은 fixture seen_keys 에 없으므로 반환
            return [new_live_pick]

        def mock_live_running(cutoff):
            return 0

        with (
            patch.object(
                bch, "_live_settled_picks_after", side_effect=mock_live_settled
            ),
            patch.object(
                bch, "_live_running_picks_after", side_effect=mock_live_running
            ),
        ):
            result = bch._merge_live_tail(fixture_summary)

        # fix 후: balance 시작 = 알테오젠(so=7) balance_after = 10,663,342.79
        # pnl = 10,663,342.79 * 0.1 * 1.0 * (3.2/100) ≈ 29,211.49
        # final_balance = 10,663,342.79 + ~29,211 ≈ 10,692,554
        # backtest 기준(10,954,305.86)이면 final_balance 가 훨씬 크게 됨(~10,984,000)
        self.assertIsNotNone(result)
        final_bal = result.get("final_balance", 0)
        self.assertLess(
            final_bal,
            10_700_000,
            f"final_balance={final_bal:.2f} 가 10,700,000 초과 — backtest base 오염 의심 (알테오젠 기준이면 ~10,692,554)",
        )
        self.assertGreater(
            final_bal,
            10_660_000,
            f"final_balance={final_bal:.2f} 가 너무 낮음 — balance 초기값 오류",
        )


class TestPnlPrecision(unittest.TestCase):
    """pnl 정확도 검증 — 범위 검증이 아닌 값 자체 검증.

    FLR-AGT-002 정답값 수동 삽입 변종 차단:
    d67fcc75가 pnl=29,211.49(수동)를 박았을 때 범위 테스트(<10,700,000)를
    통과한 사고 재발 방지. 실제 코드 weight(역산 = 대한광통신 픽 기준 1/12)로
    pnl 값 자체를 검증한다.
    """

    # 실제 코드 weight: 대한광통신(대한광통신 backtest 첫 비워터 픽)에서 역산
    # prev=10,000,000, bal=10,026,666.67, ret=3.2%
    # growth = 10,026,666.67 / 10,000,000 - 1 = 0.0026666...
    # weight = growth / 0.032 = 0.08333334
    _REAL_WEIGHT = round((10_026_666.67 / 10_000_000.0 - 1.0) / (3.2 / 100.0), 8)

    def test_vm_pnl_exact_value_real_weight(self):
        """브이엠(089970) pnl이 실제 코드 weight(≈1/12)로 28,435.58이 됨을 검증.

        수동 삽입값 29,211.49(weight=0.1 오산)와 명시 구분:
        - 29,211.49 = 10,663,342.79 × 0.1 × 0.032  (틀린 weight)
        - 28,435.58 = 10,663,342.79 × 0.08333334 × 0.032  (코드 역산 weight, 정답)
        """
        balance = 10_663_342.79  # 알테오젠(so=7) balance_after
        ret = 3.2
        watered = False
        invested = balance * self._REAL_WEIGHT * (2.0 if watered else 1.0)
        pnl = round(invested * (ret / 100.0), 2)
        balance_after = round(balance + pnl, 2)

        self.assertAlmostEqual(
            pnl,
            28_435.58,
            places=2,
            msg=(
                f"pnl={pnl} 가 정답 28,435.58과 불일치. "
                f"weight={self._REAL_WEIGHT:.8f}, invested={invested:.2f}. "
                "29,211.49는 weight=0.1(오염) 산물이므로 정답 아님."
            ),
        )
        self.assertAlmostEqual(
            balance_after,
            10_691_778.37,
            places=2,
            msg=f"balance_after={balance_after} 가 정답 10,691,778.37과 불일치",
        )

    def test_vm_pnl_not_manual_value(self):
        """수동 삽입값 29,211.49가 코드 경로 산출값이 아님을 명시 검증.

        29,211.49 = 고영(098460) so=1의 pnl과 동일 — 서로 다른 픽이 동일 pnl이면
        weight 오염(투입금액이 같을 때만 가능) 신호다.
        """
        # 고영(so=1) balance_before = start_balance = 10,000,000 (첫 live 픽)
        # 그러나 실제 so=1 고영은 balance_after=10,983,517.35, pnl=29,211.49
        # 이는 b0f33983 시점 _WEIGHT=0.1 오산 (실제 코드 역산 weight != 0.1)
        corrupted_pnl = 29_211.49  # 수동 삽입·weight=0.1 오염값

        # 실제 코드 weight로 재계산하면 다른 값이 나와야 함
        balance = 10_663_342.79
        actual_pnl = round(balance * self._REAL_WEIGHT * (3.2 / 100.0), 2)

        self.assertNotAlmostEqual(
            actual_pnl,
            corrupted_pnl,
            places=2,
            msg=(
                f"코드 산출 pnl({actual_pnl})이 수동 삽입값({corrupted_pnl})과 같으면 안 됨 "
                "— weight 오염 미탐지 상태"
            ),
        )

    def test_identity_chain_vm_so8(self):
        """항등식: balance[so=8] = balance[so=7] + pnl[so=8] (±1원)."""
        balance_so7 = 10_663_342.79  # 알테오젠
        pnl_so8 = 28_435.58  # 브이엠 코드 산출
        expected_balance_so8 = 10_691_778.37  # 코드 산출 final

        computed = round(balance_so7 + pnl_so8, 2)
        self.assertAlmostEqual(
            computed,
            expected_balance_so8,
            delta=1.0,
            msg=(
                f"항등식 실패: {balance_so7} + {pnl_so8} = {computed} "
                f"≠ {expected_balance_so8}"
            ),
        )

    def test_merge_live_tail_vm_pnl_real_weight(self):
        """_merge_live_tail이 브이엠을 실제 코드 weight로 산출함을 end-to-end 검증.

        _LIVE_ROWS fixture의 역산 weight(대한광통신 픽 기준)는 ≈0.08333334.
        브이엠 pnl이 28,435.58이어야 하며 29,211.49(weight=0.1 오염)가 아님.
        """
        list(_LIVE_ROWS) + [_BACKTEST_LAST_ROW]
        # weight 역산이 _LIVE_ROWS가 아니라 backtest 비워터 픽에서 나오도록
        # fixture_table에 실제 대한광통신 픽 추가
        backtest_kt_row = {
            "code": "010170",
            "name": "대한광통신",
            "date": "2026-04-08",
            "exit_date": "2026-04-10",
            "ret_pct": 3.2,
            "pnl": 26_666.67,
            "balance_after": 10_026_666.67,
            "watered": False,
            "same_day_afterhours": False,
            "_settle_seq": 2,
        }
        fixture_table_with_kt = (
            [backtest_kt_row] + list(_LIVE_ROWS) + [_BACKTEST_LAST_ROW]
        )

        fixture_curve = [
            {
                "date": r.get("exit_date") or r.get("date"),
                "balance": r["balance_after"],
                "name": r["name"],
            }
            for r in _LIVE_ROWS
        ]
        fixture_summary = {
            "generated_at": "2026-06-26T15:20:00+09:00",
            "since": "2026-04-08",
            "first_pick_date": "2026-04-08",
            "last_settled_date": "2026-06-26",
            "total_picks": 48,
            "settled": 48,
            "running": 0,
            "take_profit": 46,
            "expired_loss": 2,
            "expired_gain": 0,
            "win_rate": 95.8,
            "start_balance": 10_000_000,
            "source_start_balance": 12_000_000.0,
            "balance_scale": 0.83333333,
            "final_balance": 10_663_342.79,
            "total_pnl": 663_342.79,
            "total_return_pct": 6.6334,
            "_source": "backtest_ssot",
            "_live_tail_appended": 7,
            "_backtest_baseline_running": 0,
            "backtest_detail": {
                "source": "backtest_ssot",
                "as_of": "2026-06-26",
                "table": fixture_table_with_kt,
                "equity_curve": fixture_curve,
            },
        }

        new_live_pick = {
            "state": "taken_profit",
            "date": "2026-06-26",
            "exit_date": "2026-06-29",
            "name": "브이엠",
            "code": "089970",
            "pick": {
                "entry_price": 107_600,
                "result": {
                    "final_pnl_pct": 3.2,
                    "watered": False,
                    "same_day_afterhours": False,
                    "result_date": "2026-06-29",
                },
            },
        }

        def mock_live_settled(cutoff, seen_keys):
            return [new_live_pick]

        def mock_live_running(cutoff):
            return 0

        with (
            patch.object(
                bch, "_live_settled_picks_after", side_effect=mock_live_settled
            ),
            patch.object(
                bch, "_live_running_picks_after", side_effect=mock_live_running
            ),
        ):
            result = bch._merge_live_tail(fixture_summary)

        self.assertIsNotNone(result, "merge 결과가 None — live tail merge 실패")

        table = result.get("backtest_detail", {}).get("table", [])
        vm_rows = [r for r in table if r.get("code") == "089970" and r.get("_live")]
        self.assertEqual(
            len(vm_rows), 1, f"브이엠 live 행이 1개여야 함: {len(vm_rows)}개"
        )

        vm = vm_rows[0]
        self.assertAlmostEqual(
            vm["pnl"],
            28_435.58,
            places=2,
            msg=(
                f"브이엠 pnl={vm['pnl']} 가 코드 산출값 28,435.58과 불일치. "
                "29,211.49는 weight=0.1(오염) 산물 — 수동 삽입 변종 탐지"
            ),
        )
        self.assertAlmostEqual(
            vm["balance_after"],
            10_691_778.37,
            places=2,
            msg=f"브이엠 balance_after={vm['balance_after']} 가 10,691,778.37과 불일치",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
