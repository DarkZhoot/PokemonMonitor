import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import time
from config import PRODUCTOS
from monitor.http import RemoteError
from monitor.locking import exclusive, AlreadyRunning
from monitor.models import Product, Stock, normalize_url
from monitor.settings import Settings, ROOT
from monitor.sellers import enforce_ripley_scope, ripley_seller
from monitor.store import Store, now
from monitor.telegram import Telegram, deliver
from tiendas.plaza_vea import PlazaVea
from tiendas.oechsle import Oechsle
from tiendas.ripley import Ripley


def configure(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pokemonmonitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        for handler in (
            logging.StreamHandler(),
            RotatingFileHandler(
                settings.data_dir / "monitor.jsonl",
                maxBytes=5_000_000,
                backupCount=3,
                encoding="utf-8",
            ),
        ):
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)

    def log(event, **fields):
        logger.info(
            json.dumps(dict(fecha=now(), event=event, **fields), ensure_ascii=False)
        )

    return log


def cycle(
    settings, store, adapter, telegram, log, *, discover=False, retailer="Plaza Vea"
):
    started = time.monotonic()
    healthy = True
    deliver(store, telegram, log)
    result = None
    if discover:
        store.mark_discovery(retailer=retailer)
        log("discovery_started", retailer=retailer)
        try:
            result = adapter.search()
        except Exception:
            log(
                "search_failure",
                retailer=retailer,
                error="Error inesperado del adaptador",
            )
            healthy = False
        if result and not result.complete:
            healthy = False
            log("search_incomplete", retailer=retailer, errors=result.errors)
    saved = [p for p in store.products() if p.retailer == retailer]
    # Normal polling visits confirmed targets only; uncertain catalog entries stay stored.
    products = {p.key: p for p in saved if p.classification == "CANDIDATO"}
    saved_by_url = {p.url: p for p in saved}
    for raw in PRODUCTOS + settings.products:
        if raw.get("tienda", "Plaza Vea") != retailer:
            continue
        url = normalize_url(raw["url"], retailer)
        p = saved_by_url.get(url)
        if p is None:
            p = Product(
                str(raw.get("id") or "url:" + url),
                raw.get("nombre", ""),
                url,
                retailer=retailer,
            )
        if retailer == "Ripley" and ripley_seller(p) == "marketplace":
            continue
        products[p.key] = p
    fresh = set()
    if result:
        for p in result.products:
            if p.classification != "CANDIDATO":
                # Discovery metadata is sufficient to retain a pending entry, without
                # paying for another detail request or changing any known stock.
                store.observe(p, Stock(detail="Idioma o edición pendiente/excluida"))
                products.pop(p.key, None)
                continue
            products[p.key] = p
            fresh.add(p.key)
    if not products:
        log(
            "no_targets",
            retailer=retailer,
            instruction="Sin candidatos confirmados; se conserva el descubrimiento periódico",
        )
        if not (
            (result is not None and result.complete)
            or store.has_discovery_success(retailer)
        ):
            healthy = False
    seen = set()
    for product in products.values():
        product_started = time.monotonic()
        try:
            p = (
                product
                if product.key in fresh and not product.raw.get("_needs_details")
                else adapter.details(product)
            )
            if p.key in seen:
                continue
            seen.add(p.key)
            stock = (
                adapter.stock(p)
                if p.classification == "CANDIDATO"
                else Stock(detail="Identidad, idioma o edición pendiente/excluida")
            )
        except (RemoteError, ValueError, TypeError, AttributeError):
            p = product
            stock = Stock(detail="Error de consulta; no se infiere agotado")
            healthy = False
        except Exception:
            p = product
            stock = Stock(detail="Error inesperado del adaptador")
            healthy = False
        event = store.observe(p, stock)
        if event:
            deliver(store, telegram, log)
        if stock.state == "DESCONOCIDO":
            healthy = False
        log(
            "product_checked",
            retailer=retailer,
            product_key=p.key,
            classification=p.classification,
            state=stock.state,
            detail=stock.detail,
            event_type=event,
            url=p.url,
            duration_seconds=round(time.monotonic() - product_started, 3),
        )
    if discover:
        complete = result is not None and result.complete
        if complete:
            store.mark_discovery(successful=True, retailer=retailer)
        log(
            "discovery_finished",
            retailer=retailer,
            complete=complete,
            found_products=len(result.products) if result is not None else 0,
        )
    delivered = deliver(store, telegram, log)
    failed = store.db.execute(
        "SELECT count(*) FROM events WHERE status='failed'"
    ).fetchone()[0]
    pending = store.db.execute(
        "SELECT count(*) FROM events WHERE status='pending'"
    ).fetchone()[0]
    log(
        "cycle_finished",
        retailer=retailer,
        healthy=healthy,
        delivery_ok=delivered,
        failed_events=failed,
        pending_events=pending,
        mode="discover" if discover else "monitor",
        checked_products=len(seen),
        duration_seconds=round(time.monotonic() - started, 3),
    )
    return healthy and delivered and not failed and not pending


def scheduled_cycle(
    settings,
    store,
    adapter,
    telegram,
    log,
    *,
    force_discovery=False,
    retailer="Plaza Vea",
):
    due = force_discovery or store.discovery_due(
        settings.discovery_interval,
        settings.discovery_retry_interval,
        retailer=retailer,
    )
    return cycle(
        settings, store, adapter, telegram, log, discover=due, retailer=retailer
    )


def run_retailers(settings, store, adapters, telegram, log, *, force_discovery=False):
    outcomes = []
    for retailer, (retailer_settings, adapter) in adapters.items():
        try:
            outcomes.append(
                scheduled_cycle(
                    retailer_settings,
                    store,
                    adapter,
                    telegram,
                    log,
                    force_discovery=force_discovery,
                    retailer=retailer,
                )
            )
        except Exception:
            log(
                "retailer_failed",
                retailer=retailer,
                error="Error interno; otras tiendas continuarán",
            )
            outcomes.append(False)
    return bool(outcomes) and all(outcomes)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor Pokémon TCG; solo lectura y notificaciones"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--once", action="store_true", help="Una revisión (predeterminado)"
    )
    group.add_argument("--loop", action="store_true", help="Revisar periódicamente")
    group.add_argument(
        "--telegram-test", action="store_true", help="Enviar un mensaje de prueba real"
    )
    group.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reactivar eventos fallidos para próximo ciclo",
    )
    group.add_argument(
        "--discover",
        action="store_true",
        help="Buscar nuevas publicaciones una vez y guardar candidatos/enlaces",
    )
    group.add_argument(
        "--list-products",
        action="store_true",
        help="Mostrar candidatos guardados y sus enlaces, sin consultar la tienda",
    )
    parser.add_argument(
        "--retailer",
        choices=["plazavea", "oechsle", "ripley"],
        help="Limitar esta ejecución a una tienda",
    )
    args = parser.parse_args()
    settings = Settings.load()
    log = configure(settings)
    try:
        with exclusive(settings.data_dir / "monitor.lock"):
            store = Store(settings.data_dir / "monitor.sqlite3")
            try:
                store.migrate(ROOT)
                enforce_ripley_scope(store, log)
                if args.list_products:
                    print(
                        json.dumps(
                            [
                                {
                                    "id": p.id,
                                    "nombre": p.name,
                                    "url": p.url,
                                    "tienda": p.retailer,
                                }
                                for p in store.products()
                                if p.classification == "CANDIDATO"
                                and (
                                    not args.retailer
                                    or p.key.startswith(args.retailer + ":")
                                )
                            ],
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 0
                telegram = Telegram(settings)
                if args.telegram_test:
                    ok, _, error, _ = telegram.send(
                        dict(
                            id="prueba",
                            tipo="PRUEBA — SIN COMPRA",
                            nombre="Prueba PokemonMonitor",
                            tienda="Plaza Vea",
                            estado="DESCONOCIDO",
                            url=PRODUCTOS[0]["url"],
                            fecha=now(),
                        )
                    )
                    log("telegram_test", ok=ok, error=error)
                    return 0 if ok else 1
                if args.retry_failed:
                    with store.db:
                        store.db.execute(
                            "UPDATE events SET status='pending',attempts=0,next_try=0 WHERE status='failed'"
                        )
                    log("failed_events_reactivated")
                    return 0
                enabled = [args.retailer] if args.retailer else settings.retailers
                adapters = {}
                for slug, name, factory in [
                    ("plazavea", "Plaza Vea", PlazaVea),
                    ("oechsle", "Oechsle", Oechsle),
                    ("ripley", "Ripley", Ripley),
                ]:
                    if slug in enabled:
                        selected = settings.for_retailer(slug)
                        adapters[name] = (selected, factory(selected))
                while True:
                    try:
                        ok = run_retailers(
                            settings,
                            store,
                            adapters,
                            telegram,
                            log,
                            force_discovery=args.discover,
                        )
                    except Exception:
                        log(
                            "cycle_failed",
                            error="Error interno; revisar estado y configuración",
                        )
                        ok = False
                        if not args.loop:
                            return 1
                    if not args.loop:
                        return 0 if ok else 1
                    # Retry queued Telegram deliveries while respecting the retailer interval.
                    end = time.monotonic() + settings.interval
                    while time.monotonic() < end:
                        time.sleep(min(5, max(0, end - time.monotonic())))
                        try:
                            deliver(store, telegram, log)
                        except Exception:
                            log("delivery_failure", error="No se pudo procesar la cola")
            finally:
                store.close()
    except AlreadyRunning:
        log("already_running")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
