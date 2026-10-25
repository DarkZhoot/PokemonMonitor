"""Proceso aislado de red: no abre SQLite ni envía Telegram."""

import argparse
import json
import os
import time
import threading
from dataclasses import asdict
from pathlib import Path
from monitor.locking import exclusive, AlreadyRunning
from monitor.settings import Settings
from tiendas.ripley import Ripley
from tiendas.ripley_discovery import (
    DiscoveryClient,
    expanded_discovery,
    broadened_settings,
)

# Lista explícita: nunca se serializan tokens, chat IDs ni configuración de otras tiendas.
PUBLIC_SETTINGS = (
    "terms",
    "exclusions",
    "timeout",
    "request_delay",
    "retries",
    "pages",
    "context_confirmed",
    "postal_code",
    "sales_channel",
    "seller_id",
    "sku_id",
)


def public_settings(settings):
    return {key: getattr(settings, key) for key in PUBLIC_SETTINGS}


def execute(request):
    settings = broadened_settings(Settings(**request["settings"]))
    # La exploración adicional es deliberadamente menos frecuente y más pausada.
    settings.request_delay = max(2.0, settings.request_delay)
    client = DiscoveryClient(settings, request["seconds"], request["requests"])
    adapter = Ripley(settings, client)
    result = expanded_discovery(
        adapter,
        known_urls=request.get("known_urls", ()),
        max_products=request["products"],
    )
    # Conserva primero objetivos; limita el tamaño del intercambio entre procesos.
    ordered = sorted(result.products, key=lambda p: p.classification != "CANDIDATO")
    if len(ordered) > 200:
        result.complete = False
        result.errors.append("Límite de productos para importar")
    return {
        "version": 1,
        "completed_at": time.time(),
        "complete": result.complete,
        "retryable": getattr(result, "retryable", not result.complete),
        "products": [asdict(p) for p in ordered[:200]],
        "errors": result.errors,
        "stats": getattr(result, "stats", {}),
        "retry_after": client.retry_after,
        "blocked": client.blocked,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        with exclusive(args.output.parent / "worker.lock"):
            request = json.loads(args.request.read_text(encoding="utf-8"))
            watchdog = threading.Timer(request["seconds"] + 10, lambda: os._exit(124))
            watchdog.daemon = True
            watchdog.start()
            try:
                result = execute(request)
            except Exception:
                result = {
                    "version": 1,
                    "completed_at": time.time(),
                    "complete": False,
                    "products": [],
                    "errors": ["Error interno en exploración aislada"],
                    "stats": {},
                    "retry_after": 0,
                    "blocked": None,
                }
            temporary = args.output.with_suffix(".tmp")
            content = json.dumps(result, ensure_ascii=False)
            if len(content.encode("utf-8")) > 8 * 1024 * 1024:
                result.update(
                    complete=False, products=[], errors=["Resultado excede 8 MiB"]
                )
                content = json.dumps(result)
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, args.output)
            watchdog.cancel()
            print(
                json.dumps(
                    {
                        "event": "ripley_worker_finished",
                        "complete": result["complete"],
                        "products": len(result["products"]),
                        "stats": result["stats"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 0
    except AlreadyRunning:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
