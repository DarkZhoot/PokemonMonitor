import contextlib
import io
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from monitor.app import cycle, main
from monitor.classification import assess
from monitor.models import SearchResult, Stock
from monitor.settings import Settings
from monitor.store import Store
from tiendas.plaza_vea import PlazaVea
from tiendas.oechsle import Oechsle
from tiendas.ripley import Ripley
from tiendas.falabella import Falabella
from tiendas.tailoy import Tailoy
from tiendas.ilahui import Ilahui

TITLE = "Colección Ultra Premium TCG 30 Aniversario Inglés"


def products(title=TITLE, model="10-10448-120", variants="", marketplace=False):
    settings = Settings(context_confirmed=True)
    raw = {
        "productId": "123",
        "productName": title,
        "brand": "TCG",
        "description": "",
        "Modelo": [model],
        "Detalle De Variante": [variants],
        "allSpecifications": ["Modelo", "Detalle De Variante", "Vendido por"],
        "Vendido por": ["Other" if marketplace else "Oechsle"],
    }
    for cls, url in [
        (PlazaVea, "https://www.plazavea.com.pe/example/p"),
        (Oechsle, "https://www.oechsle.pe/example/p"),
    ]:
        yield cls(settings, Mock()).normalize(dict(raw, link=url))
    for cls, url, seller in [
        (Ripley, "https://simple.ripley.com.pe/example-2032376641981p", "Ripley"),
        (
            Falabella,
            "https://www.falabella.com.pe/falabella-pe/product/123/example",
            "Falabella",
        ),
        (Tailoy, "https://www.tailoy.com.pe/example.html", "Tai Loy"),
    ]:
        schema = {
            "@type": "Product",
            "name": title,
            "brand": {"name": "TCG"},
            "description": "",
            "url": url,
            "sku": "merchant123",
            "mpn": model,
            "hasVariant": [{"name": variants}],
            "offers": {"seller": {"name": "Other" if marketplace else seller}},
        }
        yield cls(settings, Mock()).normalize(schema)
    yield Ilahui(settings, Mock()).normalize(
        {
            "id": 123,
            "handle": "example",
            "title": title,
            "vendor": "TCG",
            "description": "",
            "variants": [{"title": variants, "sku": model}],
        }
    )


class SharedClassificationTests(unittest.TestCase):
    def test_verified_model_works_for_all_six_retailers(self):
        for p in products():
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "CANDIDATO")
                self.assertEqual(p.raw["_classification"]["missing"], [])
                self.assertEqual(
                    p.raw["_identity_evidence"]["source"], "verified_pokemon_model"
                )
                self.assertEqual(p.name, TITLE)

    def test_figure_variants_work_without_model_for_all_six(self):
        for p in products(
            "Colección con Figura TCG 30 Aniversario Inglés", "unknown", "MEWTWO\nMEW"
        ):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "CANDIDATO")
                self.assertEqual(
                    p.raw["_identity_evidence"]["source"], "pokemon_variant"
                )

    def test_generic_tcg_stays_pending_with_explicit_reason(self):
        for p in products(model="unknown"):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "POR_VERIFICAR")
                self.assertIn("identidad Pokemon", p.raw["_classification"]["missing"])
                self.assertTrue(p.raw["_classification"]["review_needed"])

    def test_spanish_excluded_everywhere_even_with_verified_model(self):
        for p in products(TITLE.replace("Inglés", "Español")):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "DESCARTADO")

    def test_missing_language_stays_pending_everywhere(self):
        for p in products(TITLE.replace("Inglés", "")):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "POR_VERIFICAR")
                self.assertIn("idioma ingles", p.raw["_classification"]["missing"])

    def test_other_edition_stays_pending_everywhere(self):
        for p in products(TITLE.replace("30 Aniversario", "25 Aniversario")):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "POR_VERIFICAR")
                self.assertIn("30 aniversario", p.raw["_classification"]["missing"])

    def test_other_games_conflict_not_overridden_by_pokemon_character(self):
        for p in products("Yu-Gi-Oh " + TITLE, variants="MEWTWO"):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "POR_VERIFICAR")

    def test_model_number_prefix_not_proof(self):
        for p in products(model="10-10448-120999"):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "POR_VERIFICAR")

    def test_wrong_product_family_not_proof(self):
        for p in products(TITLE.replace("Ultra Premium", "Extraña")):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "POR_VERIFICAR")

    def test_seller_restrictions_remain_in_force(self):
        for p in products(marketplace=True):
            if p.retailer in ("Oechsle", "Ripley", "Falabella"):
                with self.subTest(retailer=p.retailer):
                    self.assertEqual(p.classification, "DESCARTADO")

    def test_product_review_deduplicated_and_updates_on_change(self):
        store = Store(":memory:")
        p = next(products(model="unknown"))
        adapter = Mock()
        adapter.search.return_value = SearchResult(products=[p])
        log = Mock()
        with patch("monitor.app.PRODUCTOS", []), patch(
            "monitor.app.deliver", return_value=True
        ):
            for _ in range(3):
                cycle(Settings(), store, adapter, Mock(), log, discover=True)
            review = [
                c for c in log.call_args_list if c.args[0] == "product_review_needed"
            ]
            self.assertEqual(len(review), 1)
            self.assertEqual(review[0].kwargs["missing"], ["identidad Pokemon"])
            p.name += " nueva ficha"
            cycle(Settings(), store, adapter, Mock(), log, discover=True)
            self.assertEqual(
                len(
                    [
                        c
                        for c in log.call_args_list
                        if c.args[0] == "product_review_needed"
                    ]
                ),
                2,
            )
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )
        store.close()

    def test_pending_list_shows_reasons_and_excludes_retired(self):
        with TemporaryDirectory() as tmp:
            settings = replace(Settings(), data_dir=Path(tmp))
            store = Store(Path(tmp) / "monitor.sqlite3")
            with store.db:
                store.db.execute("INSERT INTO meta VALUES('migrated','1')")
            for p in products(model="unknown"):
                store.observe(p, Stock())
            store.retire_product("plazavea:123")
            store.close()
            output = io.StringIO()
            with patch(
                "sys.argv", ["main.py", "--list-products", "--include-pending"]
            ), patch("monitor.app.Settings.load", return_value=settings), patch(
                "monitor.app.configure", return_value=Mock()
            ), contextlib.redirect_stdout(
                output
            ):
                self.assertEqual(main(), 0)
            rows = json.loads(output.getvalue())
            self.assertEqual(len(rows), 5)
            self.assertTrue(all(r["clasificacion"] == "POR_VERIFICAR" for r in rows))
            self.assertTrue(all("identidad Pokemon" in r["pendiente"] for r in rows))

    def test_urls_and_review_text_not_identity_evidence(self):
        raw = {
            "productName": TITLE,
            "brand": "TCG",
            "url": "https://shop.test/pokemon",
            "reviews": [{"text": "Pokemon"}],
        }
        self.assertEqual(assess(raw, [])["classification"], "POR_VERIFICAR")

    def test_html_entities_in_identity_language_and_exclusions(self):
        raw = {"productName": "Pok&eacute;mon TCG 30 aniversario ingl&eacute;s"}
        self.assertEqual(assess(raw, ["espanol"])["classification"], "CANDIDATO")
        raw["productName"] = "Pok&eacute;mon TCG 30 aniversario espa&ntilde;ol"
        self.assertEqual(assess(raw, ["espanol"])["classification"], "DESCARTADO")

    def test_native_listing_enrichment_limited_and_persistent(self):
        from monitor.app import enrich_listing_metadata
        from monitor.models import Product

        store = Store(":memory:")
        adapter = Mock()
        items = []
        for i in range(5):
            metadata = {"productName": TITLE, "brand": "TCG"}
            decision = assess(metadata, [])
            p = Product(
                str(i),
                TITLE,
                "https://example.test/product",
                "POR_VERIFICAR",
                "Ripley",
                {"native": {}, "_classification": decision},
            )
            items.append(p)
        adapter.details.side_effect = lambda p: p
        result = SearchResult(products=items)
        enrich_listing_metadata(result, adapter, store, set(), Mock(), "Ripley")
        self.assertEqual(adapter.details.call_count, 3)
        enrich_listing_metadata(result, adapter, store, set(), Mock(), "Ripley")
        self.assertEqual(adapter.details.call_count, 5)
        enrich_listing_metadata(result, adapter, store, set(), Mock(), "Ripley")
        self.assertEqual(adapter.details.call_count, 5)
        store.close()

    def test_native_enrichment_stops_on_http_block_without_promoting(self):
        from monitor.app import enrich_listing_metadata
        from monitor.models import Product
        from monitor.http import RemoteError

        store = Store(":memory:")
        adapter = Mock()
        adapter.details.side_effect = RemoteError(403)
        decision = assess({"productName": TITLE, "brand": "TCG"}, [])
        items = [
            Product(
                str(i),
                TITLE,
                "https://example.test/product",
                "POR_VERIFICAR",
                "Ripley",
                {"native": {}, "_classification": decision},
            )
            for i in range(4)
        ]
        result = SearchResult(products=items)
        enrich_listing_metadata(result, adapter, store, set(), Mock(), "Ripley")
        self.assertEqual(adapter.details.call_count, 1)
        self.assertTrue(
            all(p.classification == "POR_VERIFICAR" for p in result.products)
        )
        store.close()

    def test_more_distinctive_pokemon_variants_are_supported(self):
        for p in products(
            "Colección con Figura TCG 30 Aniversario Inglés",
            "unknown",
            "PIKACHU CHARIZARD",
        ):
            with self.subTest(retailer=p.retailer):
                self.assertEqual(p.classification, "CANDIDATO")

    def test_review_deduplication_survives_reopening_database(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            p = next(products(model="unknown"))
            store = Store(path)
            self.assertTrue(store.note_classification(p))
            store.close()
            store = Store(path)
            self.assertFalse(store.note_classification(p))
            store.close()

    def test_case_and_accents_in_model_property_label(self):
        metadata = {
            "productName": TITLE,
            "brand": "TCG",
            "CÓDIGO DE MODELO": ["10-10448-120"],
            "allSpecifications": ["CÓDIGO DE MODELO"],
        }
        self.assertEqual(assess(metadata, [])["classification"], "CANDIDATO")
