import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from monitor.app import main, cycle
from monitor.models import Stock
from monitor.settings import Settings
from monitor.store import Store
from tiendas.falabella import Falabella

URL = "https://www.falabella.com.pe/falabella-pe/product/80233138/tcg-30th-ultra-pc-ing-espeon"
TITLE = "TCG30TH ULTRA PC ING ESPEON"


def schema():
    return {
        "@type": "Product",
        "name": TITLE,
        "brand": "TCG",
        "url": URL,
        "offers": {
            "seller": {"name": "Falabella"},
            "availability": "https://schema.org/InStock",
            "price": "1399",
            "priceCurrency": "PEN",
        },
    }


class TrackingURLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.settings = replace(
            Settings(), data_dir=Path(self.tmp.name), retailers=["falabella"]
        )
        self.path = Path(self.tmp.name) / "monitor.sqlite3"
        store = Store(self.path)
        with store.db:
            store.db.execute("INSERT INTO meta VALUES('migrated','1')")
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def track(self, url=URL):
        with patch(
            "sys.argv", ["main.py", "--retailer", "falabella", "--track-url", url]
        ), patch("monitor.app.Settings.load", return_value=self.settings), patch(
            "monitor.app.configure", return_value=Mock()
        ), patch(
            "requests.Session.get",
            side_effect=AssertionError("Registration must not send network requests"),
        ):
            return main()

    def test_register_preserves_url_without_inventing_stock(self):
        self.assertEqual(self.track(), 0)
        store = Store(self.path)
        p = store.products()[0]
        self.assertEqual(p.key, "falabella:80233138")
        self.assertEqual(p.url, URL)
        self.assertEqual(p.classification, "POR_VERIFICAR")
        self.assertTrue(p.raw["_falabella_watch"])
        self.assertEqual(
            store.db.execute("SELECT state FROM products").fetchone()[0], "DESCONOCIDO"
        )
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )
        store.close()

    def test_register_existing_preserves_stock_history(self):
        store = Store(self.path)
        adapter = Falabella(self.settings.for_retailer("falabella"), Mock())
        p = adapter.normalize(schema())
        p.raw["_falabella_watch"] = False
        store.observe(p, Stock("AGOTADO"))
        store.close()
        self.assertEqual(self.track(), 0)
        store = Store(self.path)
        row = store.db.execute("SELECT state,last_known FROM products").fetchone()
        self.assertEqual(tuple(row), ("AGOTADO", "AGOTADO"))
        self.assertTrue(store.products()[0].raw["_watch"])
        store.close()

    def test_retired_not_silently_restored(self):
        store = Store(self.path)
        p = Falabella(self.settings, Mock()).normalize(schema())
        store.observe(p, Stock("AGOTADO"))
        store.retire_product(p.key)
        store.close()
        self.assertEqual(self.track(), 1)
        store = Store(self.path)
        self.assertIn(p.key, store.retired_keys())
        store.close()

    def test_foreign_url_rejected(self):
        self.assertEqual(
            self.track("https://other.test/falabella-pe/product/80233138/example"), 1
        )
        store = Store(self.path)
        self.assertEqual(store.products(), [])
        store.close()

    def test_explicit_url_verifies_directly_and_queues_one_alert(self):
        self.assertEqual(self.track(), 0)
        settings = self.settings.for_retailer("falabella")
        adapter = Falabella(settings, Mock())
        verified = adapter.normalize(schema())
        verified.raw["_needs_details"] = False
        adapter.details = Mock(return_value=verified)
        store = Store(self.path)
        with patch("monitor.app.PRODUCTOS", []), patch(
            "monitor.app.deliver", return_value=True
        ):
            for _ in range(2):
                cycle(settings, store, adapter, Mock(), Mock(), retailer="Falabella")
        self.assertEqual(adapter.details.call_count, 2)
        rows = store.db.execute("SELECT payload FROM events").fetchall()
        self.assertEqual(len(rows), 1)
        event = json.loads(rows[0][0])
        self.assertEqual(event["nombre"], TITLE)
        self.assertEqual(event["estado"], "DISPONIBLE")
        self.assertEqual(
            store.db.execute("SELECT state FROM products").fetchone()[0], "DISPONIBLE"
        )
        store.close()
