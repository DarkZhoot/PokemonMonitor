import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from monitor.settings import Settings
from monitor.polling import PlazaPolling, RetailerPolling, next_poll
from monitor.store import Store
from monitor.telegram import deliver


class PollingTests(unittest.TestCase):
    def test_target_start_and_slow_cycle_backoff(self):
        self.assertEqual(next_poll(100, 106, 45), 145)
        self.assertEqual(next_poll(100, 160, 45), 205)

    def test_env_accepts_45_without_changing_other_clocks(self):
        # Exercise defaults independently of the operator's environment/.env.
        with patch("monitor.settings.load_dotenv"), patch.dict(
            "os.environ",
            {
                "PLAZAVEA_POLL_INTERVAL": "45",
                "MONITOR_INTERVAL": "120",
                "DISCOVERY_INTERVAL": "14400",
            },
            clear=True,
        ):
            s = Settings.load()
        self.assertEqual(s.plazavea_poll_interval, 45)
        self.assertEqual(s.interval, 120)
        self.assertEqual(s.discovery_interval, 14400)
        self.assertEqual(s.ripley_discovery_interval, 120)

    def test_invalid_interval_rejected(self):
        for value in ["-1", "1", "14", "86401"]:
            with self.subTest(value=value), patch.dict(
                "os.environ", {"PLAZAVEA_POLL_INTERVAL": value}
            ):
                with self.assertRaises(ValueError):
                    Settings.load()

    def test_independent_cycle_uses_existing_database_and_own_thread(self):
        with TemporaryDirectory() as tmp:
            s = replace(Settings(), data_dir=Path(tmp), plazavea_poll_interval=45)
            original = Store(Path(tmp) / "monitor.sqlite3")
            with original.db:
                original.db.execute("INSERT INTO meta VALUES('history','preserved')")
            seen = threading.Event()
            thread_ids = []

            def cycle(settings, store, adapter, telegram, log, **kwargs):
                self.assertEqual(
                    store.db.execute(
                        "SELECT value FROM meta WHERE key='history'"
                    ).fetchone()[0],
                    "preserved",
                )
                thread_ids.append(threading.get_ident())
                seen.set()

            with patch("tiendas.plaza_vea.PlazaVea"), patch(
                "monitor.telegram.Telegram"
            ):
                worker = PlazaPolling(s, Mock(), cycle)
                worker.start()
                self.assertTrue(seen.wait(3))
                worker.close()
            self.assertFalse(worker.thread.is_alive())
            self.assertNotEqual(thread_ids[0], threading.get_ident())
            original.close()

    def test_cycle_error_retries_without_overlapping(self):
        with TemporaryDirectory() as tmp:
            s = replace(Settings(), data_dir=Path(tmp), plazavea_poll_interval=0.02)
            seen = threading.Event()
            calls = []

            def cycle(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise ValueError("temporary")
                seen.set()

            with patch("tiendas.plaza_vea.PlazaVea"), patch(
                "monitor.telegram.Telegram"
            ):
                worker = PlazaPolling(s, Mock(), cycle)
                worker.start()
                self.assertTrue(seen.wait(3))
                worker.close()
            self.assertEqual(len(calls), 2)

    def test_delivery_serialized_across_connections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "monitor.sqlite3"
            store = Store(path)
            with store.db:
                store.db.execute(
                    "INSERT INTO events(id,payload) VALUES('test',?)",
                    (json.dumps({"id": "test", "tienda": "Plaza Vea"}),),
                )
            store.close()
            entered = threading.Event()
            release = threading.Event()
            errors = []
            telegram = Mock()
            telegram.settings.token = "test"
            telegram.settings.chat_id = "test"
            telegram.settings.telegram_retries = 3

            def send(event):
                entered.set()
                release.wait(3)
                return True, 0, "", False

            telegram.send.side_effect = send

            def run():
                db = Store(path)
                try:
                    deliver(db, telegram, Mock())
                except Exception as e:
                    errors.append(e)
                finally:
                    db.close()

            with patch("monitor.telegram.time.sleep"):
                first = threading.Thread(target=run)
                first.start()
                self.assertTrue(entered.wait(3))
                second = threading.Thread(target=run)
                second.start()
                release.set()
                first.join(3)
                second.join(3)
            self.assertEqual(errors, [])
            self.assertEqual(telegram.send.call_count, 1)
            db = Store(path)
            self.assertEqual(
                db.db.execute("SELECT status FROM events WHERE id='test'").fetchone()[
                    0
                ],
                "sent",
            )
            db.close()

    def test_loop_starts_one_worker_per_enabled_retailer(self):
        from monitor.app import main

        with TemporaryDirectory() as tmp:
            settings = replace(
                Settings(),
                data_dir=Path(tmp),
                retailers=["plazavea", "oechsle", "tailoy"],
                plazavea_poll_interval=25,
                interval=60,
            )
            with patch("sys.argv", ["main.py", "--loop"]), patch(
                "monitor.app.Settings.load", return_value=settings
            ), patch("monitor.app.configure", return_value=Mock()), patch(
                "monitor.app.RetailerPolling"
            ) as factory, patch(
                "monitor.app.run_retailers"
            ) as sequential, patch(
                "monitor.app.time.sleep", side_effect=KeyboardInterrupt
            ):
                workers = [Mock(), Mock(), Mock()]
                factory.side_effect = workers
                with self.assertRaises(KeyboardInterrupt):
                    main()
                self.assertEqual(
                    [call.args[3] for call in factory.call_args_list],
                    ["Plaza Vea", "Oechsle", "Tai Loy"],
                )
                self.assertEqual(
                    [call.args[5] for call in factory.call_args_list], [25, 60, 60]
                )
                sequential.assert_not_called()
                for worker in workers:
                    worker.start.assert_called_once()
                    worker.stop.assert_called_once()
                    worker.close.assert_called_once()

    def test_once_remains_sequential_even_with_option_enabled(self):
        from monitor.app import main

        with TemporaryDirectory() as tmp:
            settings = replace(
                Settings(),
                data_dir=Path(tmp),
                retailers=["plazavea"],
                plazavea_poll_interval=45,
            )
            with patch("sys.argv", ["main.py", "--once"]), patch(
                "monitor.app.Settings.load", return_value=settings
            ), patch("monitor.app.configure", return_value=Mock()), patch(
                "monitor.app.RetailerPolling"
            ) as worker, patch(
                "monitor.app.run_retailers", return_value=True
            ) as run:
                self.assertEqual(main(), 0)
                self.assertIn("Plaza Vea", run.call_args.args[2])
                worker.assert_not_called()

    def test_slow_retailer_does_not_delay_other_retailer(self):
        with TemporaryDirectory() as tmp:
            settings = replace(Settings(), data_dir=Path(tmp), interval=0.02)
            db = Store(Path(tmp) / "monitor.sqlite3")
            db.close()
            slow_entered, release, fast_repeated = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )
            fast_calls = []

            def slow_cycle(*args, **kwargs):
                slow_entered.set()
                release.wait(3)

            def fast_cycle(*args, **kwargs):
                fast_calls.append(1)
                if len(fast_calls) >= 3:
                    fast_repeated.set()

            slow = RetailerPolling(settings, Mock(), slow_cycle, "Tai Loy", Mock())
            fast = RetailerPolling(settings, Mock(), fast_cycle, "Oechsle", Mock())
            try:
                slow.start()
                self.assertTrue(slow_entered.wait(3))
                fast.start()
                self.assertTrue(fast_repeated.wait(3))
                self.assertFalse(release.is_set())
            finally:
                release.set()
                slow.close()
                fast.close()

    def test_failed_worker_initialization_retries_without_stopping_others(self):
        with TemporaryDirectory() as tmp:
            settings = replace(Settings(), data_dir=Path(tmp), interval=0.02)
            seen = threading.Event()
            factory = Mock(side_effect=[ValueError("init"), Mock()])
            worker = RetailerPolling(
                settings, Mock(), lambda *a, **kw: seen.set(), "Ilahui", factory
            )
            try:
                worker.start()
                self.assertTrue(seen.wait(3))
            finally:
                worker.close()
            self.assertEqual(factory.call_count, 2)

    def test_busy_telegram_does_not_block_another_catalog_worker(self):
        from monitor.telegram import serialized_delivery

        entered, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        @serialized_delivery
        def slow_send():
            entered.set()
            release.wait(3)
            return True

        @serialized_delivery
        def second_send():
            self.fail("Another sender already owns delivery")

        first = threading.Thread(target=slow_send)
        first.start()
        self.assertTrue(entered.wait(3))
        results = []

        def other():
            results.append(second_send())
            finished.set()

        second = threading.Thread(target=other)
        second.start()
        try:
            self.assertTrue(finished.wait(1))
            self.assertEqual(results, [False])
        finally:
            release.set()
            first.join(3)
            second.join(3)

    def test_parallel_observations_preserve_history_and_single_events(self):
        from monitor.models import Product, Stock

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "monitor.sqlite3"
            Store(path).close()
            errors = []

            def observe(retailer):
                store = Store(path)
                try:
                    for i in range(15):
                        p = Product(
                            str(i),
                            "Pokemon",
                            "https://example.test/product",
                            "CANDIDATO",
                            retailer,
                            raw={
                                "native": {"seller": "RIPLEY"},
                                "Vendido por": ["Oechsle"],
                                "schema": {"offers": {"seller": {"name": "Falabella"}}},
                            },
                        )
                        # Use different URLs to avoid intentional same-URL identity merging.
                        p.url += str(i)
                        store.observe(p, Stock("AGOTADO"))
                        self.assertEqual(
                            store.observe(p, Stock("DISPONIBLE")), "REPOSICION"
                        )
                        store.observe(p, Stock("DESCONOCIDO"))
                        self.assertIsNone(store.observe(p, Stock("DISPONIBLE")))
                except Exception as e:
                    errors.append(e)
                finally:
                    store.close()

            threads = [
                threading.Thread(target=observe, args=(name,))
                for name in [
                    "Plaza Vea",
                    "Oechsle",
                    "Ripley",
                    "Ilahui",
                    "Tai Loy",
                    "Falabella",
                ]
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(errors, [])
            store = Store(path)
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM events").fetchone()[0], 90
            )
            self.assertEqual(
                store.db.execute(
                    "SELECT count(*) FROM products WHERE last_known='DISPONIBLE'"
                ).fetchone()[0],
                90,
            )
            store.close()

    def test_discovery_start_anchor_avoids_skipping_every_other_minute(self):
        from monitor.app import cycle
        from monitor.models import SearchResult

        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "monitor.sqlite3")
            adapter = Mock()
            adapter.search.return_value = SearchResult()
            with patch("monitor.app.deliver", return_value=True), patch(
                "monitor.store.time.time", side_effect=[100, 120]
            ):
                cycle(
                    Settings(),
                    store,
                    adapter,
                    Mock(),
                    Mock(),
                    discover=True,
                    retailer="Oechsle",
                )
            self.assertFalse(
                store.discovery_due(60, 900, timestamp=159, retailer="Oechsle")
            )
            self.assertTrue(
                store.discovery_due(60, 900, timestamp=160, retailer="Oechsle")
            )
            store.mark_discovery(retailer="Oechsle", timestamp=160)
            self.assertFalse(
                store.discovery_due(60, 900, timestamp=220, retailer="Oechsle")
            )
            self.assertTrue(
                store.discovery_due(60, 900, timestamp=1060, retailer="Oechsle")
            )
            store.close()
