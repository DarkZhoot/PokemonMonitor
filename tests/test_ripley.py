"""Fixtures pequeñas simuladas con la estructura observada del HTML de Ripley."""

import copy
import json
import unittest
from unittest.mock import Mock, patch

from monitor.app import cycle, run_retailers
from monitor.http import Client, RemoteError
from monitor.models import Product, Stock, normalize_url
from monitor.settings import Settings
from monitor.store import Store
from tiendas.ripley import Ripley, CatalogHTML

URL = (
    "https://simple.ripley.com.pe/pokemon-tcg-30-aniversario-etb-ingles-pmp20001667733"
)
TITLE = "POKÉMON TCG 30.º ANIVERSARIO ELITE TRAINER BOX EN INGLÉS"


def schema(availability="https://schema.org/InStock"):
    return {
        "@type": "Product",
        "@id": URL + "#product",
        "name": TITLE,
        "brand": {"@type": "Brand", "name": "POKÉMON"},
        "description": "Caja Pokémon TCG, 30 aniversario, inglés",
        "offers": {
            "@type": "Offer",
            "price": "789",
            "priceCurrency": "PEN",
            "availability": availability,
        },
    }


def detail_html(data=None, canonical=URL):
    return f'<html><link rel="canonical" href="{canonical}"><script type="application/ld+json">{json.dumps(schema() if data is None else data)}</script></html>'


def native():
    return {
        "sku": "9865719",
        "parentProductID": "PMP20001667733",
        "name": TITLE,
        "brand": "POKÉMON",
        "description": "Pokemon TCG 30th anniversary English",
        "priceNumber": 789,
        "currency": "PEN",
        "inStock": True,
    }


def listing(products=None, total=None, offset=0, limit=48, links=None):
    products = [native()] if products is None else products
    data = {
        "props": {
            "pageProps": {
                "findabilityProps": {
                    "data": {
                        "products": products,
                        "total": len(products) if total is None else total,
                        "offset": offset,
                        "limit": limit,
                    }
                }
            }
        }
    }
    links = (
        f'<a href="{URL}?color_80=multicolor">{TITLE}</a>' if links is None else links
    )
    return f'<html>{links}<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


class RipleyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings(terms=["pokemon"], pages=3).for_retailer("ripley")
        self.a = Ripley(self.s, Mock())

    def test_canonical_marketplace_url(self):
        self.assertEqual(normalize_url(URL + "?color=red#x", "Ripley"), URL)

    def test_native_product_url(self):
        self.assertTrue(
            normalize_url(
                "https://simple.ripley.com.pe/pokemon-tcg-2032376641974p", "Ripley"
            ).endswith("2032376641974p")
        )

    def test_reject_search_url(self):
        with self.assertRaises(ValueError):
            normalize_url("https://simple.ripley.com.pe/search/pokemon", "Ripley")

    def test_reject_external_link(self):
        with self.assertRaises(ValueError):
            normalize_url("https://evil.example/product-pmp123456", "Ripley")

    def test_native_identity(self):
        page = CatalogHTML(listing())
        p = self.a.normalize_native(native(), page)
        self.assertEqual(
            (p.key, p.url, p.classification),
            ("ripley:PMP20001667733", URL, "CANDIDATO"),
        )

    def test_spanish_not_candidate(self):
        raw = native()
        raw["name"] = "POKEMON TCG 30 aniversario español"
        page = CatalogHTML(listing([raw]))
        p = self.a.normalize_native(raw, page)
        self.assertEqual(p.classification, "DESCARTADO")

    def test_native_missing_stock_unknown(self):
        raw = native()
        del raw["inStock"]
        p = self.a.normalize_native(raw, CatalogHTML(listing([raw])))
        self.assertEqual(self.a.stock(p).state, "DESCONOCIDO")

    def test_native_available_price_currency(self):
        p = self.a.normalize_native(native(), CatalogHTML(listing()))
        self.assertEqual(
            self.a.stock(p),
            Stock(
                "DISPONIBLE",
                789,
                "PEN",
                "Catálogo Ripley inStock=true; sin garantía de entrega",
            ),
        )

    def test_native_exhausted(self):
        raw = native()
        raw["inStock"] = False
        p = self.a.normalize_native(raw, CatalogHTML(listing([raw])))
        self.assertEqual(self.a.stock(p).state, "AGOTADO")

    def test_native_missing_link_is_not_invented(self):
        with self.assertRaises(ValueError):
            self.a.normalize_native(native(), CatalogHTML(listing(links="")))

    def test_detail_matches_observed_schema(self):
        self.a.client.get.return_value = detail_html()
        p = self.a.details(Product("PMP20001667733", "", URL, retailer="Ripley"))
        self.assertEqual(
            (p.key, p.classification), ("ripley:PMP20001667733", "CANDIDATO")
        )
        self.assertEqual(self.a.stock(p).state, "DISPONIBLE")
        self.assertEqual(self.a.client.get.call_args.kwargs, {"as_text": True})

    def test_detail_with_no_url_uses_unique_canonical(self):
        raw = schema()
        del raw["@id"]
        self.a.client.get.return_value = detail_html(raw)
        self.assertEqual(
            self.a.details(Product("PMP20001667733", "", URL, retailer="Ripley")).url,
            URL,
        )

    def test_recommendation_not_primary_product(self):
        wrong = schema()
        wrong["@id"] = "https://simple.ripley.com.pe/otro-pmp20000001234#product"
        self.a.client.get.return_value = detail_html({"@graph": [wrong]})
        with self.assertRaises(ValueError):
            self.a.details(Product("PMP20001667733", "", URL, retailer="Ripley"))

    def test_ambiguous_missing_urls_not_primary(self):
        raw = schema()
        del raw["@id"]
        self.a.client.get.return_value = detail_html(
            {"@graph": [raw, copy.deepcopy(raw)]}
        )
        with self.assertRaises(ValueError):
            self.a.details(Product("PMP20001667733", "", URL, retailer="Ripley"))

    def test_detail_unknown_schema_not_zero_stock(self):
        self.a.client.get.return_value = "<html>Pagina no disponible</html>"
        with self.assertRaises(ValueError):
            self.a.details(Product("PMP20001667733", "", URL, retailer="Ripley"))

    def test_detail_rejects_cross_retailer_before_network(self):
        with self.assertRaises(ValueError):
            self.a.details(
                Product(
                    "123", "", "https://www.oechsle.pe/product/p", retailer="Oechsle"
                )
            )
        self.a.client.get.assert_not_called()

    def test_schema_unknown_offer_mixed_exhausted(self):
        raw = schema()
        raw["url"] = URL
        raw["offers"] = [{"availability": "https://schema.org/OutOfStock"}, {}]
        p = self.a.normalize(raw)
        self.assertEqual(self.a.stock(p).state, "DESCONOCIDO")

    def test_schema_all_exhausted(self):
        raw = schema("https://schema.org/OutOfStock")
        raw["url"] = URL
        self.assertEqual(self.a.stock(self.a.normalize(raw)).state, "AGOTADO")

    def test_preorder_unknown(self):
        raw = schema("https://schema.org/PreOrder")
        raw["url"] = URL
        self.assertEqual(self.a.stock(self.a.normalize(raw)).state, "DESCONOCIDO")

    def test_native_search_does_not_request_each_detail(self):
        self.a.client.get.return_value = listing()
        r = self.a.search()
        self.assertTrue(r.complete)
        self.assertEqual(len(r.products), 1)
        self.assertEqual(self.a.client.get.call_count, 1)

    def test_native_search_empty(self):
        self.a.client.get.return_value = listing(products=[], links="")
        r = self.a.search()
        self.assertTrue(r.complete)
        self.assertEqual(r.products, [])

    def test_native_search_paginates_from_total_offset(self):
        self.a.client.get.side_effect = [
            listing(total=2, limit=1),
            listing(total=2, limit=1, offset=1),
        ]
        self.assertTrue(self.a.search().complete)
        self.assertTrue(self.a.client.get.call_args.args[0].endswith("?page=2"))

    def test_native_pagination_cap_incomplete(self):
        self.s.pages = 1
        self.a.client.get.return_value = listing(total=2, limit=1)
        self.assertFalse(self.a.search().complete)

    def test_wrong_page_not_silently_accepted(self):
        self.a.client.get.side_effect = [
            listing(total=2, limit=1),
            listing(total=2, limit=1),
        ]
        self.assertFalse(self.a.search().complete)

    def test_unknown_html_is_incomplete(self):
        self.a.client.get.return_value = "<html>JavaScript required</html>"
        self.assertFalse(self.a.search().complete)

    def test_captcha_not_treated_as_empty_search(self):
        self.a.client.get.return_value = "<html>Captcha access denied</html>"
        self.assertFalse(self.a.search().complete)

    def test_403_incomplete(self):
        self.a.client.get.side_effect = RemoteError(403)
        result = self.a.search()
        self.assertFalse(result.complete)
        self.assertIn("HTTP 403", result.errors)

    def test_parser_does_not_read_empty_message_from_javascript(self):
        page = CatalogHTML('<script>var message="0 resultados";</script>')
        self.assertNotIn("0 resultados", " ".join(page.text))

    def test_location_filters_are_not_invented(self):
        self.s.postal_code = "15001"
        p = self.a.normalize_native(native(), CatalogHTML(listing()))
        self.assertEqual(self.a.stock(p).state, "DESCONOCIDO")


class RipleySafetyRegressionTests(unittest.TestCase):
    def test_conflicting_primary_offers_are_unknown(self):
        a = Ripley(Settings().for_retailer("ripley"), Mock())
        other = schema("https://schema.org/OutOfStock")
        a.client.get.return_value = detail_html({"@graph": [schema(), other]})
        with self.assertRaises(ValueError):
            a.details(Product("PMP20001667733", "", URL, retailer="Ripley"))

    def test_html_response_avoids_json_decoding(self):
        client = Client(Settings(retries=0))
        client.session = Mock()
        response = Mock(status_code=200, ok=True, text="<html>ok</html>")
        client.session.get.return_value = response
        self.assertEqual(client.get(URL, as_text=True), "<html>ok</html>")
        response.json.assert_not_called()

    def test_html_403_still_raises(self):
        client = Client(Settings(retries=0))
        client.session = Mock()
        client.session.get.return_value = Mock(status_code=403, ok=False)
        with self.assertRaises(RemoteError):
            client.get(URL, as_text=True)


class RipleyIntegrationTests(unittest.TestCase):
    def test_existing_retailer_filter_independent(self):
        s = Settings(seller_id="pv", oechsle_seller_id="oe", ripley_seller_id="rip")
        self.assertEqual(s.for_retailer("ripley").seller_id, "rip")
        self.assertEqual(s.for_retailer("oechsle").seller_id, "oe")
        self.assertEqual(s.for_retailer("plazavea").seller_id, "pv")

    @patch("monitor.settings.load_dotenv")
    def test_enable_three_retailers(self, dotenv):
        with patch.dict(
            "os.environ", {"RETAILERS": "plazavea,oechsle,ripley"}, clear=True
        ):
            s = Settings.load()
        self.assertEqual(s.retailers, ["plazavea", "oechsle", "ripley"])

    @patch("monitor.app.PRODUCTOS", [])
    def test_restock_preserves_other_retailers_and_records_ripley_link(self):
        store = Store(":memory:")
        a = Ripley()
        p = a.normalize_native(native(), CatalogHTML(listing()))
        store.observe(p, Stock("AGOTADO"))
        a.client = Mock()
        a.client.get.return_value = detail_html()
        telegram = Mock()
        telegram.settings = Settings(token="", chat_id="")
        cycle(Settings(), store, a, telegram, Mock(), retailer="Ripley")
        row = store.db.execute("SELECT payload FROM events").fetchone()
        event = json.loads(row[0])
        self.assertEqual(event["tipo"], "REPOSICION")
        self.assertEqual(event["tienda"], "Ripley")
        self.assertEqual(event["url"], URL)
        store.close()

    def test_discovery_independent(self):
        store = Store(":memory:")
        with patch("monitor.store.time.time", return_value=100):
            store.mark_discovery(successful=True, retailer="Oechsle")
            store.mark_discovery(successful=True, retailer="Plaza Vea")
        self.assertTrue(
            store.discovery_due(14400, 900, timestamp=101, retailer="Ripley")
        )
        self.assertFalse(
            store.discovery_due(14400, 900, timestamp=101, retailer="Plaza Vea")
        )
        store.close()


if __name__ == "__main__":
    unittest.main()
