import unittest

from nupson.state_machine import PowerState, RecoveryMachine


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RecoveryMachineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.machine = RecoveryMachine(900, 40, clock=self.clock)

    def test_full_outage_recovery_sequence(self):
        transition = self.machine.observe("OB DISCHRG", 80)
        self.assertEqual(transition.current, PowerState.OUTAGE)
        transition = self.machine.observe("OL CHRG", 30)
        self.assertEqual(transition.current, PowerState.RECOVERY_PENDING)
        self.clock.now = 899
        self.assertIsNone(self.machine.observe("OL CHRG", 100))
        self.assertEqual(self.machine.remaining_seconds, 1)
        self.clock.now = 900
        transition = self.machine.observe("OL CHRG", 39)
        self.assertEqual(transition.current, PowerState.WAITING_FOR_CHARGE)
        transition = self.machine.observe("OL CHRG", 40)
        self.assertEqual(transition.current, PowerState.WAKING)
        self.assertEqual(self.machine.complete().current, PowerState.NORMAL)

    def test_brief_power_return_resets_timer(self):
        self.machine.observe("OB", 90)
        self.machine.observe("OL", 90)
        self.clock.now = 600
        transition = self.machine.observe("OB", 88)
        self.assertEqual(transition.current, PowerState.OUTAGE)
        self.assertIsNone(self.machine.remaining_seconds)
        self.machine.observe("OL", 88)
        self.clock.now = 1499
        self.assertEqual(self.machine.remaining_seconds, 1)

    def test_communication_loss_cancels_recovery_and_waking(self):
        self.machine.observe("OB", 90)
        self.machine.observe("OL", 90)
        transition = self.machine.observe("COMMLOST", None)
        self.assertEqual(transition.current, PowerState.OUTAGE)

        self.machine.observe("OL", 90)
        self.clock.now += 900
        self.machine.observe("OL", 90)
        self.assertEqual(self.machine.state, PowerState.WAKING)
        transition = self.machine.observe("STALE", 90)
        self.assertEqual(transition.current, PowerState.OUTAGE)

    def test_startup_online_does_not_create_wake_cycle(self):
        self.assertIsNone(self.machine.observe("OL CHRG", 100))
        self.clock.now = 10_000
        self.assertIsNone(self.machine.observe("OL", 100))
        self.assertEqual(self.machine.state, PowerState.NORMAL)

    def test_power_loss_while_waiting_for_charge_restarts_recovery(self):
        self.machine.observe("OB", 50)
        self.machine.observe("OL CHRG", 30)
        self.clock.now = 900
        self.machine.observe("OL CHRG", 30)
        self.assertEqual(self.machine.state, PowerState.WAITING_FOR_CHARGE)
        transition = self.machine.observe("OB DISCHRG", 29)
        self.assertEqual(transition.current, PowerState.OUTAGE)
        self.machine.observe("OL CHRG", 29)
        self.assertEqual(self.machine.remaining_seconds, 900)

    def test_missing_charge_does_not_bypass_configured_threshold(self):
        self.machine.observe("OB", 50)
        self.machine.observe("OL", None)
        self.clock.now = 900
        transition = self.machine.observe("OL", None)
        self.assertEqual(transition.current, PowerState.WAITING_FOR_CHARGE)

    def test_recovery_restored_after_restart_starts_full_timer_without_transition(self):
        machine = RecoveryMachine(
            900,
            40,
            initial_state=PowerState.RECOVERY_PENDING,
            outage_id=12,
            clock=self.clock,
        )
        self.assertEqual(machine.remaining_seconds, 900)
        self.assertIsNone(machine.observe("OL CHRG", 90))
        self.clock.now = 899
        self.assertEqual(machine.remaining_seconds, 1)


if __name__ == "__main__":
    unittest.main()
