"""The same persistence and direct polling contract for all six adapters."""

import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from monitor.app import cycle
from monitor.discovery import DiscoveryPolicy, tracking_stub, should_watch, remember
from monitor.models import SearchResult, Stock
from monitor.settings import Settings
from monitor.store import Store
from test_shared_classification import products
from tiendas.plaza_vea import PlazaVea
from tiendas.oechsle import Oechsle
from tiendas.ripley import Ripley
from tiendas.falabella import Falabella
from tiendas.ilahui import Ilahui
from tiendas.tailoy import Tailoy


class SharedDiscoveryTests(unittest.TestCase):
    def test_abbreviated_title_is_recognized_by_every_adapter(self):
        for p in products("TCG30TH ULTRA PC ING ESPEON", model="unknown"):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "CANDIDATO")
                self.assertTrue(should_watch(p))
                self.assertEqual(p.name, "TCG30TH ULTRA PC ING ESPEON")

    def test_every_adapter_uses_common_search_expansion_without_mutating_settings(self):
        for cls in (PlazaVea, Oechsle, Ripley, Falabella, Ilahui, Tailoy):
            with self.subTest(adapter=cls.__name__):
                settings = Settings(terms=["pokemon", "tcg 30"])
                adapter = cls(settings, Mock())
                self.assertIsInstance(adapter, DiscoveryPolicy)
                self.assertEqual(
                    adapter.discovery_terms(), ["pokemon", "tcg 30", "tcg 30th"]
                )
                self.assertEqual(settings.terms, ["pokemon", "tcg 30"])

    def test_explicit_urls_remain_unknown_until_directly_verified_for_every_retailer(
        self,
    ):
        for verified in products():
            with self.subTest(retailer=verified.retailer):
                store = Store(":memory:")
                self.addCleanup(store.close)
                pending = tracking_stub(verified.url, verified.retailer, explicit=True)
                store.observe(pending, Stock())
                self.assertEqual(pending.classification, "POR_VERIFICAR")
                self.assertEqual(
                    store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
                )
                adapter = Mock()
                adapter.details.return_value = verified
                adapter.stock.return_value = Stock("DISPONIBLE")
                with patch("monitor.app.PRODUCTOS", []), patch(
                    "monitor.app.deliver", return_value=True
                ):
                    for _ in range(2):
                        cycle(
                            Settings(),
                            store,
                            adapter,
                            Mock(),
                            Mock(),
                            retailer=verified.retailer,
                        )
                self.assertEqual(adapter.details.call_count, 2)
                self.assertEqual(
                    store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1
                )
                self.assertTrue(
                    any(
                        p.url == verified.url and should_watch(p)
                        for p in store.products()
                    )
                )

    def test_known_urls_are_polled_when_search_omits_them_or_fails(self):
        for p in products():
            for complete in (True, False):
                with self.subTest(retailer=p.retailer, complete=complete):
                    store = Store(":memory:")
                    self.addCleanup(store.close)
                    store.observe(p, Stock("AGOTADO"))
                    adapter = Mock()
                    adapter.search.return_value = SearchResult(
                        complete=complete, errors=[] if complete else ["HTTP 403"]
                    )
                    adapter.details.return_value = p
                    adapter.stock.return_value = Stock("DISPONIBLE")
                    with patch("monitor.app.PRODUCTOS", []), patch(
                        "monitor.app.deliver", return_value=True
                    ):
                        cycle(
                            Settings(),
                            store,
                            adapter,
                            Mock(),
                            Mock(),
                            discover=True,
                            retailer=p.retailer,
                        )
                    adapter.details.assert_called_once()
                    self.assertEqual(
                        store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1
                    )

    def test_failed_details_keep_url_and_last_known_stock_everywhere(self):
        from monitor.http import RemoteError

        for p in products():
            with self.subTest(retailer=p.retailer):
                store = Store(":memory:")
                self.addCleanup(store.close)
                store.observe(p, Stock("AGOTADO"))
                adapter = Mock()
                adapter.details.side_effect = RemoteError(502)
                with patch("monitor.app.PRODUCTOS", []), patch(
                    "monitor.app.deliver", return_value=True
                ):
                    cycle(
                        Settings(), store, adapter, Mock(), Mock(), retailer=p.retailer
                    )
                row = store.db.execute(
                    "SELECT state,last_known FROM products"
                ).fetchone()
                self.assertEqual(tuple(row), ("DESCONOCIDO", "AGOTADO"))
                self.assertEqual(store.products()[0].url, p.url)
                self.assertEqual(
                    store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
                )

    def test_retired_url_cannot_return_under_temporary_id_everywhere(self):
        for p in products():
            with self.subTest(retailer=p.retailer):
                store = Store(":memory:")
                self.addCleanup(store.close)
                store.observe(p, Stock("AGOTADO"))
                store.retire_product(p.key)
                stub = tracking_stub(p.url, p.retailer, explicit=True)
                store.observe(stub, Stock())
                self.assertEqual(len(store.products()), 1)
                self.assertIn((p.retailer, p.url), store.retired_urls())

    def test_failed_url_receipts_never_replace_richer_metadata(self):
        for p in products():
            with self.subTest(retailer=p.retailer):
                found = {p.key: p}
                stub = tracking_stub(p.url, p.retailer, failed=True)
                remember(found, stub)
                self.assertEqual(list(found.values()), [p])

    def test_pending_native_metadata_gets_direct_verification(self):
        for verified in products():
            with self.subTest(retailer=verified.retailer):
                pending = deepcopy(verified)
                pending.classification = "POR_VERIFICAR"
                pending.raw.update(_metadata_only=True, _watch=True)
                store = Store(":memory:")
                self.addCleanup(store.close)
                adapter = Mock()
                adapter.search.return_value = SearchResult(products=[pending])
                adapter.details.return_value = verified
                adapter.stock.return_value = Stock("DISPONIBLE")
                with patch("monitor.app.PRODUCTOS", []), patch(
                    "monitor.app.deliver", return_value=True
                ), patch("monitor.app.enrich_listing_metadata"):
                    cycle(
                        Settings(),
                        store,
                        adapter,
                        Mock(),
                        Mock(),
                        discover=True,
                        retailer=verified.retailer,
                    )
                adapter.details.assert_called_once()
                self.assertEqual(
                    store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1
                )

    def test_failed_html_details_retain_discovered_links_for_both_adapters(self):
        from monitor.http import RemoteError
        from test_ilahui import listing as ilahui_listing, URL as ilahui_url
        from test_tailoy import listing as tailoy_listing, URL as tailoy_url

        for cls, listing, url in (
            (Ilahui, ilahui_listing, ilahui_url),
            (Tailoy, tailoy_listing, tailoy_url),
        ):
            with self.subTest(adapter=cls.__name__):
                client = Mock()
                client.get.return_value = listing()
                adapter = cls(Settings(terms=["pokemon"]), client)
                adapter.details = Mock(side_effect=RemoteError(502))
                result = adapter.search()
                self.assertFalse(result.complete)
                self.assertEqual(len(result.products), 1)
                self.assertEqual(result.products[0].url, url)
                self.assertTrue(should_watch(result.products[0]))
                self.assertEqual(result.products[0].classification, "POR_VERIFICAR")
                adapter.details.assert_called_once()

    def test_incomplete_catalog_record_keeps_url_for_both_vtex_stores(self):
        for cls in (PlazaVea, Oechsle):
            with self.subTest(adapter=cls.__name__):
                client = Mock()
                adapter = cls(Settings(terms=["pokemon"]), client)
                url = adapter.base_url + "/example/p"
                client.get.return_value = [
                    {"productName": "Pokemon TCG 30 aniversario ingles", "link": url}
                ]
                result = adapter.search()
                self.assertFalse(result.complete)
                self.assertEqual(len(result.products), 1)
                self.assertEqual(result.products[0].url, url)
                self.assertTrue(should_watch(result.products[0]))
                self.assertEqual(result.products[0].classification, "POR_VERIFICAR")
