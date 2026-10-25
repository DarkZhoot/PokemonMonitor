"""Simulated public schema.org fixtures; no claim of live Falabella validation."""

import json
import unittest
from unittest.mock import Mock, patch
from dataclasses import replace
from tempfile import TemporaryDirectory
from pathlib import Path
from monitor.models import Product, Stock, normalize_url
from monitor.settings import Settings
from monitor.http import RemoteError
from monitor.store import Store
from monitor.app import scheduled_cycle
from tiendas.falabella import Falabella, BASE_URL, SELLER_PARAM

URL = BASE_URL + "/falabella-pe/product/123456789/pokemon-tcg-30-ingles/123456790"
NAME = "Pokémon TCG 30.º aniversario en inglés"


def raw(seller="Falabella", status="InStock"):
    return {
        "@type": "Product",
        "name": NAME,
        "url": URL,
        "offers": {
            "@type": "Offer",
            "seller": {"name": seller},
            "availability": "https://schema.org/" + status,
            "price": "289.90",
            "priceCurrency": "PEN",
        },
    }


def html(data):
    return '<script type="application/ld+json">' + json.dumps(data) + "</script>"


class FalabellaTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.settings = replace(
            Settings().for_retailer("falabella"), terms=["pokemon tcg 30"], pages=2
        )
        self.adapter = Falabella(self.settings, self.client)

    def product(self, data=None):
        p = self.adapter.normalize(data or raw())
        p.raw["_needs_details"] = False
        return p

    def test_url_identity(self):
        p = self.product()
        self.assertEqual(p.key, "falabella:123456789")
        self.assertEqual(normalize_url(URL + "?tracking=x", "Falabella"), URL)

    def test_invalid_urls(self):
        for u in [
            "https://evil.com/falabella-pe/product/123/a",
            BASE_URL + "/falabella-pe/search",
            BASE_URL + "/falabella-pe/product/123/..",
            "https://user@www.falabella.com.pe/falabella-pe/product/123/a",
        ]:
            with self.subTest(url=u), self.assertRaises(ValueError):
                normalize_url(u, "Falabella")

    def test_available(self):
        s = self.adapter.stock(self.product())
        self.assertEqual((s.state, s.price, s.currency), ("DISPONIBLE", 289.9, "PEN"))

    def test_exhausted(self):
        self.assertEqual(
            self.adapter.stock(self.product(raw(status="OutOfStock"))).state, "AGOTADO"
        )

    def test_preorder_unknown(self):
        self.assertEqual(
            self.adapter.stock(self.product(raw(status="PreOrder"))).state,
            "DESCONOCIDO",
        )

    def test_marketplace_excluded(self):
        p = self.product(raw(seller="Tienda externa"))
        self.assertEqual(p.classification, "DESCARTADO")
        self.assertEqual(self.adapter.stock(p).state, "DESCONOCIDO")

    def test_unknown_seller(self):
        r = raw()
        del r["offers"]["seller"]
        p = self.product(r)
        self.assertEqual(p.classification, "POR_VERIFICAR")
        self.assertEqual(self.adapter.stock(p).state, "DESCONOCIDO")

    def test_marketplace_cannot_supply_own_stock(self):
        r = raw(status="OutOfStock")
        r["offers"] = [r["offers"], raw("Other")["offers"]]
        self.assertEqual(self.adapter.stock(self.product(r)).state, "AGOTADO")

    def test_unknown_offer_prevents_exhaustion(self):
        r = raw(status="OutOfStock")
        other = raw()["offers"]
        del other["seller"]
        r["offers"] = [r["offers"], other]
        self.assertEqual(self.adapter.stock(self.product(r)).state, "DESCONOCIDO")

    def test_seller_not_substring(self):
        self.assertEqual(
            self.product(raw("Falabella ofertas externas")).classification, "DESCARTADO"
        )

    def test_spanish_excluded(self):
        r = raw()
        r["name"] = NAME.replace("inglés", "español")
        self.assertEqual(self.product(r).classification, "DESCARTADO")

    def test_context_disabled(self):
        self.adapter.settings.context_confirmed = False
        self.assertEqual(self.adapter.stock(self.product()).state, "DESCONOCIDO")

    def test_unimplemented_location(self):
        self.adapter.settings.postal_code = "15001"
        self.assertEqual(self.adapter.stock(self.product()).state, "DESCONOCIDO")

    def test_listing_alone_not_stock(self):
        self.assertEqual(
            self.adapter.stock(self.adapter.normalize(raw())).state, "DESCONOCIDO"
        )

    def test_details_select_identity_not_recommendation(self):
        other = raw()
        other["url"] = URL.replace("123456789", "999999999")
        self.client.get.return_value = html([other, raw()])
        self.assertEqual(self.adapter.details(self.product()).id, "123456789")

    def test_missing_primary_not_recommendation(self):
        other = raw()
        other["url"] = URL.replace("123456789", "999999999")
        self.client.get.return_value = html(other)
        with self.assertRaises(ValueError):
            self.adapter.details(self.product())

    def test_conflicting_details_rejected(self):
        self.client.get.return_value = html([raw(), raw(status="OutOfStock")])
        with self.assertRaises(ValueError):
            self.adapter.details(self.product())

    def test_filter_preserved_pagination(self):
        other_url = URL.replace("123456789", "999999999")
        calls = []

        def response(url, params=None, **kwargs):
            calls.append((url, dict(params or {})))
            if params:
                target = URL if params["page"] == 1 else other_url
                nxt = (
                    '<a rel="next" href="?page=2&Ntt=pokemon+tcg+30">2</a>'
                    if params["page"] == 1
                    else ""
                )
                return f'<a href="{target}">{NAME}</a>' + nxt
            data = raw()
            data["url"] = url
            return html(data)

        self.client.get.side_effect = response
        result = self.adapter.search()
        self.assertTrue(result.complete, result.errors)
        self.assertEqual(len(result.products), 2)
        searches = [p for _, p in calls if p]
        self.assertEqual([p["page"] for p in searches], [1, 2])
        self.assertTrue(
            all(
                p[SELLER_PARAM] == "FALABELLA" and p["facetSelected"] == "true"
                for p in searches
            )
        )

    def test_no_results(self):
        self.client.get.return_value = "<p>No encontramos productos</p>"
        r = self.adapter.search()
        self.assertTrue(r.complete)
        self.assertFalse(r.products)

    def test_challenge_not_empty(self):
        self.client.get.return_value = "<h1>Verify you are human</h1>"
        self.assertFalse(self.adapter.search().complete)

    def test_403_stops_no_fallback(self):
        self.client.get.side_effect = RemoteError(403)
        self.adapter.settings.terms *= 4
        self.assertFalse(self.adapter.search().complete)
        self.assertEqual(self.client.get.call_count, 1)

    def test_no_currency_assumption_or_nan(self):
        r = raw()
        r["offers"]["price"] = "NaN"
        del r["offers"]["priceCurrency"]
        s = self.adapter.stock(self.product(r))
        self.assertIsNone(s.price)
        self.assertIsNone(s.currency)

    def test_restock_and_unknown_preserves_last_known(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            p = self.product()
            store.observe(p, Stock("AGOTADO"))
            store.observe(p, Stock(detail="HTTP 403"))
            self.assertEqual(store.observe(p, Stock("DISPONIBLE")), "REPOSICION")
            store.close()

    def test_store_guards_marketplace_even_if_caller_wrong(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            p = self.product(raw("Other"))
            p.classification = "CANDIDATO"
            self.assertIsNone(store.observe(p, Stock("DISPONIBLE")))
            store.close()

    def test_profile_and_scheduler(self):
        s = Settings()
        self.assertEqual(s.for_retailer("falabella").discovery_interval, 120)
        self.assertEqual(s.discovery_interval, 14400)
        store = Mock()
        store.discovery_due.return_value = False
        with patch("monitor.app.cycle", return_value=True):
            scheduled_cycle(
                s, store, self.adapter, Mock(), Mock(), retailer="Falabella"
            )
        store.discovery_due.assert_called_once_with(120, 900, retailer="Falabella")

    def test_env_load(self):
        with patch.dict(
            "os.environ",
            {
                "RETAILERS": "plazavea,falabella",
                "FALABELLA_DISCOVERY_INTERVAL": "240",
                "FALABELLA_CONTEXT_CONFIRMED": "false",
            },
        ):
            s = Settings.load().for_retailer("falabella")
        self.assertEqual(s.discovery_interval, 240)
        self.assertFalse(s.context_confirmed)

    def test_foreign_details_no_request(self):
        with self.assertRaises(ValueError):
            self.adapter.details(Product("1", "x", URL, retailer="Ripley"))
        self.client.get.assert_not_called()

    def test_one_bad_detail_does_not_hide_other_products(self):
        other_url = URL.replace("123456789", "999999999")
        self.client.get.side_effect = [
            f'<a href="{other_url}">{NAME}</a><a href="{URL}">{NAME}</a>',
            "<p>Not a product</p>",
            html(raw()),
        ]
        result = self.adapter.search()
        self.assertFalse(result.complete)
        self.assertEqual([p.id for p in result.products], ["123456789"])

    def test_explicit_marketplace_listing_skips_detail(self):
        self.client.get.return_value = html(raw("Other"))
        result = self.adapter.search()
        self.assertTrue(result.complete)
        self.assertEqual(result.products, [])
        self.assertEqual(self.client.get.call_count, 1)
