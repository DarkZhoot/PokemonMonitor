import argparse
from dataclasses import asdict
import json
import logging
from logging.handlers import RotatingFileHandler
import time
from config import PRODUCTOS
from monitor.http import RemoteError
from monitor.discovery import (
    should_watch,
    inherit_tracking,
    tracking_stub,
    pending_detail,
)
from monitor.polling import RetailerPolling
from monitor.locking import exclusive, AlreadyRunning
from monitor.models import Product, Stock, normalize_url, RETAILERS
from monitor.settings import Settings, ROOT
from monitor.sellers import enforce_seller_scopes, ripley_seller, oechsle_seller
from monitor.store import Store, now
from monitor.telegram import Telegram, deliver
from tiendas.plaza_vea import PlazaVea
from tiendas.oechsle import Oechsle
from tiendas.ripley import Ripley
from tiendas.ilahui import Ilahui
from tiendas.tailoy import Tailoy
from tiendas.falabella import Falabella


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


def enrich_listing_metadata(result, adapter, store, retired, log, retailer):
    """Read only a few direct PDPs when a native card omits identity/language.

    No sitemap/category crawl. Persist attempts to avoid repeating enrichment
    every minute when the storefront never supplies more metadata.
    """
    budget = 3
    for index, product in enumerate(result.products):
        decision = product.raw.get("_classification", {})
        if (
            product.key in retired
            or product.classification != "POR_VERIFICAR"
            or not isinstance(product.raw.get("native"), dict)
            or not decision.get("review_needed")
            or "30 aniversario" in decision.get("missing", [])
        ):
            continue
        if budget == 0:
            break
        key = "metadata_attempt:" + product.key
        previous = store.db.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        stamp = time.time()
        if previous and stamp - float(previous[0]) < 14400:
            continue
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(stamp))
            )
        budget -= 1
        try:
            detailed = adapter.details(product)
            if detailed.key != product.key:
                raise ValueError("Identidad distinta")
            result.products[index] = detailed
        except RemoteError as error:
            pending_detail(product)
            log(
                "metadata_enrichment_failed",
                retailer=retailer,
                product_key=product.key,
                http_status=error.status,
            )
            if error.status in (401, 403, 429):
                break
        except (ValueError, TypeError, AttributeError):
            pending_detail(product)
            log(
                "metadata_enrichment_failed",
                retailer=retailer,
                product_key=product.key,
                error="Ficha sin metadata verificable",
            )


def cycle(
    settings,
    store,
    adapter,
    telegram,
    log,
    *,
    discover=False,
    retailer="Plaza Vea",
    discovery_timestamp=None,
):
    started = time.monotonic()
    healthy = True
    deliver(store, telegram, log)
    result = None
    discovery_stamp = None
    if discover:
        discovery_stamp = store.mark_discovery(
            retailer=retailer,
            **(
                {"timestamp": discovery_timestamp}
                if discovery_timestamp is not None
                else {}
            ),
        )
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
    retired = store.retired_keys()
    retired_urls = store.retired_urls()
    if result:
        enrich_listing_metadata(result, adapter, store, retired, log, retailer)
    saved = [
        p
        for p in store.products()
        if p.retailer == retailer
        and p.key not in retired
        and (p.retailer, p.url) not in retired_urls
    ]
    # Poll confirmed targets and retained near-target/explicit URLs; verify before alerting.
    products = {p.key: p for p in saved if should_watch(p)}
    saved_by_url = {p.url: p for p in saved}
    saved_by_key = {p.key: p for p in saved}
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
        if p.key in retired or (p.retailer, p.url) in retired_urls:
            continue
        if retailer == "Ripley" and ripley_seller(p) == "marketplace":
            continue
        if retailer == "Oechsle" and oechsle_seller(p) == "marketplace":
            continue
        products[p.key] = p
    fresh = set()
    if result:
        for p in result.products:
            if p.key in retired or (p.retailer, p.url) in retired_urls:
                continue
            previous = saved_by_key.get(p.key) or saved_by_url.get(p.url)
            if previous:
                if p.raw.get("_detail_error") and p.id.startswith("url:"):
                    p.id = previous.id
                inherit_tracking(previous, p)
            changed = store.note_classification(p)
            decision = p.raw.get("_classification", {})
            if (
                changed
                and p.classification == "POR_VERIFICAR"
                and (
                    decision.get("review_needed")
                    or decision.get("classification") == "CANDIDATO"
                )
            ):
                log(
                    "product_review_needed",
                    retailer=retailer,
                    product_key=p.key,
                    name=p.name,
                    url=p.url,
                    missing=decision.get("missing")
                    or ["vendedor u otras validaciones del retailer"],
                )
            if p.classification != "CANDIDATO":
                # Discovery metadata is sufficient to retain a pending entry, without
                # paying for another detail request or changing any known stock.
                store.observe(
                    p,
                    Stock(
                        detail=(
                            "Ficha no verificable; URL conservada"
                            if p.raw.get("_detail_error")
                            else "Idioma o edición pendiente/excluida"
                        )
                    ),
                )
                if (
                    should_watch(p)
                    and p.raw.get("_metadata_only")
                    and not p.raw.get("_detail_error")
                ):
                    p.raw["_needs_details"] = True
                    products[p.key] = p
                else:
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
            if p.key in retired or (p.retailer, p.url) in retired_urls or p.key in seen:
                continue
            seen.add(p.key)
            inherit_tracking(product, p)
            if p.classification == "CANDIDATO":
                stock = adapter.stock(p)
            elif p.classification == "POR_VERIFICAR":
                missing = p.raw.get("_classification", {}).get("missing", []) or [
                    "vendedor confirmado"
                ]
                stock = Stock(detail="Pendiente: " + ", ".join(missing))
            else:
                stock = Stock(detail="Identidad, idioma o edición pendiente/excluida")
        except (RemoteError, ValueError, TypeError, AttributeError) as error:
            p = product
            detail = "Error de consulta; no se infiere agotado"
            if isinstance(error, RemoteError):
                detail = (
                    f"Error HTTP {error.status}; URL conservada"
                    if error.status
                    else "Error de conexión; URL conservada"
                )
            elif product.raw.get("_watch") or product.raw.get("_explicit_watch"):
                detail = (
                    "Ficha no interpretable o identidad no verificable; URL conservada"
                )
            stock = Stock(detail=detail)
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
            if retailer == "Plaza Vea":
                store.mark_discovery(successful=True, retailer=retailer)
            else:
                store.mark_discovery(
                    successful=True, retailer=retailer, timestamp=discovery_stamp
                )
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
    cycle_timestamp=None,
):
    interval = {
        "Ripley": settings.ripley_discovery_interval,
        "Oechsle": settings.oechsle_discovery_interval,
        "Ilahui": settings.ilahui_discovery_interval,
        "Tai Loy": settings.tailoy_discovery_interval,
        "Falabella": settings.falabella_discovery_interval,
    }.get(retailer, settings.discovery_interval)
    due = force_discovery or store.discovery_due(
        interval,
        settings.discovery_retry_interval,
        retailer=retailer,
        **({"timestamp": cycle_timestamp} if cycle_timestamp is not None else {}),
    )
    return cycle(
        settings,
        store,
        adapter,
        telegram,
        log,
        discover=due,
        retailer=retailer,
        **(
            {"discovery_timestamp": cycle_timestamp}
            if cycle_timestamp is not None
            else {}
        ),
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
    group.add_argument(
        "--retire-product",
        action="append",
        metavar="TIENDA:ID",
        help="Retirar un producto del seguimiento conservando su historial; repetible",
    )
    group.add_argument(
        "--restore-product",
        action="append",
        metavar="TIENDA:ID",
        help="Reactivar un producto retirado; repetible",
    )
    group.add_argument(
        "--track-url",
        metavar="URL",
        help="Guardar una ficha del retailer seleccionado para revisión directa",
    )
    parser.add_argument(
        "--retailer",
        choices=["plazavea", "oechsle", "ripley", "ilahui", "tailoy", "falabella"],
        help="Limitar esta ejecución a una tienda",
    )
    parser.add_argument(
        "--include-pending",
        action="store_true",
        help="Con --list-products, incluir POR_VERIFICAR y motivos pendientes",
    )
    args = parser.parse_args()
    if args.track_url and not args.retailer:
        parser.error("--track-url requiere --retailer")
    if args.include_pending and not args.list_products:
        parser.error("--include-pending requiere --list-products")
    settings = Settings.load()
    log = configure(settings)
    try:
        with exclusive(settings.data_dir / "monitor.lock"):
            store = Store(settings.data_dir / "monitor.sqlite3")
            workers = []
            try:
                store.migrate(ROOT)
                enforce_seller_scopes(store, log)
                if args.track_url:
                    try:
                        name = next(
                            name
                            for name, values in RETAILERS.items()
                            if values[0] == args.retailer
                        )
                        stub = tracking_stub(args.track_url, name, explicit=True)
                    except (ValueError, TypeError):
                        log(
                            "product_tracking_failed",
                            error="URL inválida para el retailer seleccionado",
                        )
                        return 1
                    if stub.key in store.retired_keys():
                        log(
                            "product_tracking_failed",
                            error="El producto está retirado; restaurarlo explícitamente antes de seguirlo",
                        )
                        return 1
                    existing = next(
                        (
                            p
                            for p in store.products()
                            if p.key == stub.key or p.url == stub.url
                        ),
                        None,
                    )
                    product = existing or stub
                    if (
                        product.key in store.retired_keys()
                        or (product.retailer, product.url) in store.retired_urls()
                    ):
                        log(
                            "product_tracking_failed",
                            error="Producto retirado; requiere restauración explícita",
                        )
                        return 1
                    product.raw["_watch"] = True
                    product.raw["_explicit_watch"] = True
                    if existing:
                        row = store.db.execute(
                            "SELECT state,last_known FROM products WHERE key=?",
                            (product.key,),
                        ).fetchone()
                        counts = dict(
                            store.db.execute(
                                "SELECT status,count(*) FROM events WHERE product_key=? GROUP BY status",
                                (product.key,),
                            )
                        )
                        log(
                            "product_tracking_existing",
                            product_key=product.key,
                            classification=product.classification,
                            state=row["state"],
                            last_known=row["last_known"],
                            alerts=counts,
                        )
                        with store.db:
                            store.db.execute(
                                "UPDATE products SET data=? WHERE key=?",
                                (
                                    json.dumps(asdict(product), ensure_ascii=False),
                                    product.key,
                                ),
                            )
                    else:
                        store.observe(
                            product,
                            Stock(
                                detail="URL guardada; pendiente verificar identidad, vendedor y stock"
                            ),
                        )
                    log("product_url_saved", product_key=product.key, url=product.url)
                    return 0
                if args.retire_product or args.restore_product:
                    keys = args.retire_product or args.restore_product
                    known = {p.key for p in store.products()}
                    if any(key not in known for key in keys):
                        log(
                            "product_scope_failed",
                            error="Algún identificador no está registrado; no se modificó el seguimiento",
                        )
                        return 1
                    for key in keys:
                        if args.retire_product:
                            store.retire_product(key)
                        else:
                            store.restore_product(key)
                        log(
                            (
                                "product_retired"
                                if args.retire_product
                                else "product_restored"
                            ),
                            product_key=key,
                        )
                    return 0
                if args.list_products:
                    retired = store.retired_keys()
                    print(
                        json.dumps(
                            [
                                {
                                    "id": p.id,
                                    "nombre": p.name,
                                    "url": p.url,
                                    "tienda": p.retailer,
                                    **(
                                        {
                                            "clasificacion": p.classification,
                                            "pendiente": p.raw.get(
                                                "_classification", {}
                                            ).get("missing", [])
                                            or (
                                                ["vendedor u otros datos sin confirmar"]
                                                if p.classification == "POR_VERIFICAR"
                                                else []
                                            ),
                                        }
                                        if args.include_pending
                                        else {}
                                    ),
                                }
                                for p in store.products()
                                if (
                                    p.classification == "CANDIDATO"
                                    or (
                                        args.include_pending
                                        and p.classification == "POR_VERIFICAR"
                                    )
                                )
                                and p.key not in retired
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
                factories = [
                    ("plazavea", "Plaza Vea", PlazaVea),
                    ("oechsle", "Oechsle", Oechsle),
                    ("ripley", "Ripley", Ripley),
                    ("ilahui", "Ilahui", Ilahui),
                    ("tailoy", "Tai Loy", Tailoy),
                    ("falabella", "Falabella", Falabella),
                ]
                adapters = {}
                for slug, name, factory in factories:
                    if slug not in enabled:
                        continue
                    selected = settings.for_retailer(slug)
                    if args.loop:
                        interval = (
                            (settings.plazavea_poll_interval or settings.interval)
                            if slug == "plazavea"
                            else settings.interval
                        )
                        workers.append(
                            RetailerPolling(
                                selected, log, scheduled_cycle, name, factory, interval
                            )
                        )
                    else:
                        adapters[name] = (selected, factory(selected))
                if not args.loop:
                    try:
                        ok = run_retailers(
                            settings,
                            store,
                            adapters,
                            telegram,
                            log,
                            force_discovery=args.discover,
                        )
                        return 0 if ok else 1
                    except Exception:
                        log(
                            "cycle_failed",
                            error="Error interno; revisar estado y configuración",
                        )
                        return 1
                for worker in workers:
                    worker.start()
                # No retailer HTTP work runs here: only supervision and queued delivery.
                while True:
                    if any(not worker.thread.is_alive() for worker in workers):
                        log(
                            "polling_supervisor_failed",
                            error="Un trabajador se detuvo; reiniciar servicio",
                        )
                        return 1
                    time.sleep(5)
                    try:
                        deliver(store, telegram, log)
                    except Exception:
                        log("delivery_failure", error="No se pudo procesar la cola")
            finally:
                for worker in workers:
                    worker.stop()
                for worker in workers:
                    worker.close()
                store.close()
    except AlreadyRunning:
        log("already_running")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
