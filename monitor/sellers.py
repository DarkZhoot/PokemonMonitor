"""Políticas de ventas propias confirmadas de Ripley y Oechsle."""

import json
from dataclasses import asdict
from monitor.models import Product


def schema_seller(offer):
    if not isinstance(offer, dict):
        return "unknown"
    seller = offer.get("seller")
    name = seller.get("name") if isinstance(seller, dict) else seller
    if not isinstance(name, str) or not name.strip():
        return "unknown"
    return "ripley" if name.strip().casefold() == "ripley" else "marketplace"


def ripley_seller(product):
    native = product.raw.get("native")
    if isinstance(native, dict):
        seller = native.get("seller")
        if isinstance(seller, str):
            if seller.strip().upper() == "RIPLEY":
                return "ripley"
            if seller.strip().upper() == "MARKETPLACE":
                return "marketplace"
        return "unknown"
    raw = product.raw.get("schema", {})
    offers = raw.get("offers") if isinstance(raw, dict) else None
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list) or not offers:
        return "unknown"
    states = [schema_seller(offer) for offer in offers]
    if "ripley" in states:
        return "ripley"
    return (
        "marketplace" if all(state == "marketplace" for state in states) else "unknown"
    )


def restrict_ripley(product):
    if product.retailer == "Ripley":
        seller = ripley_seller(product)
        if seller == "marketplace":
            product.classification = "DESCARTADO"
        elif seller == "unknown" and product.classification != "DESCARTADO":
            product.classification = "POR_VERIFICAR"
    return product


def enforce_ripley_scope(store, log):
    """Conserva historial; retira objetivos y cancela avisos fuera del nuevo alcance."""
    products = {
        product.key: product
        for product in (
            Product(**json.loads(row[0]))
            for row in store.db.execute(
                "SELECT data FROM products WHERE key LIKE 'ripley:%'"
            )
        )
    }
    excluded = {}
    with store.db:
        if not store.db.execute(
            "SELECT 1 FROM meta WHERE key='ripley_scope_direct_v1'"
        ).fetchone():
            # Only Ripley's old discovery covered the wrong seller scope.
            store.db.execute(
                "DELETE FROM meta WHERE key IN ('ripley:discovery_attempt','ripley:discovery_success')"
            )
            store.db.execute("INSERT INTO meta VALUES('ripley_scope_direct_v1','1')")
            log("ripley_scope_updated", scope="Vendido por:Ripley")
        for key, product in products.items():
            seller = ripley_seller(product)
            if seller == "ripley":
                continue
            excluded[key] = seller
            original = product.classification
            restrict_ripley(product)
            if product.classification != original:
                store.db.execute(
                    "UPDATE products SET data=?,detail=? WHERE key=?",
                    (
                        json.dumps(asdict(product), ensure_ascii=False),
                        "Fuera de seguimiento: vendedor Ripley no confirmado",
                        key,
                    ),
                )
        for row in store.db.execute(
            "SELECT id,product_key,payload FROM events WHERE status IN ('pending','failed')"
        ).fetchall():
            event = json.loads(row["payload"])
            key = row["product_key"]
            if event.get("tienda") != "Ripley" and not (key or "").startswith(
                "ripley:"
            ):
                continue
            if (
                key in products
                and key not in excluded
                and event.get("seller_scope") == "ripley_direct_v1"
            ):
                continue
            store.db.execute(
                "UPDATE events SET status='cancelled',error=? WHERE id=?",
                (
                    "Cancelado: solo ventas propias de Ripley; vendedor no admitido o desconocido",
                    row["id"],
                ),
            )
            log(
                "telegram_cancelled",
                event_id=row["id"],
                reason="Ripley: vendedor fuera del alcance",
            )
    return len(excluded)


def oechsle_seller(product):
    """El vendedor 1 vacío en una ficha externa NO prueba que sea venta propia."""
    declared = product.raw.get("Vendido por")
    if isinstance(declared, str):
        declared = [declared]
    if (
        not isinstance(declared, list)
        or not declared
        or any(not isinstance(x, str) or not x.strip() for x in declared)
    ):
        return "unknown"
    names = {x.strip().casefold() for x in declared}
    if names == {"oechsle"}:
        return "oechsle"
    if "oechsle" in names:
        return "unknown"
    return "marketplace"


def restrict_oechsle(product):
    if product.retailer == "Oechsle":
        seller = oechsle_seller(product)
        if seller == "marketplace":
            product.classification = "DESCARTADO"
        elif seller == "unknown" and product.classification != "DESCARTADO":
            product.classification = "POR_VERIFICAR"
    return product


def enforce_oechsle_scope(store, log):
    products = {
        p.key: p
        for p in (
            Product(**json.loads(row[0]))
            for row in store.db.execute(
                "SELECT data FROM products WHERE key LIKE 'oechsle:%'"
            )
        )
    }
    excluded = {}
    with store.db:
        if not store.db.execute(
            "SELECT 1 FROM meta WHERE key='oechsle_scope_direct_v1'"
        ).fetchone():
            store.db.execute(
                "DELETE FROM meta WHERE key IN ('oechsle:discovery_attempt','oechsle:discovery_success')"
            )
            store.db.execute("INSERT INTO meta VALUES('oechsle_scope_direct_v1','1')")
            log("oechsle_scope_updated", scope="Vendido por:Oechsle")
        for key, p in products.items():
            if oechsle_seller(p) == "oechsle":
                continue
            excluded[key] = True
            original = p.classification
            restrict_oechsle(p)
            if original != p.classification:
                store.db.execute(
                    "UPDATE products SET data=?,detail=? WHERE key=?",
                    (
                        json.dumps(asdict(p), ensure_ascii=False),
                        "Fuera de seguimiento: vendedor Oechsle no confirmado",
                        key,
                    ),
                )
        for row in store.db.execute(
            "SELECT id,product_key,payload FROM events WHERE status IN ('pending','failed')"
        ).fetchall():
            event = json.loads(row["payload"])
            key = row["product_key"]
            if event.get("tienda") != "Oechsle" and not (key or "").startswith(
                "oechsle:"
            ):
                continue
            if (
                key in products
                and key not in excluded
                and event.get("seller_scope") == "oechsle_direct_v1"
            ):
                continue
            store.db.execute(
                "UPDATE events SET status='cancelled',error=? WHERE id=?",
                (
                    "Cancelado: solo ventas propias de Oechsle; vendedor no confirmado",
                    row["id"],
                ),
            )
            log(
                "telegram_cancelled",
                event_id=row["id"],
                reason="Oechsle: vendedor fuera del alcance",
            )
    return len(excluded)


def enforce_seller_scopes(store, log):
    return enforce_ripley_scope(store, log) + enforce_oechsle_scope(store, log)


def falabella_offer_seller(offer):
    if not isinstance(offer, dict):
        return "unknown"
    seller = offer.get("seller")
    name = seller.get("name") if isinstance(seller, dict) else seller
    if not isinstance(name, str) or not name.strip():
        return "unknown"
    return (
        "falabella"
        if name.strip().casefold() in ("falabella", "saga falabella")
        else "marketplace"
    )


def restrict_falabella(product):
    if product.retailer != "Falabella":
        return product
    offers = product.raw.get("schema", {}).get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    states = (
        [falabella_offer_seller(x) for x in offers] if isinstance(offers, list) else []
    )
    if "falabella" not in states:
        if states and all(x == "marketplace" for x in states):
            product.classification = "DESCARTADO"
        elif product.classification != "DESCARTADO":
            product.classification = "POR_VERIFICAR"
    return product
