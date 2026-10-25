"""Datos Shopify simulados; las pruebas no consultan Ilahui ni envían Telegram."""

import copy
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from monitor.app import cycle, scheduled_cycle
from monitor.models import Product, Stock, normalize_url
from monitor.settings import Settings
from monitor.store import Store
from monitor.http import RemoteError
from tiendas.ilahui import Ilahui, IlahuiHTML

URL = "https://ilahuiperu.com/products/pokemon-tcg-30-aniversario-ingles"


def fixture():
    return {
        "id": 1234567890,
        "title": "Pokemon TCG 30 Aniversario Inglés",
        "handle": "pokemon-tcg-30-aniversario-ingles",
        "vendor": "Ilahui Perú",
        "type": "Coleccionables",
        "description": "Producto en English del 30 aniversario.",
        "tags": ["Pokemon"],
        "available": True,
        "price": 19990,
        "variants": [
            {"id": 777, "title": "Default Title", "available": True, "price": 19990}
        ],
    }


def listing(next_page=False):
    return (
        '<html><script>Shopify.currency = {"active":"PEN","rate":"1.0"};</script><a href="'
        + URL
        + '?variant=777">Pokemon TCG 30 Aniversario Inglés</a>'
        + ('<a rel="next" href="/search?page=2">2</a>' if next_page else "")
        + "</html>"
    )


class IlahuiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings(terms=["pokemon tcg 30"]).for_retailer("ilahui")
        self.a = Ilahui(self.s, Mock())

    def test_url_normalizes_collection_alias_tracking_and_variant(self):
        self.assertEqual(
            normalize_url(
                "https://ilahuiperu.com/collections/pokemon/products/pokemon-tcg-30-aniversario-ingles?variant=777#x",
                "Ilahui",
            ),
            URL,
        )

    def test_foreign_domain_rejected(self):
        with self.assertRaises(ValueError):
            normalize_url("https://evil.example/products/pokemon", "Ilahui")

    def test_search_url_is_not_product(self):
        with self.assertRaises(ValueError):
            normalize_url("https://ilahuiperu.com/search?q=pokemon", "Ilahui")

    def test_normalize_identity_and_metadata(self):
        p = self.a.normalize(fixture())
        self.assertEqual(
            (p.key, p.url, p.classification), ("ilahui:1234567890", URL, "CANDIDATO")
        )

    def test_spanish_excluded(self):
        data = fixture()
        data["title"] = "Pokemon TCG 30 aniversario Español"
        self.assertEqual(self.a.normalize(data).classification, "DESCARTADO")

    def test_missing_edition_pending(self):
        data = fixture()
        data["title"] = "Pokemon TCG English"
        data["description"] = "Otro producto"
        self.assertEqual(self.a.normalize(data).classification, "POR_VERIFICAR")

    def test_variant_language_and_product_type_used(self):
        data = fixture()
        data["title"] = "Pokemon 30 aniversario"
        data["description"] = ""
        data["type"] = "TCG"
        data["variants"][0]["title"] = "English"
        self.assertEqual(self.a.normalize(data).classification, "CANDIDATO")

    def test_details_use_public_product_json_and_known_currency(self):
        self.a.client.get.return_value = fixture()
        p = self.a.details(
            Product(
                "1234567890", "", URL, retailer="Ilahui", raw={"_store_currency": "PEN"}
            )
        )
        self.a.client.get.assert_called_once_with(URL + ".js")
        self.assertEqual(p.raw["_store_currency"], "PEN")

    def test_wrong_product_id_rejected(self):
        self.a.client.get.return_value = fixture()
        with self.assertRaises(ValueError):
            self.a.details(Product("999", "", URL, retailer="Ilahui"))

    def test_cross_retailer_rejected_before_request(self):
        with self.assertRaises(ValueError):
            self.a.details(
                Product("123", "", "https://www.oechsle.pe/test/p", retailer="Oechsle")
            )
        self.a.client.get.assert_not_called()

    def test_currency_extracted_without_eval(self):
        self.assertEqual(IlahuiHTML(listing()).currency, "PEN")
        self.assertEqual(
            IlahuiHTML(
                '<meta property="product:price:currency" content="PEN">'
            ).currency,
            "PEN",
        )

    def test_currency_absent_not_assumed(self):
        p = self.a.normalize(fixture())
        self.assertIsNone(self.a.stock(p).currency)

    def test_price_in_cents_and_available(self):
        data = fixture()
        data["_store_currency"] = "PEN"
        s = self.a.stock(self.a.normalize(data))
        self.assertEqual((s.state, s.price, s.currency), ("DISPONIBLE", 199.9, "PEN"))

    def test_all_variants_false_exhausted(self):
        data = fixture()
        data["available"] = False
        data["variants"][0]["available"] = False
        self.assertEqual(self.a.stock(self.a.normalize(data)).state, "AGOTADO")

    def test_false_plus_unknown_not_exhausted(self):
        data = fixture()
        data["available"] = False
        data["variants"][0]["available"] = False
        data["variants"].append({"id": 778})
        self.assertEqual(self.a.stock(self.a.normalize(data)).state, "DESCONOCIDO")

    def test_parent_and_variant_contradiction_unknown(self):
        data = fixture()
        data["available"] = False
        self.assertEqual(self.a.stock(self.a.normalize(data)).state, "DESCONOCIDO")

    def test_missing_variants_unknown(self):
        data = fixture()
        data["variants"] = []
        self.assertEqual(self.a.stock(self.a.normalize(data)).state, "DESCONOCIDO")

    def test_unknown_variant_filter_not_exhausted(self):
        self.s.sku_id = "missing"
        self.assertEqual(self.a.stock(self.a.normalize(fixture())).state, "DESCONOCIDO")

    def test_search_preserves_supplied_options(self):
        self.a.client.get.side_effect = [listing(), fixture()]
        r = self.a.search()
        self.assertTrue(r.complete)
        self.assertEqual(len(r.products), 1)
        options = self.a.client.get.call_args_list[0].args[1]
        self.assertEqual(options["options[unavailable_products]"], "last")
        self.assertEqual(options["options[prefix]"], "last")
        self.assertEqual(
            options["options[fields]"], "title,vendor,product_type,variants.title"
        )
        self.assertEqual(options["q"], "pokemon tcg 30")

    def test_same_product_across_terms_fetched_once(self):
        self.s.terms = ["pokemon", "pokemon tcg"]
        self.a.client.get.side_effect = [listing(), fixture(), listing()]
        self.assertEqual(len(self.a.search().products), 1)
        self.assertEqual(self.a.client.get.call_count, 3)

    def test_pagination_preserves_query_and_options(self):
        self.a.client.get.side_effect = [
            listing(True),
            fixture(),
            "<html>0 resultados</html>",
        ]
        self.assertTrue(self.a.search().complete)
        opts = self.a.client.get.call_args.args[1]
        self.assertEqual(opts["page"], 2)
        self.assertEqual(opts["q"], "pokemon tcg 30")
        self.assertEqual(opts["options[unavailable_products]"], "last")

    def test_no_results_completes_without_more_sources(self):
        self.a.client.get.return_value = (
            "<title>Búsqueda: 0 resultados encontrados</title>"
        )
        r = self.a.search()
        self.assertTrue(r.complete)
        self.assertEqual(r.products, [])
        self.assertEqual(self.a.client.get.call_count, 1)

    def test_unrecognized_html_not_false_empty_success(self):
        self.a.client.get.return_value = "<html>CAPTCHA</html>"
        self.assertFalse(self.a.search().complete)

    def test_403_is_incomplete_not_stock_zero(self):
        self.a.client.get.side_effect = RemoteError(403)
        r = self.a.search()
        self.assertFalse(r.complete)
        self.assertIn("HTTP 403", r.errors)


class IlahuiIntegrationTests(unittest.TestCase):
    @patch("monitor.settings.load_dotenv")
    def test_configuration_and_intervals_isolated(self, dotenv):
        with patch.dict(
            "os.environ", {"RETAILERS": "plazavea,oechsle,ripley,ilahui"}, clear=True
        ):
            s = Settings.load()
        self.assertEqual(s.for_retailer("ilahui").discovery_interval, 120)
        self.assertEqual(s.for_retailer("plazavea").discovery_interval, 14400)
        self.assertEqual(s.for_retailer("oechsle").discovery_interval, 120)
        self.assertEqual(s.for_retailer("ripley").discovery_interval, 120)

    @patch("monitor.settings.load_dotenv")
    def test_invalid_interval_rejected(self, dotenv):
        with patch.dict("os.environ", {"ILAHUI_DISCOVERY_INTERVAL": "0"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.load()

    def test_own_namespace_and_restock_event(self):
        store = Store(":memory:")
        p = Ilahui().normalize(fixture())
        store.observe(p, Stock("AGOTADO"))
        self.assertEqual(
            store.observe(p, Stock("DISPONIBLE", 199.9, "PEN")), "REPOSICION"
        )
        event = json.loads(store.db.execute("SELECT payload FROM events").fetchone()[0])
        self.assertEqual(event["tienda"], "Ilahui")
        self.assertEqual(event["url"], URL)
        self.assertIsNone(store.observe(p, Stock("DISPONIBLE")))
        store.close()

    @patch("monitor.app.cycle", return_value=True)
    def test_schedule_two_minutes_without_resetting_plaza(self, cycle):
        store = Store(":memory:")
        with patch("monitor.store.time.time", return_value=100):
            store.mark_discovery(successful=True, retailer="Ilahui")
            store.mark_discovery(successful=True, retailer="Plaza Vea")
        with patch("monitor.store.time.time", return_value=220):
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Ilahui"
            )
            self.assertTrue(cycle.call_args.kwargs["discover"])
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Plaza Vea"
            )
            self.assertFalse(cycle.call_args.kwargs["discover"])
        store.close()


if __name__ == "__main__":
    unittest.main()
