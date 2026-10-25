"""Solo el proceso principal importa resultados y modifica estado/cola de eventos."""

import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
from monitor.models import Product, Stock, normalize_url
from monitor.settings import ROOT
from monitor.sellers import ripley_seller
from monitor.ripley_worker import public_settings


class RipleyDiscoveryManager:
    def __init__(self, settings):
        self.settings = settings
        self.folder = settings.data_dir / "ripley-discovery"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.process = None
        self.started = None
        self.request_path = None
        self.output_path = None
        self.log_handle = None

    def consume(self, store, log):
        for path in sorted(self.folder.glob("*.result.json")):
            if not re.fullmatch(r"[0-9a-f]{32}\.result\.json", path.name):
                continue
            try:
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("Resultado demasiado grande")
                result = json.loads(path.read_text(encoding="utf-8"))
                if (
                    result.get("version") != 1
                    or not isinstance(result.get("products"), list)
                    or len(result["products"]) > 200
                ):
                    raise ValueError("Resultado incompatible")
                completed = result.get("completed_at")
                if not isinstance(completed, (int, float)) or not math.isfinite(
                    completed
                ):
                    raise ValueError("Fecha de resultado inválida")
                if (
                    completed > time.time() + 300
                    or time.time() - completed > self.settings.discovery_interval
                ):
                    log("ripley_background_result_expired")
                    path.unlink()
                    continue
                accepted = 0
                with store.db:
                    for row in result["products"]:
                        p = Product(**row)
                        if (
                            p.retailer != "Ripley"
                            or normalize_url(p.url, "Ripley") != p.url
                            or ripley_seller(p) != "ripley"
                        ):
                            continue
                        # No se reproduce stock almacenado por el explorador. El proceso
                        # principal lo reconsulta antes de decidir cualquier notificación.
                        old = store.db.execute(
                            "SELECT data FROM products WHERE key=?", (p.key,)
                        ).fetchone()
                        if old:
                            current = Product(**json.loads(old[0]))
                            if current.classification == "CANDIDATO":
                                continue
                            store.db.execute(
                                "UPDATE products SET data=? WHERE key=?",
                                (json.dumps(row, ensure_ascii=False), p.key),
                            )
                        else:
                            store.observe(
                                p,
                                Stock(
                                    detail="Descubierto automáticamente; pendiente consulta actual"
                                ),
                            )
                        accepted += 1
                    if (
                        result.get("complete") is True
                        or result.get("retryable") is False
                    ):
                        store.mark_discovery(successful=True, retailer="Ripley")
                    delay = result.get("retry_after", 0)
                    if isinstance(delay, (int, float)) and delay > 0:
                        store.db.execute(
                            "INSERT OR REPLACE INTO meta VALUES('ripley:background_blocked_until',?)",
                            (str(time.time() + min(delay, 86400)),),
                        )
                log(
                    "ripley_background_imported",
                    accepted=accepted,
                    complete=result.get("complete") is True,
                    stats=result.get("stats", {}),
                    errors=result.get("errors", []),
                )
                path.unlink()
                request_path = path.with_name(
                    path.name.replace(".result.json", ".request.json")
                )
                request_path.unlink(missing_ok=True)
            except (ValueError, TypeError, KeyError, AttributeError):
                log("ripley_background_result_invalid")
                path.rename(path.with_suffix(".invalid"))

    def blocked(self, store):
        row = store.db.execute(
            "SELECT value FROM meta WHERE key='ripley:background_blocked_until'"
        ).fetchone()
        return bool(row and float(row[0]) > time.time())

    def tick(self, store, log):
        with store.db:
            if not store.db.execute(
                "SELECT 1 FROM meta WHERE key='ripley_multisource_v1'"
            ).fetchone():
                store.db.execute(
                    "DELETE FROM meta WHERE key IN ('ripley:discovery_attempt','ripley:discovery_success')"
                )
                store.db.execute("INSERT INTO meta VALUES('ripley_multisource_v1','1')")
        self.consume(store, log)
        if self.process is not None:
            if self.process.poll() is None:
                if (
                    time.monotonic() - self.started
                    <= self.settings.ripley_discovery_seconds + 10
                ):
                    return
                self.process.terminate()
                log("ripley_background_timeout")
                return
            self.process.wait()
            self.process = None
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            self.consume(store, log)
            if (
                self.request_path
                and self.request_path.exists()
                and not self.output_path.exists()
            ):
                self.request_path.unlink()
        if self.blocked(store) or not store.discovery_due(
            self.settings.discovery_interval,
            self.settings.discovery_retry_interval,
            retailer="Ripley",
        ):
            return
        identifier = uuid4().hex
        self.request_path = self.folder / (identifier + ".request.json")
        self.output_path = self.folder / (identifier + ".result.json")
        selected = self.settings.for_retailer("ripley")
        known = [
            p.url
            for p in store.products()
            if p.retailer == "Ripley" and p.classification == "CANDIDATO"
        ]
        request = {
            "settings": public_settings(selected),
            "seconds": self.settings.ripley_discovery_seconds,
            "requests": self.settings.ripley_discovery_requests,
            "products": self.settings.ripley_discovery_products,
            "known_urls": known,
        }
        self.request_path.write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        store.mark_discovery(retailer="Ripley")
        self.log_handle = open(self.folder / "worker.log", "a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "monitor.ripley_worker",
                    str(self.request_path),
                    str(self.output_path),
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.started = time.monotonic()
            log(
                "ripley_background_started",
                max_seconds=self.settings.ripley_discovery_seconds,
            )
        except OSError:
            self.log_handle.close()
            self.log_handle = None
            log("ripley_background_start_failed")

    def close(self, *, detach=False):
        if self.process is not None and self.process.poll() is None and not detach:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
