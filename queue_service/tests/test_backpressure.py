"""The dynamic-backpressure control law in isolation. No Redis, no Prometheus, no IO.

These run against hand-written integers for the same reason core/state.py's do: the rule that
decides how hard to load the booking service is too important to only ever exercise against a live
stack. Each test names what breaks if the assertion fails.
"""

import unittest

from adapters.prometheus_latency import parse_p99_seconds
from core.backpressure import AIMDConfig, BackpressureController, UnknownEventError, next_rate

# A concrete config to test against. target 0.2s mirrors design.md §13's p99 < 200ms goal.
CFG = AIMDConfig(
    target_p99_seconds=0.2,
    rate_min=10,
    rate_max=600,
    increase_step=50,
    decrease_factor=0.5,
)


class HealthyIncreasesTests(unittest.TestCase):
    def test_below_target_probes_up_by_one_step(self) -> None:
        """Healthy booking service -> reclaim capacity, but only additively. Jumping straight back
        to rate_max is how you re-trigger the overload you just backed off from."""
        self.assertEqual(next_rate(100, 0.15, CFG), 150)

    def test_exactly_at_target_still_increases(self) -> None:
        """p99 == target is 'meeting the SLO', not 'violating it'. Treating equality as overload
        would make the controller back off forever at the very rate it is trying to hold."""
        self.assertEqual(next_rate(100, 0.2, CFG), 150)

    def test_increase_is_clamped_at_rate_max(self) -> None:
        """The operator's ceiling is absolute. A healthy signal must never let the controller admit
        faster than the maximum the booking service was provisioned for."""
        self.assertEqual(next_rate(580, 0.1, CFG), 600)  # 580 + 50 = 630 -> clamp 600
        self.assertEqual(next_rate(600, 0.1, CFG), 600)  # already at the ceiling


class OverloadDecreasesTests(unittest.TestCase):
    def test_above_target_halves_the_rate(self) -> None:
        """Overload -> shed load immediately. Multiplicative, not additive: a slow -50/tick climb-
        down would keep the booking service in violation for many ticks while it drained."""
        self.assertEqual(next_rate(400, 0.5, CFG), 200)

    def test_decrease_floors_rather_than_rounds(self) -> None:
        """On overload we err toward fewer admissions, never more: floor(101 * 0.5) = 50, not 51."""
        self.assertEqual(next_rate(101, 0.5, CFG), 50)

    def test_decrease_never_goes_below_rate_min(self) -> None:
        """rate_min is the trickle that guarantees the queue eventually drains. Even under sustained
        overload the controller must not admit slower than it — floor(10 * 0.5) = 5 clamps to 10.
        (An operator who *wants* overload to fully pause the drop sets rate_min = 0 deliberately.)"""
        self.assertEqual(next_rate(10, 5.0, CFG), 10)
        self.assertEqual(next_rate(15, 5.0, CFG), 10)  # floor(7.5)=7 -> clamp 10


class MissingSignalHoldsTests(unittest.TestCase):
    def test_none_holds_the_current_rate(self) -> None:
        """No p99 (Prometheus down / no data) must NOT be read as healthy. Probing up without a
        signal raises load on the booking service exactly when we have lost sight of it."""
        self.assertEqual(next_rate(300, None, CFG), 300)

    def test_none_still_clamps_to_bounds(self) -> None:
        """Even holding goes through the clamp, so a rate left out of range by a config change
        (e.g. rate_max lowered under it) is corrected rather than frozen out of bounds."""
        self.assertEqual(next_rate(9, None, CFG), 10)  # below min -> min
        self.assertEqual(next_rate(9999, None, CFG), 600)  # above max -> max

    def test_none_does_not_decrease(self) -> None:
        """A monitoring blip is not a booking outage. Holding, not backing off, avoids starving the
        queue for an unrelated Prometheus hiccup."""
        self.assertEqual(next_rate(300, None, CFG), 300)


class ConvergenceTests(unittest.TestCase):
    """AIMD's defining behaviour, exercised as a loop rather than asserted in prose."""

    def test_sustained_overload_converges_to_the_floor(self) -> None:
        """Keep the service overloaded and the rate multiplicatively decays to rate_min and stops —
        it does not undershoot to 0 and freeze the queue."""
        rate = 600
        for _ in range(20):
            rate = next_rate(rate, 1.0, CFG)  # always over target
        self.assertEqual(rate, CFG.rate_min)

    def test_sustained_health_converges_to_the_ceiling(self) -> None:
        """Keep the service healthy and the rate additively climbs to rate_max and stops there."""
        rate = 10
        for _ in range(20):
            rate = next_rate(rate, 0.05, CFG)  # always under target
        self.assertEqual(rate, CFG.rate_max)

    def test_the_aimd_sawtooth(self) -> None:
        """Climb while healthy, then one overload sample halves it — the sawtooth that keeps the
        rate hunting just under the booking service's real ceiling."""
        rate = 100
        rate = next_rate(rate, 0.1, CFG)  # 150
        rate = next_rate(rate, 0.1, CFG)  # 200
        self.assertEqual(rate, 200)
        rate = next_rate(rate, 0.9, CFG)  # overload -> 100
        self.assertEqual(rate, 100)


class BoundsInvariantTests(unittest.TestCase):
    def test_output_is_always_within_bounds(self) -> None:
        """The one invariant that must hold for every input: the controller can never write a rate
        outside [rate_min, rate_max], whatever the current rate or the p99."""
        for rate in (-100, 0, 10, 250, 600, 10_000):
            for p99 in (None, 0.0, 0.05, 0.2, 0.2001, 1.0, 100.0):
                with self.subTest(rate=rate, p99=p99):
                    result = next_rate(rate, p99, CFG)
                    self.assertGreaterEqual(result, CFG.rate_min)
                    self.assertLessEqual(result, CFG.rate_max)


class PrometheusParseTests(unittest.TestCase):
    """The fragile part of the Prometheus adapter: turning a query reply into a float or None. This
    is why parse_p99_seconds is split out of the IO — it can be checked with no network."""

    def _vector(self, value: str) -> dict:
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1.0, value]}]}}

    def test_a_normal_vector_yields_the_float(self) -> None:
        self.assertEqual(parse_p99_seconds(self._vector("0.153")), 0.153)

    def test_empty_result_is_none(self) -> None:
        """No series matched — e.g. no traffic yet. That is 'no signal', not 'p99 is 0'."""
        self.assertIsNone(parse_p99_seconds({"status": "success", "data": {"result": []}}))

    def test_nan_quantile_is_none(self) -> None:
        """histogram_quantile returns NaN when no samples fall in the window. float('NaN') is a real
        truthy float and would sail through as a latency unless caught — the whole point of this
        function existing."""
        self.assertIsNone(parse_p99_seconds(self._vector("NaN")))

    def test_a_failed_query_is_none(self) -> None:
        self.assertIsNone(parse_p99_seconds({"status": "error", "errorType": "bad_data"}))

    def test_a_malformed_reply_is_none(self) -> None:
        self.assertIsNone(parse_p99_seconds({}))
        self.assertIsNone(
            parse_p99_seconds({"status": "success", "data": {"result": [{"metric": {}}]}})
        )


class FakeLatency:
    """Implements LatencySource with scripted p99 values; None once the script runs out."""

    def __init__(self, values: list[float | None]) -> None:
        self._values = list(values)
        self.calls = 0

    async def booking_p99_seconds(self) -> float | None:
        self.calls += 1
        return self._values.pop(0) if self._values else None


class FakeRateStore:
    """Implements RateStore over an in-memory rate. rate=None models an event that does not exist."""

    def __init__(self, rate: int | None) -> None:
        self._rate = rate
        self.writes: list[int] = []

    async def get_rate(self, event_id: str) -> int | None:
        return self._rate

    async def set_rate(self, event_id: str, rate_per_min: int) -> None:
        self._rate = rate_per_min
        self.writes.append(rate_per_min)


class BackpressureControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_signal_writes_an_increased_rate(self) -> None:
        latency, rates = FakeLatency([0.15]), FakeRateStore(100)
        controller = BackpressureController(latency, rates, CFG)

        self.assertEqual(await controller.adjust_once("e"), 150)
        self.assertEqual(rates.writes, [150])

    async def test_overload_signal_writes_a_halved_rate(self) -> None:
        latency, rates = FakeLatency([0.5]), FakeRateStore(400)
        controller = BackpressureController(latency, rates, CFG)

        self.assertEqual(await controller.adjust_once("e"), 200)
        self.assertEqual(rates.writes, [200])

    async def test_a_held_rate_costs_no_write(self) -> None:
        """No signal, and healthy-but-capped, both leave the rate unchanged — and an unchanged rate
        must not churn the config with a pointless HSET every tick."""
        held_none = BackpressureController(FakeLatency([None]), rs1 := FakeRateStore(300), CFG)
        self.assertEqual(await held_none.adjust_once("e"), 300)
        self.assertEqual(rs1.writes, [])

        capped = BackpressureController(FakeLatency([0.1]), rs2 := FakeRateStore(600), CFG)
        self.assertEqual(await capped.adjust_once("e"), 600)  # 600+50 clamps to 600 == current
        self.assertEqual(rs2.writes, [])

    async def test_unknown_event_raises(self) -> None:
        controller = BackpressureController(FakeLatency([0.1]), FakeRateStore(None), CFG)
        with self.assertRaises(UnknownEventError):
            await controller.adjust_once("ghost")

    async def test_unknown_event_never_reads_the_p99(self) -> None:
        """Order matters: if the event does not exist there is nothing to tune, so we must not even
        query Prometheus for it."""
        latency = FakeLatency([0.1])
        controller = BackpressureController(latency, FakeRateStore(None), CFG)
        with self.assertRaises(UnknownEventError):
            await controller.adjust_once("ghost")
        self.assertEqual(latency.calls, 0)

    async def test_the_loop_survives_a_failing_tick(self) -> None:
        """A backpressure loop that dies leaves the rate frozen at whatever it last wrote — possibly
        a value it had just halved during a spike. Ticking uselessly is far better."""
        import asyncio
        import logging

        class ExplodingRates(FakeRateStore):
            def __init__(self) -> None:
                super().__init__(100)
                self.attempts = 0

            async def get_rate(self, event_id: str) -> int | None:
                self.attempts += 1
                if self.attempts < 3:
                    raise ConnectionError("redis went away")
                return 100

        rates = ExplodingRates()
        controller = BackpressureController(FakeLatency([0.15] * 10), rates, CFG)

        logging.getLogger("core.backpressure").setLevel(logging.CRITICAL)
        self.addCleanup(logging.getLogger("core.backpressure").setLevel, logging.NOTSET)

        task = asyncio.create_task(controller.run_forever("e", interval_seconds=0.001))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if rates.attempts >= 4:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        self.assertGreaterEqual(rates.attempts, 4, "loop must survive its failures")


if __name__ == "__main__":
    unittest.main()
