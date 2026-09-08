"""Configuration tests.

Settings is the single source of truth for defaults, so what matters here is
that the environment is read exactly as documented and that a value which would
break at the first frame is rejected at startup instead.
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from nep2mqtt.config import ConfigError, Settings  # noqa: E402

MINIMAL = {"MQTT_HOST": "broker.local"}


def with_env(**overrides):
    return mock.patch.dict(os.environ, {**MINIMAL, **overrides}, clear=True)


class TestDefaults(unittest.TestCase):
    def test_host_is_the_only_requirement(self):
        with with_env():
            settings = Settings.from_env()
        self.assertEqual(settings.mqtt_host, "broker.local")
        self.assertEqual(settings.mqtt_port, 1883)
        self.assertEqual(settings.mqtt_client_id, "nep2mqtt")
        self.assertEqual(settings.grid_voltage_divisor, 30.0)
        self.assertEqual(settings.energy_wh_per_count, 9.25)

    def test_missing_host_is_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError):
                Settings.from_env()


class TestOverrides(unittest.TestCase):
    def test_every_field_can_be_set(self):
        with with_env(
            MQTT_PORT="8883",
            MQTT_USERNAME="user",
            MQTT_PASSWORD="secret",
            MQTT_CLIENT_ID="solar",
            MQTT_PREFIX="solar",
            HA_DISCOVERY_PREFIX="ha",
            UPSTREAM_HOST="example.invalid",
            UPSTREAM_TIMEOUT_S="2.5",
            ENERGY_WH_PER_COUNT="10",
            GRID_VOLTAGE_DIVISOR="17.32",
            UNPOPULATED_VOLTAGE_V="3",
            LOG_LEVEL="debug",
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.mqtt_port, 8883)
        self.assertEqual(settings.mqtt_client_id, "solar")
        self.assertEqual(settings.upstream_host, "example.invalid")
        self.assertEqual(settings.upstream_timeout_s, 2.5)
        self.assertEqual(settings.energy_wh_per_count, 10.0)
        self.assertEqual(settings.grid_voltage_divisor, 17.32)
        self.assertEqual(settings.unpopulated_voltage_v, 3.0)
        self.assertEqual(settings.log_level, "DEBUG")

    def test_empty_upstream_disables_forwarding(self):
        # An empty value is a choice, not a missing setting.
        with with_env(UPSTREAM_IP=""):
            self.assertIsNone(Settings.from_env().upstream_ip)

    def test_absent_upstream_keeps_the_default(self):
        with with_env():
            self.assertTrue(Settings.from_env().upstream_ip)


class TestValidation(unittest.TestCase):
    def test_non_numeric_port(self):
        with with_env(MQTT_PORT="http"):
            with self.assertRaises(ConfigError):
                Settings.from_env()

    def test_zero_divisor_is_rejected_at_startup(self):
        # Left to run, this would be a division by zero on the first frame,
        # thrown deep in the decoder instead of at boot where it is readable.
        for name in ("GRID_VOLTAGE_DIVISOR", "ENERGY_WH_PER_COUNT"):
            with self.subTest(variable=name):
                with with_env(**{name: "0"}):
                    with self.assertRaises(ConfigError):
                        Settings.from_env()

    def test_negative_timeout_is_rejected(self):
        with with_env(UPSTREAM_TIMEOUT_S="-1"):
            with self.assertRaises(ConfigError):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main(verbosity=2)
