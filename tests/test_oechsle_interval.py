import unittest
from unittest.mock import Mock, patch
from monitor.app import scheduled_cycle
from monitor.settings import Settings
from monitor.store import Store


class OechsleIntervalTests(unittest.TestCase):
    @patch("monitor.app.cycle", return_value=True)
    def test_oechsle_every_two_minutes_and_plaza_unchanged(self, cycle):
        store = Store(":memory:")
        with patch("monitor.store.time.time", return_value=100):
            for retailer in ("Oechsle", "Plaza Vea", "Ripley"):
                store.mark_discovery(successful=True, retailer=retailer)
        with patch("monitor.store.time.time", return_value=219):
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Oechsle"
            )
            self.assertFalse(cycle.call_args.kwargs["discover"])
        with patch("monitor.store.time.time", return_value=220):
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Oechsle"
            )
            self.assertTrue(cycle.call_args.kwargs["discover"])
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Plaza Vea"
            )
            self.assertFalse(cycle.call_args.kwargs["discover"])
        store.close()

    @patch("monitor.settings.load_dotenv")
    def test_custom_interval_does_not_change_plaza_or_ripley(self, dotenv):
        with patch.dict(
            "os.environ", {"OECHSLE_DISCOVERY_INTERVAL": "300"}, clear=True
        ):
            s = Settings.load()
        self.assertEqual(s.for_retailer("oechsle").discovery_interval, 300)
        self.assertEqual(s.for_retailer("ripley").discovery_interval, 120)
        self.assertEqual(s.for_retailer("plazavea").discovery_interval, 14400)

    @patch("monitor.settings.load_dotenv")
    def test_invalid_interval_rejected(self, dotenv):
        with patch.dict("os.environ", {"OECHSLE_DISCOVERY_INTERVAL": "0"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.load()

    def test_failed_discovery_still_respects_retry_delay(self):
        store = Store(":memory:")
        with patch("monitor.store.time.time", return_value=100):
            store.mark_discovery(retailer="Oechsle")
        self.assertFalse(
            store.discovery_due(120, 900, timestamp=220, retailer="Oechsle")
        )
        self.assertTrue(
            store.discovery_due(120, 900, timestamp=1000, retailer="Oechsle")
        )
        store.close()


if __name__ == "__main__":
    unittest.main()
