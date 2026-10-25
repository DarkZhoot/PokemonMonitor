"""Ventas propias exclusivamente, incluso al actualizar una base de la versión anterior."""

import copy
import json
import unittest
from dataclasses import asdict
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from monitor.models import Product, Stock
from monitor.sellers import enforce_ripley_scope
from monitor.settings import Settings
from monitor.store import Store
from monitor.telegram import deliver
from tiendas.ripley import Ripley, CatalogHTML
from test_ripley import native, schema, listing, detail_html, URL


class RipleySellerTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Ripley(
            Settings(terms=["pokemon"]).for_retailer("ripley"), Mock()
        )

    def test_search_always_requests_ripley_facet(self):
        self.adapter.client.get.return_value = listing()
        self.adapter.search()
        params = parse_qs(urlsplit(self.adapter.client.get.call_args.args[0]).query)
        self.assertEqual(params["facets"], ["Vendido por:Ripley"])
        self.assertEqual(params["sort"], ["relevance_desc"])

    def test_native_marketplace_is_discarded_even_if_filter_ignored(self):
        raw = native()
        raw["seller"] = "MARKETPLACE"
        self.adapter.client.get.return_value = listing([raw])
        result = self.adapter.search()
        self.assertTrue(result.complete)
        self.assertEqual(result.products, [])

    def test_missing_native_seller_never_candidate(self):
        raw = native()
        raw.pop("seller")
        p = self.adapter.normalize_native(raw, CatalogHTML(listing([raw])))
        self.assertEqual(p.classification, "POR_VERIFICAR")
        self.assertEqual(self.adapter.stock(p).state, "DESCONOCIDO")

    def test_marketplace_stock_is_never_used(self):
        raw = native()
        raw["seller"] = "MARKETPLACE"
        p = self.adapter.normalize_native(raw, CatalogHTML(listing([raw])))
        self.assertEqual(self.adapter.stock(p).state, "DESCONOCIDO")

    def test_schema_marketplace_excluded(self):
        raw = schema()
        raw["url"] = URL
        raw["offers"]["seller"]["name"] = "UNALUKA INTERNACIONAL"
        self.assertEqual(self.adapter.normalize(raw).classification, "DESCARTADO")

    def test_schema_missing_seller_unknown(self):
        raw = schema()
        raw["url"] = URL
        raw["offers"].pop("seller")
        p = self.adapter.normalize(raw)
        self.assertEqual(p.classification, "POR_VERIFICAR")
        self.assertEqual(self.adapter.stock(p).state, "DESCONOCIDO")

    def test_mixed_offers_use_only_ripley_price(self):
        raw = schema()
        raw["url"] = URL
        own = copy.deepcopy(raw["offers"])
        own["price"] = "100"
        market = copy.deepcopy(own)
        market["seller"]["name"] = "Otro vendedor"
        market["price"] = "1"
        raw["offers"] = [market, own]
        s = self.adapter.stock(self.adapter.normalize(raw))
        self.assertEqual((s.state, s.price), ("DISPONIBLE", 100.0))

    def test_marketplace_stock_cannot_override_ripley_exhausted(self):
        raw = schema()
        raw["url"] = URL
        own = copy.deepcopy(raw["offers"])
        own["availability"] = "https://schema.org/OutOfStock"
        market = copy.deepcopy(raw["offers"])
        market["seller"]["name"] = "Otro vendedor"
        raw["offers"] = [own, market]
        self.assertEqual(
            self.adapter.stock(self.adapter.normalize(raw)).state, "AGOTADO"
        )

    def test_unknown_seller_cannot_prove_all_ripley_offers_exhausted(self):
        raw = schema()
        raw["url"] = URL
        own = copy.deepcopy(raw["offers"])
        own["availability"] = "https://schema.org/OutOfStock"
        unknown = copy.deepcopy(raw["offers"])
        unknown.pop("seller")
        raw["offers"] = [own, unknown]
        self.assertEqual(
            self.adapter.stock(self.adapter.normalize(raw)).state, "DESCONOCIDO"
        )

    def test_actual_review_node_is_not_an_offer_contradiction(self):
        review = {
            "@type": "Product",
            "@id": URL + "#product",
            "aggregateRating": {"ratingValue": 5},
        }
        self.adapter.client.get.return_value = detail_html(
            {"@graph": [review, schema()]}
        )
        p = self.adapter.details(Product("PMP20001667733", "", URL, retailer="Ripley"))
        self.assertEqual(p.classification, "CANDIDATO")

    def test_fallback_pagination_restores_facet(self):
        raw = schema()
        raw["url"] = URL
        first = (
            detail_html(raw) + '<a rel="next" href="/search/pokemon?page=2">Next</a>'
        )
        self.adapter.client.get.side_effect = [first, detail_html(raw)]
        self.assertTrue(self.adapter.search().complete)
        params = parse_qs(urlsplit(self.adapter.client.get.call_args.args[0]).query)
        self.assertEqual(params["facets"], ["Vendido por:Ripley"])
        self.assertEqual(params["page"], ["2"])

    def test_store_guard_does_not_queue_marketplace(self):
        raw = native()
        raw["seller"] = "MARKETPLACE"
        p = self.adapter.normalize_native(raw, CatalogHTML(listing([raw])))
        p.classification = "CANDIDATO"  # Simulate a stale caller using the old scope.
        store = Store(":memory:")
        self.assertIsNone(store.observe(p, Stock("DISPONIBLE")))
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )
        store.close()


class ExistingQueueTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.p = Ripley().normalize_native(native(), CatalogHTML(listing()))
        self.store.observe(self.p, Stock("DISPONIBLE"))

    def tearDown(self):
        self.store.close()

    def make_legacy_marketplace(self):
        raw = asdict(self.p)
        raw["raw"]["native"]["seller"] = "MARKETPLACE"
        raw["classification"] = "CANDIDATO"
        payload = json.loads(
            self.store.db.execute("SELECT payload FROM events").fetchone()[0]
        )
        payload.pop("seller_scope", None)
        with self.store.db:
            self.store.db.execute("UPDATE products SET data=?", (json.dumps(raw),))
            self.store.db.execute("UPDATE events SET payload=?", (json.dumps(payload),))

    def test_existing_marketplace_removed_from_tracking_without_deletion(self):
        self.make_legacy_marketplace()
        enforce_ripley_scope(self.store, Mock())
        products = self.store.products()
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].classification, "DESCARTADO")
        self.assertEqual(
            self.store.db.execute("SELECT last_known FROM products").fetchone()[0],
            "DISPONIBLE",
        )

    def test_existing_pending_marketplace_cancelled_before_any_send(self):
        self.make_legacy_marketplace()
        telegram = Mock()
        telegram.settings = Settings(token="test", chat_id="1")
        deliver(self.store, telegram, Mock())
        telegram.send.assert_not_called()
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0],
            "cancelled",
        )

    def test_failed_marketplace_cannot_be_reactivated(self):
        self.make_legacy_marketplace()
        self.store.db.execute("UPDATE events SET status='failed'")
        self.store.db.commit()
        enforce_ripley_scope(self.store, Mock())
        self.store.db.execute(
            "UPDATE events SET status='pending' WHERE status='failed'"
        )
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0],
            "cancelled",
        )

    def test_sent_history_preserved(self):
        self.make_legacy_marketplace()
        self.store.db.execute("UPDATE events SET status='sent'")
        self.store.db.commit()
        enforce_ripley_scope(self.store, Mock())
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0], "sent"
        )

    @patch("monitor.telegram.time.sleep")
    def test_verified_direct_event_still_delivered(self, sleep):
        telegram = Mock()
        telegram.settings = Settings(token="test", chat_id="1")
        telegram.send.return_value = (True, 0, "", False)
        deliver(self.store, telegram, Mock())
        telegram.send.assert_called_once()
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0], "sent"
        )

    def test_old_unverified_snapshot_cancelled_even_if_current_product_is_direct(self):
        event = json.loads(
            self.store.db.execute("SELECT payload FROM events").fetchone()[0]
        )
        event.pop("seller_scope")
        self.store.db.execute("UPDATE events SET payload=?", (json.dumps(event),))
        self.store.db.commit()
        enforce_ripley_scope(self.store, Mock())
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0],
            "cancelled",
        )

    def test_other_retailer_events_untouched(self):
        self.make_legacy_marketplace()
        pv = Product(
            "123",
            "Pokemon TCG 30 aniversario English",
            "https://www.plazavea.com.pe/item/p",
            "CANDIDATO",
        )
        self.store.observe(pv, Stock("DISPONIBLE"))
        enforce_ripley_scope(self.store, Mock())
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM events WHERE product_key='plazavea:123'"
            ).fetchone()[0],
            "pending",
        )


class ScopeMigrationTests(unittest.TestCase):
    def test_reset_only_ripley_schedule_once(self):
        store = Store(":memory:")
        for retailer in ("Plaza Vea", "Oechsle", "Ripley"):
            store.mark_discovery(successful=True, retailer=retailer)
        enforce_ripley_scope(store, Mock())
        self.assertFalse(store.has_discovery_success("Ripley"))
        self.assertTrue(store.has_discovery_success("Plaza Vea"))
        self.assertTrue(store.has_discovery_success("Oechsle"))
        store.mark_discovery(successful=True, retailer="Ripley")
        enforce_ripley_scope(store, Mock())
        self.assertTrue(store.has_discovery_success("Ripley"))
        store.close()

    def test_unknown_seller_pending_is_cancelled(self):
        store = Store(":memory:")
        p = Ripley().normalize_native(native(), CatalogHTML(listing()))
        store.observe(p, Stock("DISPONIBLE"))
        p.raw["native"].pop("seller")
        store.db.execute("UPDATE products SET data=?", (json.dumps(asdict(p)),))
        store.db.commit()
        enforce_ripley_scope(store, Mock())
        self.assertEqual(store.products()[0].classification, "POR_VERIFICAR")
        self.assertEqual(
            store.db.execute("SELECT status FROM events").fetchone()[0], "cancelled"
        )
        store.close()

    @patch("monitor.app.PRODUCTOS", [])
    def test_manual_marketplace_cannot_override_scope(self):
        from monitor.app import cycle

        store = Store(":memory:")
        raw = native()
        raw["seller"] = "MARKETPLACE"
        p = Ripley().normalize_native(raw, CatalogHTML(listing([raw])))
        store.observe(p, Stock())
        store.mark_discovery(successful=True, retailer="Ripley")
        enforce_ripley_scope(store, Mock())
        store.mark_discovery(successful=True, retailer="Ripley")
        settings = Settings(products=[{"tienda": "Ripley", "url": p.url}])
        telegram = Mock()
        telegram.settings = Settings(token="test", chat_id="1")
        adapter = Mock()
        self.assertTrue(
            cycle(settings, store, adapter, telegram, Mock(), retailer="Ripley")
        )
        adapter.details.assert_not_called()
        adapter.stock.assert_not_called()
        store.close()


if __name__ == "__main__":
    unittest.main()
