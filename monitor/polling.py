"""Optional independent Plaza Vea polling within the single locked process."""

import threading
import time


def next_poll(started, finished, interval):
    """Skip overruns rather than overlapping requests or catching up in a burst."""
    target = started + interval
    return target if target > finished else finished + interval


class PlazaPolling:
    def __init__(self, settings, log, run_cycle):
        self.settings = settings
        self.log = log
        self.run_cycle = run_cycle
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="plazavea-poll", daemon=True
        )

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def run(self):
        # Connections and HTTP sessions belong exclusively to this thread.
        from monitor.store import Store
        from monitor.telegram import Telegram
        from tiendas.plaza_vea import PlazaVea

        store = None
        try:
            store = Store(self.settings.data_dir / "monitor.sqlite3")
            adapter = PlazaVea(self.settings)
            telegram = Telegram(self.settings)
            self.log(
                "plazavea_polling_started",
                interval_seconds=self.settings.plazavea_poll_interval,
            )
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    self.run_cycle(
                        self.settings,
                        store,
                        adapter,
                        telegram,
                        self.log,
                        retailer="Plaza Vea",
                    )
                except Exception:
                    self.log(
                        "retailer_failed",
                        retailer="Plaza Vea",
                        error="Error de revisión independiente; se reintentará",
                    )
                target = next_poll(
                    started, time.monotonic(), self.settings.plazavea_poll_interval
                )
                if self.stop_event.wait(max(0, target - time.monotonic())):
                    break
        except Exception:
            self.log(
                "plazavea_polling_failed",
                error="No se pudo mantener la revisión independiente",
            )
        finally:
            if store is not None:
                store.close()
