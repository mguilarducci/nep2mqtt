"""Decoder tests driven by packets captured from three BDM-2250 units.

The fixtures under tests/fixtures/ are bytes off real hardware, including one
unit whose last two channels have no panel wired to them.

Serial numbers have been replaced with synthetic ones (AA0000xx) and the
checksums recomputed, so the frames remain byte-valid without carrying the
identity of anyone's equipment. One sample per unit rather than a series, so
they carry no generation curve either. Everything that gives these fixtures
their worth is untouched: the real frame structure, the real physical readings,
and the cross-sums that tie the channels to the totals.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from nep2mqtt.protocol import ProtocolError, decode  # noqa: E402

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.bin"))


def load(name):
    return (FIXTURE_DIR / name).read_bytes()


class TestRealFixtures(unittest.TestCase):
    def test_fixtures_are_present(self):
        self.assertEqual(len(FIXTURES), 3)

    def test_every_fixture_decodes(self):
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                self.assertIsNotNone(decode(path.read_bytes()))

    def test_checksums_pass_on_every_fixture(self):
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                self.assertTrue(decode(path.read_bytes())["checksum_ok"])

    def test_expected_serials(self):
        seen = {decode(p.read_bytes())["serial"] for p in FIXTURES}
        self.assertEqual(seen, {"AA000001", "AA000002", "AA000003"})


class TestKnownValues(unittest.TestCase):
    """Values checked by hand against unit A."""

    def setUp(self):
        self.reading = decode(load("unit-a-4ch.bin"))

    def test_serial(self):
        self.assertEqual(self.reading["serial"], "AA000001")

    def test_total_power(self):
        self.assertAlmostEqual(self.reading["power_w"], 775.7, places=1)

    def test_frequency(self):
        self.assertAlmostEqual(self.reading["frequency_hz"], 59.98, places=2)

    def test_temperature(self):
        self.assertAlmostEqual(self.reading["temperature_c"], 42.56, places=2)

    def test_grid_voltage_phase_to_neutral(self):
        self.assertAlmostEqual(self.reading["grid_voltage_v"], 126.93, places=2)

    def test_four_channels(self):
        self.assertEqual(len(self.reading["channels"]), 4)

    def test_first_channel(self):
        channel = self.reading["channels"][0]
        self.assertAlmostEqual(channel["voltage_v"], 30.33, places=2)
        self.assertAlmostEqual(channel["power_w"], 172.8, places=1)


class TestInvariants(unittest.TestCase):
    """Relations that hold on every valid frame - the real safety net."""

    def test_channel_power_sums_to_the_total(self):
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                reading = decode(path.read_bytes())
                total = sum(c["power_w"] for c in reading["channels"])
                # The inverter itself disagrees by up to ~2 raw counts (0.2 W)
                # between the total and the sum, and rounding four channels to
                # one decimal adds another ~0.25 W.
                self.assertAlmostEqual(total, reading["power_w"], delta=0.5)

    def test_channel_energy_sums_to_the_total(self):
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                reading = decode(path.read_bytes())
                total = sum(c["energy_raw"] for c in reading["channels"])
                self.assertAlmostEqual(
                    total, reading["energy_today_raw"], delta=2
                )

    def test_frequency_is_plausible(self):
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                frequency = decode(path.read_bytes())["frequency_hz"]
                self.assertTrue(45 < frequency < 65)


class TestUnpopulatedChannels(unittest.TestCase):
    """Unit C only has two panels: channels 3 and 4 read a steady ~0.93 V."""

    def setUp(self):
        self.reading = decode(load("unit-c-2ch.bin"))

    def test_two_channels_producing(self):
        live = [c for c in self.reading["channels"] if c["power_w"] > 0]
        self.assertEqual(len(live), 2)

    def test_empty_channels_are_flagged(self):
        flags = [c["unpopulated"] for c in self.reading["channels"]]
        self.assertEqual(flags, [False, False, True, True])

    def test_empty_channel_reports_zero_power(self):
        self.assertEqual(self.reading["channels"][3]["power_w"], 0.0)


class TestConfigurableScales(unittest.TestCase):
    """The two unconfirmed scales are arguments, so both conventions work."""

    def setUp(self):
        self.frame = load("unit-a-4ch.bin")

    def test_phase_to_phase_convention(self):
        # Same raw reading, the other convention: 30 -> ~127 V phase-to-neutral,
        # 17.32 (10*sqrt(3)) -> ~220 V phase-to-phase.
        reading = decode(self.frame, grid_voltage_divisor=17.32)
        self.assertAlmostEqual(reading["grid_voltage_v"], 219.86, places=1)

    def test_threshold_decides_what_counts_as_empty(self):
        # A 4-panel unit: nothing is empty at the default threshold.
        default = decode(self.frame)
        self.assertFalse(any(c["unpopulated"] for c in default["channels"]))
        # Raise it above the real panel voltage and everything reads empty,
        # which is what makes the threshold worth exposing for other models.
        absurd = decode(self.frame, unpopulated_voltage_v=100.0)
        self.assertTrue(all(c["unpopulated"] for c in absurd["channels"]))


class TestInvalidInput(unittest.TestCase):
    def test_too_short(self):
        with self.assertRaises(ProtocolError):
            decode(b"\x79" * 10)

    def test_wrong_start_byte(self):
        frame = bytearray(load("unit-a-4ch.bin"))
        frame[0] = 0x7A
        with self.assertRaises(ProtocolError):
            decode(bytes(frame))

    def test_empty_body(self):
        with self.assertRaises(ProtocolError):
            decode(b"")

    def test_corrupted_checksum_is_reported(self):
        frame = bytearray(load("unit-a-4ch.bin"))
        frame[30] ^= 0xFF
        self.assertFalse(decode(bytes(frame))["checksum_ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
