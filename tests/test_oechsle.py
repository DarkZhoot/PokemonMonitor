"""Respuestas VTEX simuladas; no representan stock observado en Oechsle."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.app import cycle, run_retailers
from monitor.http import RemoteError
from monitor.models import Product, SearchResult, Stock, normalize_url
from monitor.settings import Settings
from monitor.store import Store
from tiendas.oechsle import Oechsle
from tiendas.plaza_vea import PlazaVea

URL = "https://www.oechsle.pe/pokemon-30-aniversario/p"


def fixture():
    return {
        "Vendido por": ["Oechsle"],
        "productId": "123",
        "productName": "Pokemon TCG 30 aniversario English",
        "link": URL,
        "items": [
            {
                "itemId": "sku1",
                "sellers": [
                    {
                        "sellerId": "1",
                        "commertialOffer": {
                            "IsAvailable": True,
                            "AvailableQuantity": 3,
                            "Price": 49.9,
                            "CurrencyCode": "PEN",
                        },
                    }
                ],
            }
        ],
    }


class OechsleTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings().for_retailer("oechsle")
        self.adapter = Oechsle(self.settings, Mock())

    def test_normalization_and_stable_identity(self):
        p = self.adapter.normalize(fixture())
        self.assertEqual(p.key, "oechsle:123")
        self.assertEqual(p.retailer, "Oechsle")
        self.assertEqual(p.classification, "CANDIDATO")

    def test_canonical_url(self):
        self.assertEqual(
            normalize_url(
                "http://oechsle.pe/pokemon-30-aniversario/p/?utm_source=test#x",
                "Oechsle",
            ),
            URL,
        )

    def test_rejects_cross_retailer_link(self):
        r = fixture()
        r["link"] = "https://www.plazavea.com.pe/product/p"
        with self.assertRaises(ValueError):
            self.adapter.normalize(r)

    def test_rejects_external_link(self):
        r = fixture()
        r["link"] = "https://evil.example/product/p"
        with self.assertRaises(ValueError):
            self.adapter.normalize(r)

    def test_search_endpoint(self):
        self.settings.terms = ["pokemon"]
        self.adapter.client.get.return_value = [fixture()]
        result = self.adapter.search()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.products), 1)
        self.assertEqual(
            self.adapter.client.get.call_args.args[0],
            "https://www.oechsle.pe/api/catalog_system/pub/products/search/pokemon",
        )

    def test_pagination(self):
        self.settings.terms = ["pokemon"]
        page = [
            dict(fixture(), productId=str(i), link=f"https://www.oechsle.pe/item{i}/p")
            for i in range(20)
        ]
        self.adapter.client.get.side_effect = [page, []]
        self.assertTrue(self.adapter.search().complete)
        self.assertEqual(self.adapter.client.get.call_args.args[1]["_from"], 20)

    def test_details_by_id(self):
        p = self.adapter.normalize(fixture())
        self.adapter.client.get.return_value = [fixture()]
        self.assertEqual(self.adapter.details(p).key, p.key)
        self.assertEqual(
            self.adapter.client.get.call_args.args[0],
            "https://www.oechsle.pe/api/catalog_system/pub/products/search",
        )
        self.assertEqual(
            self.adapter.client.get.call_args.args[1]["fq"], "productId:123"
        )

    def test_details_by_slug(self):
        p = Product("", "", URL, retailer="Oechsle")
        self.adapter.client.get.return_value = [fixture()]
        self.assertEqual(self.adapter.details(p).id, "123")
        self.assertTrue(
            self.adapter.client.get.call_args.args[0].endswith(
                "/pokemon-30-aniversario/p"
            )
        )

    def test_details_reject_cross_retailer_before_request(self):
        with self.assertRaises(ValueError):
            self.adapter.details(
                Product("123", "", "https://www.plazavea.com.pe/item/p")
            )
        self.adapter.client.get.assert_not_called()

    def test_positive_catalog_offer(self):
        s = self.adapter.stock(self.adapter.normalize(fixture()))
        self.assertEqual((s.state, s.price, s.currency), ("DISPONIBLE", 49.9, "PEN"))

    def test_incomplete_offers_never_exhausted(self):
        r = fixture()
        r["items"][0]["sellers"] = [
            {"commertialOffer": {"IsAvailable": False, "AvailableQuantity": 0}},
            {"commertialOffer": {}},
        ]
        self.assertEqual(
            self.adapter.stock(self.adapter.normalize(r)).state, "DESCONOCIDO"
        )

    def test_blocked_search_reports_incomplete(self):
        self.adapter.client.get.side_effect = RemoteError(403)
        self.assertFalse(self.adapter.search().complete)

    def test_excludes_japanese(self):
        r = fixture()
        r["productName"] += " Japanese"
        self.assertEqual(self.adapter.normalize(r).classification, "DESCARTADO")

    def test_missing_language_pending(self):
        r = fixture()
        r["productName"] = "Pokemon TCG 30 aniversario"
        self.assertEqual(self.adapter.normalize(r).classification, "POR_VERIFICAR")

    def test_independent_filters(self):
        s = Settings(
            seller_id="pv",
            sales_channel="2",
            oechsle_seller_id="oe",
            oechsle_sales_channel="1",
        )
        self.assertEqual(s.for_retailer("oechsle").seller_id, "oe")
        self.assertEqual(s.for_retailer("oechsle").sales_channel, "1")
        self.assertEqual(s.for_retailer("plazavea").seller_id, "pv")


class MultiRetailerTests(unittest.TestCase):
    def test_identical_ids_have_independent_state_and_events(self):
        store = Store(":memory:")
        oe = Oechsle().normalize(fixture())
        pv = copy.deepcopy(oe)
        pv.retailer = "Plaza Vea"
        pv.url = "https://www.plazavea.com.pe/item/p"
        store.observe(pv, Stock("AGOTADO"))
        store.observe(oe, Stock("AGOTADO"))
        self.assertEqual(store.observe(oe, Stock("DISPONIBLE")), "REPOSICION")
        self.assertEqual(
            store.db.execute(
                "SELECT state FROM products WHERE key=?", (pv.key,)
            ).fetchone()[0],
            "AGOTADO",
        )
        event = json.loads(store.db.execute("SELECT payload FROM events").fetchone()[0])
        self.assertEqual(event["tienda"], "Oechsle")
        self.assertEqual(event["url"], URL)
        self.assertEqual(len(store.products()), 2)
        store.close()

    def test_discovery_dates_independent_and_preserve_legacy_keys(self):
        store = Store(":memory:")
        with patch("monitor.store.time.time", return_value=100):
            store.mark_discovery(successful=True)
        self.assertFalse(
            store.discovery_due(14400, 900, timestamp=101, retailer="Plaza Vea")
        )
        self.assertTrue(
            store.discovery_due(14400, 900, timestamp=101, retailer="Oechsle")
        )
        with patch("monitor.store.time.time", return_value=101):
            store.mark_discovery(retailer="Oechsle")
        self.assertFalse(
            store.discovery_due(14400, 900, timestamp=200, retailer="Oechsle")
        )
        self.assertIsNotNone(
            store.db.execute(
                "SELECT 1 FROM meta WHERE key='discovery_success'"
            ).fetchone()
        )
        store.close()

    @patch("monitor.app.PRODUCTOS", [])
    def test_polling_only_visits_current_retailer(self):
        store = Store(":memory:")
        oe = Oechsle().normalize(fixture())
        pv = copy.deepcopy(oe)
        pv.retailer = "Plaza Vea"
        pv.url = "https://www.plazavea.com.pe/item/p"
        store.observe(pv, Stock("AGOTADO"))
        store.observe(oe, Stock("AGOTADO"))
        adapter = Mock()
        adapter.details.return_value = oe
        adapter.stock.return_value = Stock("AGOTADO")
        telegram = Mock()
        telegram.settings = Settings(token="test", chat_id="1")
        self.assertTrue(
            cycle(Settings(), store, adapter, telegram, Mock(), retailer="Oechsle")
        )
        adapter.details.assert_called_once_with(oe)
        adapter.search.assert_not_called()
        store.close()

    @patch("monitor.app.scheduled_cycle")
    def test_one_failure_does_not_stop_the_other_retailer(self, scheduled):
        scheduled.side_effect = [RuntimeError("simulated"), True]
        adapters = {"Oechsle": (Settings(), Mock()), "Plaza Vea": (Settings(), Mock())}
        self.assertFalse(run_retailers(Settings(), Mock(), adapters, Mock(), Mock()))
        self.assertEqual(scheduled.call_count, 2)
        self.assertEqual(scheduled.call_args.kwargs["retailer"], "Plaza Vea")

    @patch("monitor.settings.load_dotenv")
    def test_configuration_can_disable_oechsle(self, dotenv):
        with patch.dict("os.environ", {"RETAILERS": "plazavea"}, clear=True):
            s = Settings.load()
        self.assertEqual(s.retailers, ["plazavea"])

    @patch("monitor.settings.load_dotenv")
    def test_invalid_retailer_rejected(self, dotenv):
        with patch.dict("os.environ", {"RETAILERS": "inventado"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.load()


class EmptyDiscoveryTests(unittest.TestCase):
    @patch("monitor.app.PRODUCTOS", [])
    def test_successful_empty_catalog_does_not_fail_health(self):
        store = Store(":memory:")
        adapter = Mock()
        adapter.search.return_value = SearchResult(products=[])
        telegram = Mock()
        telegram.settings = Settings(token="test", chat_id="1")
        self.assertTrue(
            cycle(
                Settings(),
                store,
                adapter,
                telegram,
                Mock(),
                discover=True,
                retailer="Oechsle",
            )
        )
        adapter.details.assert_not_called()
        self.assertTrue(
            cycle(Settings(), store, adapter, telegram, Mock(), retailer="Oechsle")
        )
        store.close()


class BackoffRegressionTests(unittest.TestCase):
    def test_429_after_403_uses_current_status(self):
        from monitor.http import Client

        client = Client(Settings(retries=0, request_delay=0))
        client.session = Mock()
        denied = Mock(status_code=403, ok=False)
        limited = Mock(status_code=429, ok=False, headers={"Retry-After": "60"})
        client.session.get.side_effect = [denied, limited]
        with self.assertRaises(RemoteError):
            client.get(URL)
        client.blocked_until = 0
        with self.assertRaises(RemoteError):
            client.get(URL)
        with self.assertRaises(RemoteError) as result:
            client.get(URL)
        self.assertEqual(result.exception.status, 429)


if __name__ == "__main__":
    unittest.main()
