import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests
from monitor.app import cycle
from monitor.http import Client, RemoteError
from monitor.locking import exclusive, AlreadyRunning
from monitor.models import Product, Stock, normalize_url
from monitor.settings import Settings, ROOT
from monitor.store import Store
from monitor.telegram import Telegram, deliver
from tiendas.plaza_vea import PlazaVea, classify

URL = "https://www.plazavea.com.pe/pokemon/p"


def raw(offers=None):
    return {
        "productId": "123",
        "productName": "Pokémon TCG 30.º Aniversario Inglés",
        "link": URL,
        "items": [
            {
                "itemId": "sku1",
                "sellers": [
                    {"sellerId": "1", "commertialOffer": o}
                    for o in (
                        offers
                        if offers is not None
                        else [
                            {"IsAvailable": True, "AvailableQuantity": 2, "Price": 25}
                        ]
                    )
                ],
            }
        ],
    }


def response(code=200, data=None, headers=None):
    r = Mock(status_code=code, ok=200 <= code < 300, headers=headers or {})
    r.json.return_value = data
    return r


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings(context_confirmed=True, terms=["pokemon"], retries=1)
        self.a = PlazaVea(self.s, Mock())

    def test_title(self):
        self.assertEqual(classify(raw(), self.s.exclusions), "CANDIDATO")

    def test_other_tcg(self):
        self.assertNotEqual(
            classify({"productName": "YuGiOh TCG 30 aniversario ingles"}, []),
            "CANDIDATO",
        )

    def test_missing_language(self):
        self.assertEqual(
            classify({"productName": "Pokemon TCG 30 aniversario"}, []), "POR_VERIFICAR"
        )

    def test_missing_edition(self):
        self.assertEqual(
            classify({"productName": "Pokemon TCG Scarlet Violet English"}, []),
            "POR_VERIFICAR",
        )

    def test_excluded_language(self):
        self.assertEqual(
            classify(
                {"productName": "Pokemon TCG 30 aniversario English Japanese"},
                self.s.exclusions,
            ),
            "DESCARTADO",
        )

    def test_metadata(self):
        r = raw()
        r["productName"] = "Pokemon TCG"
        r.update(
            description="30th anniversary",
            allSpecifications=["Idioma"],
            Idioma=["English"],
        )
        self.assertEqual(classify(r, []), "CANDIDATO")

    def test_variants_conflict(self):
        r = raw()
        r["items"].append({"name": "Japanese"})
        self.assertEqual(classify(r, self.s.exclusions), "DESCARTADO")

    def test_normalized_url(self):
        self.assertEqual(normalize_url(URL + "/?utm_source=x#test"), URL)

    def test_external_url(self):
        with self.assertRaises(ValueError):
            normalize_url("https://evil.example/p")

    def test_available(self):
        self.assertEqual(self.a.stock(self.a.normalize(raw())).state, "DISPONIBLE")

    def test_empty_stock(self):
        self.assertEqual(
            self.a.stock(
                self.a.normalize(raw([{"IsAvailable": False, "AvailableQuantity": 0}]))
            ).state,
            "AGOTADO",
        )

    def test_partial_stock(self):
        self.assertEqual(
            self.a.stock(
                self.a.normalize(
                    raw([{"IsAvailable": False, "AvailableQuantity": 0}, {}])
                )
            ).state,
            "DESCONOCIDO",
        )

    def test_contradictory(self):
        self.assertEqual(
            self.a.stock(
                self.a.normalize(raw([{"IsAvailable": False, "AvailableQuantity": 4}]))
            ).state,
            "DESCONOCIDO",
        )

    def test_bad_quantity(self):
        self.assertEqual(
            self.a.stock(
                self.a.normalize(raw([{"IsAvailable": True, "AvailableQuantity": "5"}]))
            ).state,
            "DESCONOCIDO",
        )

    def test_context_required(self):
        self.s.context_confirmed = False
        self.assertEqual(self.a.stock(self.a.normalize(raw())).state, "DESCONOCIDO")

    def test_postal_not_guessed(self):
        self.s.postal_code = "15001"
        self.assertEqual(self.a.stock(self.a.normalize(raw())).state, "DESCONOCIDO")

    def test_seller_filter(self):
        self.s.seller_id = "other"
        self.assertEqual(self.a.stock(self.a.normalize(raw())).state, "DESCONOCIDO")

    def test_empty_search(self):
        self.a.client.get.return_value = []
        r = self.a.search()
        self.assertTrue(r.complete)
        self.assertEqual(r.products, [])

    def test_416_search(self):
        self.a.client.get.return_value = None
        self.assertTrue(self.a.search().complete)

    def test_pagination(self):
        page = [
            dict(raw(), productId=str(i), link=f"https://www.plazavea.com.pe/p{i}/p")
            for i in range(20)
        ]
        self.a.client.get.side_effect = [page, [raw()]]
        r = self.a.search()
        self.assertTrue(r.complete)
        self.assertEqual(len(r.products), 21)
        self.assertEqual(self.a.client.get.call_args_list[1].args[1]["_from"], 20)

    def test_cap_incomplete(self):
        self.s.pages = 1
        self.a.client.get.return_value = [
            dict(raw(), productId=str(i)) for i in range(20)
        ]
        self.assertFalse(self.a.search().complete)

    def test_search_blocked(self):
        self.a.client.get.side_effect = RemoteError(403)
        self.assertFalse(self.a.search().complete)

    def test_details_mismatch(self):
        self.a.client.get.return_value = [
            dict(raw(), link="https://www.plazavea.com.pe/other/p")
        ]
        with self.assertRaises(ValueError):
            self.a.details(Product("wrong", "", URL))

    def test_legacy_id_resolution(self):
        self.a.client.get.side_effect = [[], [raw()]]
        self.assertEqual(self.a.details(Product("oldsku", "", URL)).id, "123")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.c = Client(Settings(retries=2, request_delay=0))
        self.c.session = Mock()

    def test_403_not_retried(self):
        self.c.session.get.return_value = response(403)
        with self.assertRaises(RemoteError):
            self.c.get(URL)
        self.assertEqual(self.c.session.get.call_count, 1)

    def test_429_cooldown(self):
        self.c.session.get.return_value = response(429, headers={"Retry-After": "120"})
        for _ in range(2):
            with self.assertRaises(RemoteError):
                self.c.get(URL)
        self.assertEqual(self.c.session.get.call_count, 1)

    def test_416_no_retry(self):
        self.c.session.get.return_value = response(416)
        self.assertIsNone(self.c.get(URL))
        self.assertEqual(self.c.session.get.call_count, 1)

    @patch("monitor.http.time.sleep")
    def test_503_retry(self, sleep):
        self.c.session.get.side_effect = [response(503), response(200, [])]
        self.assertEqual(self.c.get(URL), [])
        self.assertEqual(sleep.call_count, 1)

    def test_invalid_json(self):
        self.c.session.get.return_value = response()
        self.c.session.get.return_value.json.side_effect = ValueError
        with self.assertRaises(RemoteError):
            self.c.get(URL)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.p = PlazaVea().normalize(raw())

    def tearDown(self):
        self.store.close()

    def test_new_once(self):
        self.assertEqual(
            self.store.observe(self.p, Stock("DISPONIBLE")), "PRODUCTO_NUEVO"
        )
        self.assertIsNone(self.store.observe(self.p, Stock("DISPONIBLE")))

    def test_restock(self):
        self.store.observe(self.p, Stock("AGOTADO"))
        self.assertEqual(self.store.observe(self.p, Stock("DISPONIBLE")), "REPOSICION")

    def test_unknown_first(self):
        self.store.observe(self.p, Stock())
        self.assertEqual(
            self.store.observe(self.p, Stock("DISPONIBLE")), "DISPONIBILIDAD_CONFIRMADA"
        )

    def test_error_not_restock(self):
        self.store.observe(self.p, Stock("DISPONIBLE"))
        self.store.observe(self.p, Stock())
        self.assertIsNone(self.store.observe(self.p, Stock("DISPONIBLE")))

    def test_unknown_preserves_exhausted(self):
        self.store.observe(self.p, Stock("AGOTADO"))
        self.store.observe(self.p, Stock())
        self.assertEqual(self.store.observe(self.p, Stock("DISPONIBLE")), "REPOSICION")

    def test_pending_saved(self):
        self.p.classification = "POR_VERIFICAR"
        self.store.observe(self.p, Stock())
        self.assertEqual(len(self.store.products()), 1)

    def test_slug_change(self):
        self.store.observe(self.p, Stock("DISPONIBLE"))
        self.p.url = "https://www.plazavea.com.pe/new/p"
        self.assertIsNone(self.store.observe(self.p, Stock("DISPONIBLE")))
        self.assertEqual(len(self.store.products()), 1)

    def test_legacy_identity(self):
        old = copy.deepcopy(self.p)
        old.id = "oldsku"
        self.store.observe(old, Stock("AGOTADO"))
        self.assertEqual(self.store.observe(self.p, Stock("DISPONIBLE")), "REPOSICION")
        self.assertEqual(len(self.store.products()), 1)

    def test_historical_idempotent(self):
        self.store.migrate(ROOT)
        self.store.migrate(ROOT)
        self.assertEqual(len(self.store.products()), 13)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM events WHERE status='historical'"
            ).fetchone()[0],
            3,
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM events WHERE status='pending'"
            ).fetchone()[0],
            0,
        )

    def test_transaction_rollback(self):
        self.store.db.execute(
            "CREATE TRIGGER reject BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'test'); END"
        )
        with self.assertRaises(Exception):
            self.store.observe(self.p, Stock("DISPONIBLE"))
        self.assertEqual(self.store.products(), [])


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings(token="test-secret", chat_id="123")
        self.t = Telegram(self.s)
        self.t.session = Mock()
        self.store = Store(":memory:")
        self.p = PlazaVea().normalize(raw())
        self.store.observe(self.p, Stock("DISPONIBLE", 25, "PEN"))
        self.event = json.loads(
            self.store.db.execute("SELECT payload FROM events").fetchone()[0]
        )

    def tearDown(self):
        self.store.close()

    def test_message(self):
        self.t.session.post.return_value = response(200, {"ok": True})
        self.assertTrue(self.t.send(self.event)[0])
        payload = self.t.session.post.call_args.kwargs["json"]
        self.assertIn("25.00 PEN", payload["text"])
        self.assertIn("America/Lima", payload["text"])
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"], URL)

    @patch("monitor.telegram.time.sleep")
    def test_sent_not_repeated(self, sleep):
        self.t.session.post.return_value = response(200, {"ok": True})
        deliver(self.store, self.t, Mock())
        deliver(self.store, self.t, Mock())
        self.assertEqual(self.t.session.post.call_count, 1)

    def test_rate_limit(self):
        self.t.session.post.return_value = response(
            429, {"ok": False, "parameters": {"retry_after": 120}}
        )
        deliver(self.store, self.t, Mock())
        deliver(self.store, self.t, Mock())
        self.assertEqual(self.t.session.post.call_count, 1)
        self.assertEqual(
            self.store.db.execute("SELECT attempts FROM events").fetchone()[0], 1
        )

    def test_permanent_failure(self):
        self.t.session.post.return_value = response(
            403, {"ok": False, "error_code": 403}
        )
        deliver(self.store, self.t, Mock())
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0], "failed"
        )

    def test_secret_redaction(self):
        self.t.session.post.side_effect = requests.ConnectionError("test-secret")
        self.assertNotIn("test-secret", str(self.t.send(self.event)))

    def test_missing_credentials_keeps_pending(self):
        self.s.token = ""
        deliver(self.store, self.t, Mock())
        self.assertEqual(
            self.store.db.execute("SELECT attempts FROM events").fetchone()[0], 0
        )

    def test_attempt_limit(self):
        self.s.telegram_retries = 1
        self.t.session.post.return_value = response(503, {})
        deliver(self.store, self.t, Mock())
        self.assertEqual(
            self.store.db.execute("SELECT status FROM events").fetchone()[0], "failed"
        )


class IntegrationTests(unittest.TestCase):
    def test_lock(self):
        with tempfile.TemporaryDirectory() as d:
            with exclusive(Path(d) / "lock"):
                with self.assertRaises(AlreadyRunning):
                    with exclusive(Path(d) / "lock"):
                        pass
            with exclusive(Path(d) / "lock"):
                pass

    def test_cycle_failure_keeps_monitoring(self):
        store = Store(":memory:")
        p = PlazaVea().normalize(raw())
        store.observe(p, Stock("AGOTADO"))
        adapter = Mock()
        adapter.search.side_effect = RemoteError(403)
        adapter.details.side_effect = RemoteError(403)
        t = Telegram(Settings())
        self.assertFalse(cycle(Settings(), store, adapter, t, Mock()))
        self.assertEqual(
            store.db.execute(
                "SELECT last_known FROM products WHERE key=?", (p.key,)
            ).fetchone()[0],
            "AGOTADO",
        )
        store.close()


class RegressionTests(unittest.TestCase):
    def test_legacy_slug_requires_p_suffix(self):
        a = PlazaVea(Settings(), Mock())
        a.client.get.side_effect = [[], [raw()]]
        a.details(Product("legacy", "", URL))
        self.assertTrue(a.client.get.call_args.args[0].endswith("/pokemon/p"))

    def test_verified_id_can_change_slug(self):
        a = PlazaVea(Settings(), Mock())
        p = a.normalize(raw())
        a.client.get.return_value = [
            dict(raw(), link="https://www.plazavea.com.pe/new/p")
        ]
        self.assertTrue(a.details(p).url.endswith("/new/p"))

    def test_currency_from_offer(self):
        a = PlazaVea(Settings(context_confirmed=True))
        p = a.normalize(
            raw(
                [
                    {
                        "IsAvailable": True,
                        "AvailableQuantity": 1,
                        "Price": 25,
                        "CurrencyCode": "PEN",
                    }
                ]
            )
        )
        self.assertEqual(a.stock(p).currency, "PEN")

    def test_unknown_currency_not_assumed(self):
        a = PlazaVea(Settings(context_confirmed=True))
        self.assertIsNone(a.stock(a.normalize(raw())).currency)

    def test_pending_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite3"
            store = Store(path)
            p = PlazaVea().normalize(raw())
            store.observe(p, Stock("DISPONIBLE"))
            store.close()
            store = Store(path)
            self.assertIsNone(store.observe(p, Stock("DISPONIBLE")))
            self.assertEqual(
                store.db.execute(
                    "SELECT count(*) FROM events WHERE status='pending'"
                ).fetchone()[0],
                1,
            )
            store.close()

    def test_rate_limit_shared_by_events(self):
        s = Settings(token="secret", chat_id="1")
        t = Telegram(s)
        t.session = Mock()
        t.session.post.return_value = response(
            429, {"ok": False, "parameters": {"retry_after": 120}}
        )
        store = Store(":memory:")
        p = PlazaVea().normalize(raw())
        store.observe(p, Stock("DISPONIBLE"))
        other = copy.deepcopy(p)
        other.id = "another"
        other.url = "https://www.plazavea.com.pe/another/p"
        store.observe(other, Stock("DISPONIBLE"))
        deliver(store, t, Mock())
        deliver(store, t, Mock())
        self.assertEqual(t.session.post.call_count, 1)
        store.close()

    @patch("monitor.telegram.time.sleep")
    def test_functional_cycle_sends_and_deduplicates(self, sleep):
        s = Settings(token="secret", chat_id="1", context_confirmed=True)
        t = Telegram(s)
        t.session = Mock()
        t.session.post.return_value = response(200, {"ok": True})
        store = Store(":memory:")
        adapter = Mock()
        p = PlazaVea().normalize(raw())
        from monitor.models import SearchResult

        adapter.search.return_value = SearchResult(products=[p])
        adapter.details.return_value = p
        adapter.stock.return_value = Stock("DISPONIBLE", 25, "PEN")
        self.assertTrue(cycle(s, store, adapter, t, Mock()))
        self.assertTrue(cycle(s, store, adapter, t, Mock()))
        self.assertEqual(t.session.post.call_count, 1)
        store.close()

    def test_corrupt_history_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "datos").mkdir()
            (root / "datos" / "productos.json").write_text("{invalid")
            store = Store(":memory:")
            with self.assertRaises(ValueError):
                store.migrate(root)
            self.assertEqual(store.products(), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
