"""Catalog identity regression from user-provided metadata and verified images."""

import copy
import unittest
from unittest.mock import Mock, patch
from monitor.settings import Settings
from monitor.models import Stock
from monitor.store import Store
from monitor.app import cycle
from tiendas.plaza_vea import PlazaVea
from tiendas.oechsle import Oechsle
from tiendas.vtex import classify


def catalog(figure=False):
    return {
        "productId": "102355697" if figure else "102355701",
        "productName": (
            "Colección con Figura TCG 30 Aniversario Inglés"
            if figure
            else "Colección Ultra Premium TCG 30 Aniversario Inglés"
        ),
        "brand": "TCG",
        "description": "",
        "link": "https://www.plazavea.com.pe/"
        + (
            "coleccion-con-figura-tcg-30-aniversario-ingles-20711637/p"
            if figure
            else "coleccion-ultra-premium-tcg-30-aniversario-ingles-20711636/p"
        ),
        "allSpecifications": ["Modelo", "Detalle De Variante", "Vendido por"],
        "Modelo": ["10-10452-120" if figure else "10-10448-120"],
        "Detalle De Variante": ["MEWTWO\n\nMEW" if figure else "DIA \n\nNOCHE"],
        "Vendido por": ["plazaVea"],
    }


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(context_confirmed=True, terms=["pokemon tcg 30"])
        self.adapter = PlazaVea(self.settings, Mock())

    def test_upc_classified_by_verified_model_and_family(self):
        p = self.adapter.normalize(catalog())
        self.assertEqual(p.classification, "CANDIDATO")
        self.assertEqual(
            p.raw["_identity_evidence"]["source"], "verified_pokemon_model"
        )

    def test_figure_classified_from_character_variant(self):
        p = self.adapter.normalize(catalog(True))
        self.assertEqual(p.classification, "CANDIDATO")
        self.assertEqual(p.raw["_identity_evidence"]["source"], "pokemon_variant")

    def test_no_manual_product_id_or_url_whitelist(self):
        r = catalog(True)
        r["productId"] = "9999"
        r["link"] = "https://www.plazavea.com.pe/other-publication/p"
        r["Modelo"] = ["new-model"]
        self.assertEqual(self.adapter.normalize(r).classification, "CANDIDATO")

    def test_generic_tcg_remains_pending(self):
        r = catalog()
        r["Modelo"] = ["unknown"]
        r["Detalle De Variante"] = ["DIA NOCHE"]
        self.assertEqual(self.adapter.normalize(r).classification, "POR_VERIFICAR")

    def test_model_must_match_product_family(self):
        r = catalog()
        r["productName"] = "Otra caja TCG 30 aniversario ingles"
        self.assertEqual(self.adapter.normalize(r).classification, "POR_VERIFICAR")

    def test_language_exclusions_preserved(self):
        for figure in (True, False):
            r = catalog(figure)
            r["productName"] = r["productName"].replace("Inglés", "Español")
            self.assertEqual(self.adapter.normalize(r).classification, "DESCARTADO")

    def test_unknown_language_and_other_edition_remain_pending(self):
        for field in ["language", "edition"]:
            r = catalog(True)
            r["productName"] = (
                r["productName"].replace("Inglés", "")
                if field == "language"
                else r["productName"].replace("30 Aniversario", "25 Aniversario")
            )
            self.assertEqual(self.adapter.normalize(r).classification, "POR_VERIFICAR")

    def test_other_games_not_accepted_from_model_or_character(self):
        for figure in (True, False):
            r = catalog(figure)
            r["productName"] = "Yu-Gi-Oh " + r["productName"]
            self.assertEqual(self.adapter.normalize(r).classification, "POR_VERIFICAR")

    def test_raw_metadata_preserved(self):
        r = catalog()
        before = copy.deepcopy(r)
        p = self.adapter.normalize(r)
        self.assertEqual(r, before)
        self.assertEqual(p.name, r["productName"])
        self.assertEqual(p.raw["description"], "")

    def test_shared_classifier_and_oechsle_not_relaxed(self):
        r = catalog(True)
        self.assertEqual(classify(r, self.settings.exclusions), "POR_VERIFICAR")
        r["link"] = r["link"].replace("www.plazavea.com.pe", "www.oechsle.pe")
        r["Vendido por"] = ["Oechsle"]
        self.assertEqual(
            Oechsle(self.settings, Mock()).normalize(r).classification, "POR_VERIFICAR"
        )

    def test_manual_retirement_still_prevents_monitoring(self):
        store = Store(":memory:")
        p = self.adapter.normalize(catalog())
        store.observe(p, Stock("AGOTADO"))
        store.retire_product(p.key)
        adapter = Mock()
        adapter.search.return_value.products = [p]
        adapter.search.return_value.complete = True
        with patch("monitor.app.PRODUCTOS", []), patch(
            "monitor.app.deliver", return_value=True
        ):
            cycle(self.settings, store, adapter, Mock(), Mock(), discover=True)
        adapter.details.assert_not_called()
        adapter.stock.assert_not_called()
        store.close()

    def test_discovery_promotes_pending_entries_and_preserves_history(self):
        store = Store(":memory:")
        for figure in (True, False):
            p = self.adapter.normalize(catalog(figure))
            p.classification = "POR_VERIFICAR"
            store.observe(p, Stock("AGOTADO"))
        client = Mock()
        client.get.return_value = [catalog(), catalog(True)]
        adapter = PlazaVea(self.settings, client)
        with patch("monitor.app.PRODUCTOS", []), patch(
            "monitor.app.deliver", return_value=True
        ), patch.object(adapter, "stock", return_value=Stock("AGOTADO")):
            cycle(self.settings, store, adapter, Mock(), Mock(), discover=True)
        self.assertEqual(
            [p.classification for p in store.products()], ["CANDIDATO", "CANDIDATO"]
        )
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )
        store.close()
