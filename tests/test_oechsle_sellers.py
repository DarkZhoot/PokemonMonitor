import copy
import json
import unittest
from dataclasses import asdict
from unittest.mock import Mock, patch

from monitor.models import Product, Stock
from monitor.sellers import enforce_oechsle_scope
from monitor.settings import Settings
from monitor.store import Store
from monitor.telegram import deliver
from tiendas.oechsle import Oechsle
from test_oechsle import fixture


class OechsleSellerTests(unittest.TestCase):
    def setUp(self):
        self.a = Oechsle(Settings(terms=["pokemon"]).for_retailer("oechsle"), Mock())

    def test_search_has_vendor_filter(self):
        self.a.client.get.return_value = [fixture()]
        self.assertTrue(self.a.search().complete)
        self.assertEqual(self.a.client.get.call_args.args[1]["fq"], "sellerId:1")

    def test_plazavea_with_empty_oechsle_placeholder_rejected(self):
        raw = fixture()
        raw["Vendido por"] = ["plazaVea"]
        raw["items"][0]["sellers"] = [
            {
                "sellerId": "plazavea",
                "sellerName": "Plaza Vea",
                "commertialOffer": {
                    "IsAvailable": True,
                    "AvailableQuantity": 50,
                    "Price": 1,
                },
            },
            {
                "sellerId": "1",
                "sellerName": "oechsle",
                "commertialOffer": {"IsAvailable": False, "AvailableQuantity": 0},
            },
        ]
        p = self.a.normalize(raw)
        self.assertEqual(p.classification, "DESCARTADO")
        self.assertEqual(self.a.stock(p).state, "DESCONOCIDO")

    def test_unknown_ownership_not_candidate(self):
        raw = fixture()
        raw.pop("Vendido por")
        p = self.a.normalize(raw)
        self.assertEqual(p.classification, "POR_VERIFICAR")
        self.assertEqual(self.a.stock(p).state, "DESCONOCIDO")

    def test_mixed_product_ownership_is_ambiguous(self):
        raw = fixture()
        raw["Vendido por"] = ["Oechsle", "plazaVea"]
        self.assertEqual(self.a.normalize(raw).classification, "POR_VERIFICAR")

    def test_foreign_offer_does_not_make_own_product_available(self):
        raw = fixture()
        raw["items"][0]["sellers"] = [
            {
                "sellerId": "1",
                "sellerName": "oechsle",
                "commertialOffer": {"IsAvailable": False, "AvailableQuantity": 0},
            },
            {
                "sellerId": "other",
                "sellerName": "Otro",
                "commertialOffer": {
                    "IsAvailable": True,
                    "AvailableQuantity": 3,
                    "Price": 1,
                },
            },
        ]
        self.assertEqual(self.a.stock(self.a.normalize(raw)).state, "AGOTADO")

    def test_prices_from_oechsle_only(self):
        raw = fixture()
        raw["items"][0]["sellers"].append(
            {
                "sellerId": "other",
                "commertialOffer": {
                    "IsAvailable": True,
                    "AvailableQuantity": 10,
                    "Price": 1,
                },
            }
        )
        self.assertEqual(self.a.stock(self.a.normalize(raw)).price, 49.9)

    def test_no_own_offer_is_unknown(self):
        raw = fixture()
        raw["items"][0]["sellers"][0]["sellerId"] = "other"
        self.assertEqual(self.a.stock(self.a.normalize(raw)).state, "DESCONOCIDO")

    def test_conflicting_own_seller_name_unknown(self):
        raw = fixture()
        raw["items"][0]["sellers"][0]["sellerName"] = "Otro"
        self.assertEqual(self.a.stock(self.a.normalize(raw)).state, "DESCONOCIDO")

    def test_stock_does_not_mutate_raw(self):
        raw = fixture()
        p = self.a.normalize(raw)
        before = copy.deepcopy(raw)
        self.a.stock(p)
        self.assertEqual(raw, before)

    def test_stale_caller_cannot_queue_marketplace(self):
        raw = fixture()
        raw["Vendido por"] = ["Marketplace"]
        p = self.a.normalize(raw)
        p.classification = "CANDIDATO"
        store = Store(":memory:")
        self.assertIsNone(store.observe(p, Stock("DISPONIBLE")))
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )
        store.close()


class OechsleMigrationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.p = Oechsle().normalize(fixture())
        self.store.observe(self.p, Stock("DISPONIBLE"))

    def tearDown(self):
        self.store.close()

    def legacy_marketplace(self):
        row = asdict(self.p)
        row["raw"]["Vendido por"] = ["plazaVea"]
        row["classification"] = "CANDIDATO"
        event = json.loads(
            self.store.db.execute("SELECT payload FROM events").fetchone()[0]
        )
        event.pop("seller_scope")
        with self.store.db:
            self.store.db.execute("UPDATE products SET data=?", (json.dumps(row),))
            self.store.db.execute("UPDATE events SET payload=?", (json.dumps(event),))

    def test_existing_pending_cancelled_before_send(self):
        self.legacy_marketplace()
        telegram = Mock()
        telegram.settings = Settings(token="test", chat_id="1")
        deliver(self.store, telegram, Mock())
        telegram.send.assert_not_called()
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0],
            "cancelled",
        )
        self.assertEqual(self.store.products()[0].classification, "DESCARTADO")

    def test_sent_history_and_stock_are_preserved(self):
        self.legacy_marketplace()
        self.store.db.execute("UPDATE events SET status='sent'")
        self.store.db.commit()
        enforce_oechsle_scope(self.store, Mock())
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0], "sent"
        )
        self.assertEqual(
            self.store.db.execute("SELECT last_known FROM products").fetchone()[0],
            "DISPONIBLE",
        )

    def test_reset_only_oechsle_clock_once(self):
        for retailer in ("Plaza Vea", "Ripley", "Oechsle"):
            self.store.mark_discovery(successful=True, retailer=retailer)
        enforce_oechsle_scope(self.store, Mock())
        self.assertFalse(self.store.has_discovery_success("Oechsle"))
        self.assertTrue(self.store.has_discovery_success("Plaza Vea"))
        self.assertTrue(self.store.has_discovery_success("Ripley"))
        self.store.mark_discovery(successful=True, retailer="Oechsle")
        enforce_oechsle_scope(self.store, Mock())
        self.assertTrue(self.store.has_discovery_success("Oechsle"))

    @patch("monitor.telegram.time.sleep")
    def test_own_new_event_delivered(self, sleep):
        telegram = Mock()
        telegram.settings = Settings(token="test", chat_id="1")
        telegram.send.return_value = (True, 0, "", False)
        deliver(self.store, telegram, Mock())
        telegram.send.assert_called_once()
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0], "sent"
        )

    def test_failed_external_event_not_reactivated(self):
        self.legacy_marketplace()
        self.store.db.execute("UPDATE events SET status='failed'")
        self.store.db.commit()
        enforce_oechsle_scope(self.store, Mock())
        self.store.db.execute(
            "UPDATE events SET status='pending' WHERE status='failed'"
        )
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0],
            "cancelled",
        )

    def test_plaza_product_and_alert_not_changed(self):
        self.legacy_marketplace()
        p = Product(
            "pv",
            "Pokemon TCG 30 aniversario English",
            "https://www.plazavea.com.pe/item/p",
            "CANDIDATO",
        )
        self.store.observe(p, Stock("DISPONIBLE"))
        enforce_oechsle_scope(self.store, Mock())
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM events WHERE product_key='plazavea:pv'"
            ).fetchone()[0],
            "pending",
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM products WHERE key='plazavea:pv'"
            ).fetchone()[0],
            "DISPONIBLE",
        )


if __name__ == "__main__":
    unittest.main()
