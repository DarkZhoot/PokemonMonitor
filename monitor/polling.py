"""Independent retailer clocks inside the single locked monitor process."""

import threading
import time


def next_poll(started, finished, interval):
    """Skip overruns rather than overlapping requests or catching up in a burst."""
    target = started + interval
    return target if target > finished else finished + interval


class RetailerPolling:
    def __init__(self, settings, log, run_cycle, retailer, factory, interval=None):
        self.settings = settings
        self.log = log
        self.run_cycle = run_cycle
        self.retailer = retailer
        self.factory = factory
        self.interval = settings.interval if interval is None else interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name=f"poll-{retailer}", daemon=True
        )

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def close(self):
        self.stop()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)

    def run(self):
        from monitor.store import Store
        from monitor.telegram import Telegram

        self.log(
            "retailer_polling_started",
            retailer=self.retailer,
            interval_seconds=self.interval,
        )
        if self.retailer == "Plaza Vea":
            self.log("plazavea_polling_started", interval_seconds=self.interval)
        while not self.stop_event.is_set():
            store = None
            try:
                # Connections and HTTP sessions belong exclusively to this worker.
                store = Store(self.settings.data_dir / "monitor.sqlite3")
                adapter = self.factory(self.settings)
                telegram = Telegram(self.settings)
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    self.log("retailer_cycle_started", retailer=self.retailer)
                    try:
                        self.run_cycle(
                            self.settings,
                            store,
                            adapter,
                            telegram,
                            self.log,
                            retailer=self.retailer,
                        )
                    except Exception:
                        self.log(
                            "retailer_failed",
                            retailer=self.retailer,
                            error="Error de revisión independiente; se reintentará",
                        )
                    target = next_poll(started, time.monotonic(), self.interval)
                    if self.stop_event.wait(max(0, target - time.monotonic())):
                        break
            except Exception:
                self.log(
                    "retailer_polling_failed",
                    retailer=self.retailer,
                    error="No se pudo iniciar el trabajador; se reintentará",
                )
            finally:
                if store is not None:
                    store.close()
            if not self.stop_event.is_set():
                self.stop_event.wait(self.interval)


class PlazaPolling(RetailerPolling):
    """Compatibility wrapper for callers of the original independent worker."""

    def __init__(self, settings, log, run_cycle):
        from tiendas.plaza_vea import PlazaVea

        super().__init__(
            settings,
            log,
            run_cycle,
            "Plaza Vea",
            PlazaVea,
            settings.plazavea_poll_interval or settings.interval,
        )
