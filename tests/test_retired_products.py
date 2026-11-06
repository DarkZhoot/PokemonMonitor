import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from monitor.models import Product, Stock, SearchResult
from monitor.store import Store
from monitor.app import cycle
from monitor.settings import Settings


class RetiredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")
        self.p = Product(
            "102372340",
            "Pokemon TCG 30 aniversario ingles",
            "https://www.plazavea.com.pe/old-product/p",
            "CANDIDATO",
        )
        self.store.observe(self.p, Stock("AGOTADO"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_retirement_preserves_stock_history_and_blocks_new_events(self):
        self.store.retire_product(self.p.key)
        self.assertIsNone(self.store.observe(self.p, Stock("DISPONIBLE")))
        row = self.store.db.execute(
            "SELECT last_known FROM products WHERE key=?", (self.p.key,)
        ).fetchone()
        self.assertEqual(row[0], "AGOTADO")
        self.assertEqual(self.store.products()[0].key, self.p.key)
        self.assertIn(self.p.key, self.store.retired_keys())

    def test_pending_cancelled_sent_history_preserved(self):
        self.store.observe(self.p, Stock("DISPONIBLE"))
        with self.store.db:
            self.store.db.execute("UPDATE events SET status='sent'")
        self.store.observe(self.p, Stock("AGOTADO"))
        self.store.observe(self.p, Stock("DISPONIBLE"))
        self.store.retire_product(self.p.key)
        statuses = [r[0] for r in self.store.db.execute("SELECT status FROM events")]
        self.assertCountEqual(statuses, ["sent", "cancelled"])

    def test_rediscovery_and_seed_cannot_reactivate_retired(self):
        self.store.retire_product(self.p.key)
        adapter = Mock()
        adapter.search.return_value = SearchResult(products=[self.p])
        seed = {"tienda": "Plaza Vea", "id": self.p.id, "url": self.p.url}
        with patch("monitor.app.PRODUCTOS", [seed]), patch(
            "monitor.app.deliver", return_value=True
        ):
            cycle(Settings(), self.store, adapter, Mock(), Mock(), discover=True)
        adapter.details.assert_not_called()
        adapter.stock.assert_not_called()
        self.assertEqual(
            self.store.db.execute("SELECT last_known FROM products").fetchone()[0],
            "AGOTADO",
        )

    def test_restore_allows_future_restock(self):
        self.store.retire_product(self.p.key)
        self.store.restore_product(self.p.key)
        self.assertNotIn(self.p.key, self.store.retired_keys())
        self.assertEqual(self.store.observe(self.p, Stock("DISPONIBLE")), "REPOSICION")

    def test_unknown_key_not_silently_retired(self):
        with self.assertRaises(ValueError):
            self.store.retire_product("plazavea:missing")
        self.assertFalse(self.store.retired_keys())

    def test_alias_with_different_key_does_not_retire_healthy_product(self):
        healthy = Product(
            "102355632", "Album", "https://www.plazavea.com.pe/new-album/p", "CANDIDATO"
        )
        self.store.observe(healthy, Stock("AGOTADO"))
        self.store.retire_product(self.p.key)
        self.assertEqual(self.store.observe(healthy, Stock("DISPONIBLE")), "REPOSICION")
