"""Shared discovery, URL retention and direct-follow-up policy for every retailer."""

import re
from urllib.parse import urlsplit
from monitor.models import Product, normalize_url
from monitor.classification import assess, annotated_raw, normalizar, canonical_metadata

EXTRA_SEARCH_TERMS = ("tcg 30", "tcg 30th")


def should_watch(product):
    return product.classification != "DESCARTADO" and (
        product.classification == "CANDIDATO"
        or product.raw.get("_watch") is True
        or product.raw.get("_falabella_watch") is True
        or product.raw.get("_explicit_watch") is True
    )


def inherit_tracking(previous, current):
    if current.classification != "DESCARTADO" and should_watch(previous):
        current.raw["_watch"] = True
        if previous.raw.get("_explicit_watch"):
            current.raw["_explicit_watch"] = True
    return current


def tracking_stub(url, retailer, name="", metadata=None, explicit=False, failed=False):
    url = normalize_url(url, retailer)
    identifier = "url:" + url
    if retailer == "Falabella":
        identifier = urlsplit(url).path.split("/")[3]
    elif retailer == "Ripley":
        identifier = re.search(r"([0-9]{6,}p|pmp[a-z0-9]+)$", urlsplit(url).path, re.I)[
            1
        ].upper()
    decision = assess(metadata or {"productName": name}, [])
    # A URL receipt is not a verified identity or offer, even if its title looks right.
    decision = dict(
        decision,
        classification="POR_VERIFICAR",
        review_needed=decision.get("review_needed", False),
        missing=list(
            dict.fromkeys(decision["missing"] + ["detalles verificables de ficha"])
        ),
    )
    raw = annotated_raw({}, decision)
    raw.update(_url_discovered=True, _needs_details=True)
    if explicit:
        raw.update(_watch=True, _explicit_watch=True)
    if failed:
        raw["_detail_error"] = True
    if retailer == "Falabella":
        raw["_falabella_watch"] = raw.get("_watch", False)
    return Product(
        identifier,
        name or "Producto pendiente de verificar",
        url,
        "POR_VERIFICAR",
        retailer,
        raw,
    )


def pending_detail(product):
    product.classification = "POR_VERIFICAR"
    product.raw["_detail_error"] = True
    previous = product.raw.get("_classification", {})
    missing = list(
        dict.fromkeys(previous.get("missing", []) + ["detalles verificables de ficha"])
    )
    product.raw["_classification"] = dict(
        previous, classification="POR_VERIFICAR", review_needed=True, missing=missing
    )
    return product


def remember(found, product):
    if product.classification == "DESCARTADO":
        found.pop(product.key, None)
        return
    previous = found.get(product.key)
    if (
        previous
        and product.raw.get("_detail_error")
        and not previous.raw.get("_detail_error")
    ):
        return
    aliases = [
        key for key, p in found.items() if p.url == product.url and key != product.key
    ]
    for key in aliases:
        if product.raw.get("_detail_error") and not found[key].raw.get("_detail_error"):
            return
        found.pop(key)
    found[product.key] = product


def relevant_card(metadata):
    metadata, _ = canonical_metadata(metadata)
    text = normalizar(
        str(metadata.get("productName") or "") + " " + str(metadata.get("brand") or "")
    )
    card = re.search(
        r"\b(tcg|trading|cartas|booster|deck|etb|binder|mini tin)\b|elite trainer|ultra (?:pc|premium)",
        text,
    )
    return bool(
        card
        and (
            re.search(r"\bpokemon\b", text)
            or re.search(
                r"\b30\s*th\b|\b30\s*[.oº°]*\s*(aniversario|anniversary)\b", text
            )
        )
    )


class DiscoveryPolicy:
    def discovery_terms(self):
        return list(dict.fromkeys(self.settings.terms + list(EXTRA_SEARCH_TERMS)))

    def failed_url(self, url, name="", metadata=None):
        return tracking_stub(url, self.retailer, name, metadata, failed=True)
