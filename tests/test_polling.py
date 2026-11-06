import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from monitor.settings import Settings
from monitor.polling import PlazaPolling, next_poll
from monitor.store import Store
from monitor.telegram import deliver


class PollingTests(unittest.TestCase):
    def test_target_start_and_slow_cycle_backoff(self):
        self.assertEqual(next_poll(100, 106, 45), 145)
        self.assertEqual(next_poll(100, 160, 45), 205)

    def test_env_accepts_45_without_changing_other_clocks(self):
        with patch.dict(
            "os.environ",
            {
                "PLAZAVEA_POLL_INTERVAL": "45",
                "MONITOR_INTERVAL": "120",
                "DISCOVERY_INTERVAL": "14400",
            },
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

    def test_loop_removes_plaza_from_sequential_batch(self):
        from monitor.app import main

        with TemporaryDirectory() as tmp:
            settings = replace(
                Settings(),
                data_dir=Path(tmp),
                retailers=["plazavea", "oechsle"],
                plazavea_poll_interval=45,
            )
            with patch("sys.argv", ["main.py", "--loop"]), patch(
                "monitor.app.Settings.load", return_value=settings
            ), patch("monitor.app.configure", return_value=Mock()), patch(
                "monitor.app.PlazaPolling"
            ) as worker, patch(
                "monitor.app.run_retailers", side_effect=KeyboardInterrupt
            ) as run:
                with self.assertRaises(KeyboardInterrupt):
                    main()
                self.assertNotIn("Plaza Vea", run.call_args.args[2])
                self.assertIn("Oechsle", run.call_args.args[2])
                worker.return_value.start.assert_called_once()
                worker.return_value.close.assert_called_once()

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
                "monitor.app.PlazaPolling"
            ) as worker, patch(
                "monitor.app.run_retailers", return_value=True
            ) as run:
                self.assertEqual(main(), 0)
                self.assertIn("Plaza Vea", run.call_args.args[2])
                worker.assert_not_called()
