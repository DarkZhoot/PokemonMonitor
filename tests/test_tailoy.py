"""Fixtures públicas simuladas Magento; no se consulta la red en las pruebas."""

import json
import unittest
from unittest.mock import Mock, patch
from monitor.app import scheduled_cycle
from monitor.http import RemoteError
from monitor.models import Product, Stock, normalize_url
from monitor.settings import Settings
from monitor.store import Store
from tiendas.tailoy import Tailoy, TailoyHTML, BASE_URL, SEARCH_PATH

URL = BASE_URL + "/pokemon-tcg-30-aniversario-ingles-12345.html"
NAME = "Pokémon TCG 30.º Aniversario en Inglés"


def schema(availability="https://schema.org/InStock"):
    return {
        "@type": "Product",
        "url": URL,
        "sku": "12345",
        "name": NAME,
        "description": "30th anniversary English",
        "offers": {
            "@type": "Offer",
            "price": "199.90",
            "priceCurrency": "PEN",
            "availability": availability,
        },
    }


def schema_html(raw=None):
    return (
        '<link rel="canonical" href="'
        + URL
        + '"><script type="application/ld+json">'
        + json.dumps(schema() if raw is None else raw)
        + "</script>"
    )


def micro(preorder=False, sku=True):
    button = (
        'class="preorder-product" title="Pre-Venta"'
        if preorder
        else 'class="action primary tocart"'
    )
    label = "NO DISPONIBLE" if preorder else "Agregar"
    attr = 'data-product-sku="12345"' if sku else ""
    return f"""<link rel="canonical" href="{URL}"><div class="product-info-main">
    <h1><span itemprop="name" data-ui-id="page-title-wrapper">{NAME}</span></h1>
    <div class="label-des"><p>Pokemon TCG 30 aniversario English</p></div>
    <span itemprop="offers"><meta itemprop="price" content="199.9"><meta itemprop="priceCurrency" content="PEN"></span>
    <form id="product_addtocart_form" {attr}><input name="product" value="999">
    <input name="form_key" value="ignored-private-csrf-token">
    <button id="product-addtocart-button" {button}>{label}</button></form></div>"""


def backend(status="IN_STOCK", sku="12345", kind="SimpleProduct"):
    return {
        "data": {
            "products": {
                "items": [
                    {
                        "sku": sku,
                        "name": NAME,
                        "__typename": kind,
                        "stock_status": status,
                        "price_range": {
                            "minimum_price": {
                                "final_price": {"value": 199.9, "currency": "PEN"}
                            }
                        },
                    }
                ]
            }
        }
    }


def listing(next_page=False):
    return f'<a class="product-item-link" href="{URL}">{NAME}</a>' + (
        '<a rel="next" href="' + BASE_URL + SEARCH_PATH + '?p=2">2</a>'
        if next_page
        else ""
    )


class TailoyTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings(terms=["pokemon tcg 30 ingles"]).for_retailer("tailoy")
        self.a = Tailoy(self.s, Mock())

    def product(self):
        return Product("12345", "", URL, retailer="Tai Loy")

    def test_normalizes_canonical_and_tracking(self):
        self.assertEqual(normalize_url(URL + "?q=tracking#x", "Tai Loy"), URL)

    def test_external_url_rejected(self):
        with self.assertRaises(ValueError):
            normalize_url("https://evil.example/product.html", "Tai Loy")

    def test_search_page_not_product(self):
        with self.assertRaises(ValueError):
            normalize_url(BASE_URL + SEARCH_PATH, "Tai Loy")

    def test_jsonld_identity_classification_and_stock(self):
        self.a.client.get.return_value = schema_html()
        p = self.a.details(self.product())
        self.assertEqual((p.key, p.classification), ("tailoy:12345", "CANDIDATO"))
        self.assertEqual(
            (self.a.stock(p).state, self.a.stock(p).price, self.a.stock(p).currency),
            ("DISPONIBLE", 199.9, "PEN"),
        )
        self.a.client.get.assert_called_once()

    def test_metadata_excludes_spanish(self):
        raw = schema()
        raw["name"] = "Pokemon TCG 30 aniversario Español"
        self.assertEqual(self.a.normalize(raw).classification, "DESCARTADO")

    def test_missing_edition_pending(self):
        raw = schema()
        raw["name"] = "Pokemon TCG English"
        raw["description"] = "Otra edición"
        self.assertEqual(self.a.normalize(raw).classification, "POR_VERIFICAR")

    def test_microdata_capture(self):
        p = TailoyHTML(micro())
        self.assertEqual(p.product_sku, "12345")
        self.assertEqual(p.product_id, "999")
        self.assertEqual("".join(p.primary_name), NAME)
        self.assertEqual(p.micro_offer["priceCurrency"], "PEN")
        self.assertNotIn("ignored-private-csrf-token", str(p.micro_offer))

    def test_microdata_uses_public_read_only_graphql(self):
        self.a.client.get.side_effect = [micro(), backend()]
        p = self.a.details(self.product())
        self.assertEqual(self.a.stock(p).state, "DISPONIBLE")
        call = self.a.client.get.call_args
        self.assertEqual(call.args[0], BASE_URL + "/graphql")
        self.assertEqual(json.loads(call.args[1]["variables"]), {"sku": "12345"})
        self.assertIn("query(", call.args[1]["query"])
        self.assertNotIn("mutation", call.args[1]["query"])

    def test_preorder_never_available_even_if_backend_would_say_in_stock(self):
        self.a.client.get.return_value = micro(preorder=True)
        p = self.a.details(self.product())
        self.assertEqual(self.a.stock(p).state, "DESCONOCIDO")
        self.a.client.get.assert_called_once()
        p.raw["_graphql_stock"] = backend()["data"]["products"]["items"][0]
        self.assertEqual(self.a.stock(p).state, "DESCONOCIDO")

    def test_out_of_stock_backend(self):
        self.a.client.get.side_effect = [micro(), backend("OUT_OF_STOCK")]
        self.assertEqual(self.a.stock(self.a.details(self.product())).state, "AGOTADO")

    def test_backend_wrong_sku_not_used(self):
        self.a.client.get.side_effect = [micro(), backend(sku="different")]
        self.assertEqual(
            self.a.stock(self.a.details(self.product())).state, "DESCONOCIDO"
        )

    def test_graphql_errors_never_zero_stock(self):
        self.a.client.get.side_effect = [micro(), {"errors": [{"message": "no data"}]}]
        self.assertEqual(
            self.a.stock(self.a.details(self.product())).state, "DESCONOCIDO"
        )

    def test_unverified_variants_unknown(self):
        self.a.client.get.side_effect = [micro(), backend(kind="ConfigurableProduct")]
        self.assertEqual(
            self.a.stock(self.a.details(self.product())).state, "DESCONOCIDO"
        )

    def test_internal_form_id_not_used_as_merchant_sku(self):
        self.a.client.get.return_value = micro(sku=False)
        p = self.a.details(Product("", "", URL, retailer="Tai Loy"))
        self.assertEqual(p.id, "999")
        self.a.client.get.assert_called_once()
        self.assertEqual(self.a.stock(p).state, "DESCONOCIDO")

    def test_generic_button_alone_never_confirms_stock(self):
        self.a.client.get.side_effect = [micro(), {"data": {"products": {"items": []}}}]
        self.assertEqual(
            self.a.stock(self.a.details(self.product())).state, "DESCONOCIDO"
        )

    def test_jsonld_incomplete_offer_mixed_out_is_unknown(self):
        raw = schema()
        raw["offers"] = [{"availability": "https://schema.org/OutOfStock"}, {}]
        self.assertEqual(self.a.stock(self.a.normalize(raw)).state, "DESCONOCIDO")

    def test_no_currency_assumption(self):
        raw = schema()
        raw["offers"].pop("priceCurrency")
        self.assertIsNone(self.a.stock(self.a.normalize(raw)).currency)

    def test_foreign_retailer_rejected_before_network(self):
        with self.assertRaises(ValueError):
            self.a.details(
                Product(
                    "123", "", "https://ilahuiperu.com/products/test", retailer="Ilahui"
                )
            )
        self.a.client.get.assert_not_called()

    def test_search_preserves_brand_filter_and_omits_tracking(self):
        self.a.client.get.side_effect = [listing(), schema_html()]
        r = self.a.search()
        self.assertTrue(r.complete)
        self.assertEqual(len(r.products), 1)
        params = self.a.client.get.call_args_list[0].args[1]
        self.assertEqual(params["tailoyetl_999_marcav2"], "7212")
        self.assertEqual(params["q"], "pokemon tcg 30 ingles")
        self.assertNotIn("_gsSearchId", params)

    def test_pagination_preserves_filter(self):
        self.a.client.get.side_effect = [
            listing(True),
            schema_html(),
            "<div>No se encontraron productos</div>",
        ]
        self.assertTrue(self.a.search().complete)
        self.assertEqual(self.a.client.get.call_args.args[1]["p"], 2)
        self.assertEqual(
            self.a.client.get.call_args.args[1]["tailoyetl_999_marcav2"], "7212"
        )

    def test_no_results_no_further_exploration(self):
        self.a.client.get.return_value = "<div>Su búsqueda no arrojó resultados</div>"
        # The standard empty messages include the exact storefront response.
        r = self.a.search()
        self.assertEqual(r.products, [])
        self.assertTrue(r.complete)
        self.assertEqual(self.a.client.get.call_count, 1)

    def test_403_reports_incomplete(self):
        self.a.client.get.side_effect = RemoteError(403)
        self.assertFalse(self.a.search().complete)

    def test_normal_navigation_not_crawled(self):
        self.a.client.get.return_value = (
            '<a href="'
            + BASE_URL
            + '/juguetes.html">Juguetes</a>No se encontraron productos'
        )
        self.assertTrue(self.a.search().complete)
        self.a.client.get.assert_called_once()


class TailoyParsingSafetyTests(unittest.TestCase):
    def test_javascript_empty_text_is_not_a_search_result(self):
        a = Tailoy(Settings(terms=["pokemon"]).for_retailer("tailoy"), Mock())
        a.client.get.return_value = '<script>var message="No se encontraron productos";</script><p>Acceso no disponible</p>'
        self.assertFalse(a.search().complete)


class TailoyIntegrationTests(unittest.TestCase):
    @patch("monitor.settings.load_dotenv")
    def test_settings_default_intervals_preserve_other_stores(self, dotenv):
        with patch.dict(
            "os.environ",
            {"RETAILERS": "plazavea,oechsle,ripley,ilahui,tailoy"},
            clear=True,
        ):
            s = Settings.load()
        self.assertEqual(s.for_retailer("tailoy").discovery_interval, 120)
        self.assertEqual(s.for_retailer("plazavea").discovery_interval, 14400)
        self.assertEqual(s.for_retailer("oechsle").discovery_interval, 120)
        self.assertEqual(s.for_retailer("ripley").discovery_interval, 120)
        self.assertEqual(s.for_retailer("ilahui").discovery_interval, 120)

    @patch("monitor.app.cycle", return_value=True)
    def test_clock_independent(self, cycle):
        store = Store(":memory:")
        with patch("monitor.store.time.time", return_value=100):
            store.mark_discovery(successful=True, retailer="Tai Loy")
            store.mark_discovery(successful=True, retailer="Plaza Vea")
        with patch("monitor.store.time.time", return_value=220):
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Tai Loy"
            )
            self.assertTrue(cycle.call_args.kwargs["discover"])
            scheduled_cycle(
                Settings(), store, Mock(), Mock(), Mock(), retailer="Plaza Vea"
            )
            self.assertFalse(cycle.call_args.kwargs["discover"])
        store.close()

    def test_restock_event_uses_tailoy_link_and_namespace(self):
        store = Store(":memory:")
        p = Tailoy().normalize(schema())
        store.observe(p, Stock("AGOTADO"))
        self.assertEqual(
            store.observe(p, Stock("DISPONIBLE", 199.9, "PEN")), "REPOSICION"
        )
        event = json.loads(store.db.execute("SELECT payload FROM events").fetchone()[0])
        self.assertEqual(event["tienda"], "Tai Loy")
        self.assertEqual(event["url"], URL)
        self.assertIsNone(store.observe(p, Stock("DISPONIBLE")))
        store.close()


if __name__ == "__main__":
    unittest.main()
