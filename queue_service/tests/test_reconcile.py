"""Phase 11: the position clamp (FR-7) and that reconciliation restores the authoritative rank.

Pure `core/` arithmetic — no Redis, no Django, no event loop. The reconciliation timing itself is
plumbing; what matters, and what these pin, is that a correction can never be SHOWN moving a
position up, and that re-pinning from a ZRANK reading makes the cheap arithmetic agree with it.
"""

import unittest

from core.state import clamp_position, position_from


class ClampTests(unittest.TestCase):
    def test_a_correction_that_would_raise_the_position_is_clamped_down(self) -> None:
        self.assertEqual(clamp_position(computed=8, last_shown=5), 5)

    def test_a_correction_that_lowers_the_position_is_shown(self) -> None:
        self.assertEqual(clamp_position(computed=3, last_shown=5), 3)

    def test_equal_is_unchanged(self) -> None:
        self.assertEqual(clamp_position(computed=5, last_shown=5), 5)


class ReconcileArithmeticTests(unittest.TestCase):
    def test_repinning_from_the_authoritative_rank_restores_it(self) -> None:
        # Drift: 3 people ahead abandoned, so the cheap arithmetic reads 3 too high.
        admitted = 100
        true_rank = 5  # what ZRANK actually reports
        drifted_sequence = 108  # arithmetic: position = 108 - 100 = 8 (3 too high)
        self.assertEqual(position_from(drifted_sequence, admitted), 8)

        # Reconciliation re-pins the sequence from the authoritative rank, and the arithmetic agrees.
        repinned = true_rank + admitted
        self.assertEqual(position_from(repinned, admitted), true_rank)

    def test_arithmetic_only_decreases_as_admitted_grows(self) -> None:
        seq = 50
        positions = [position_from(seq, admitted) for admitted in range(0, 60, 5)]
        self.assertEqual(positions, sorted(positions, reverse=True))  # monotonic, non-increasing
