import unittest
from unittest.mock import Mock, patch
from monitor.app import scheduled_cycle
from monitor.settings import Settings
from monitor.store import Store


class RipleyIntervalTests(unittest.TestCase):
    @patch("monitor.app.cycle", return_value=True)
    def test_ripley_due_after_two_minutes_without_affecting_plaza(self, cycle):
        store = Store(":memory:")
        with patch("monitor.store.time.time", return_value=100):
            store.mark_discovery(successful=True, retailer="Ripley")
            store.mark_discovery(successful=True, retailer="Plaza Vea")
            store.mark_discovery(successful=True, retailer="Oechsle")
        with patch("monitor.store.time.time", return_value=219):
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Ripley"
            )
            self.assertFalse(cycle.call_args.kwargs["discover"])
        with patch("monitor.store.time.time", return_value=220):
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Ripley"
            )
            self.assertTrue(cycle.call_args.kwargs["discover"])
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Plaza Vea"
            )
            self.assertFalse(cycle.call_args.kwargs["discover"])
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Oechsle"
            )
            self.assertTrue(cycle.call_args.kwargs["discover"])
        store.close()

    def test_profiles_have_independent_intervals(self):
        s = Settings()
        self.assertEqual(s.for_retailer("ripley").discovery_interval, 120)
        self.assertEqual(s.for_retailer("plazavea").discovery_interval, 14400)
        self.assertEqual(s.for_retailer("oechsle").discovery_interval, 120)
        self.assertEqual(s.discovery_interval, 14400)

    @patch("monitor.settings.load_dotenv")
    def test_config_override(self, dotenv):
        with patch.dict("os.environ", {"RIPLEY_DISCOVERY_INTERVAL": "180"}, clear=True):
            s = Settings.load()
        self.assertEqual(s.for_retailer("ripley").discovery_interval, 180)
        self.assertEqual(s.for_retailer("plazavea").discovery_interval, 14400)

    @patch("monitor.settings.load_dotenv")
    def test_invalid_interval_rejected(self, dotenv):
        with patch.dict("os.environ", {"RIPLEY_DISCOVERY_INTERVAL": "0"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.load()

    def test_failed_discovery_keeps_backoff(self):
        store = Store(":memory:")
        with patch("monitor.store.time.time", return_value=100):
            store.mark_discovery(retailer="Ripley")
        self.assertFalse(
            store.discovery_due(120, 900, timestamp=220, retailer="Ripley")
        )
        self.assertTrue(
            store.discovery_due(120, 900, timestamp=1000, retailer="Ripley")
        )
        store.close()


if __name__ == "__main__":
    unittest.main()
