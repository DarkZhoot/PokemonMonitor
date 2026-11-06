"""Falabella: listado Next.js público y ofertas schema.org de venta propia.

Sin API privada ni ejecución de JavaScript. Un HTML no interpretable se declara
incompleto; jamás se interpreta un bloqueo o dato ausente como stock agotado.
"""

import math
import re
from urllib.parse import urljoin, urlsplit, parse_qs

from monitor.http import Client, RemoteError
from monitor.models import Product, SearchResult, Stock, normalize_url
from monitor.settings import Settings
from monitor.classification import assess, annotated_raw
from monitor.sellers import restrict_falabella, falabella_offer_seller
from tiendas.ripley import CatalogHTML, evidence
from tiendas.vtex import normalizar

BASE_URL = "https://www.falabella.com.pe"
SEARCH_URL = BASE_URL + "/falabella-pe/search"
SELLER_PARAM = "f.derived.variant.sellerId"


EXTRA_SEARCH_TERMS = ("tcg 30", "tcg 30th")


def falabella_evidence(raw):
    metadata = evidence(raw)
    name = re.sub(
        r"\btcg(?=30(?:th)?\b)",
        "TCG ",
        str(metadata.get("productName") or ""),
        flags=re.IGNORECASE,
    )
    context = normalizar(name + " " + str(metadata.get("brand") or ""))
    family = re.search(
        r"\b(ultra\s+(?:pc|premium)|upc|mini\s+tin|booster|binder|elite\s+trainer|etb|figure|figura)\b",
        context,
    )
    aliases = []
    if family and re.search(r"\b(tcg|trading card game)\b", context):
        if re.search(r"\b30\s*th\b", name, re.IGNORECASE) and not re.search(
            r"anniversary|aniversario", name, re.IGNORECASE
        ):
            name = re.sub(r"\b30\s*th\b", "30th anniversary", name, flags=re.IGNORECASE)
            aliases.append("30th: aniversario en familia TCG")
        if re.search(r"\bing\b", name, re.IGNORECASE):
            name = re.sub(r"\bing\b", "ingles", name, flags=re.IGNORECASE)
            aliases.append("ing: ingles")
        # A bilingual abbreviation must not be silently treated as English-only.
        name = re.sub(r"\besp\b", "espanol", name, flags=re.IGNORECASE)
    metadata["productName"] = name
    return metadata, aliases


def near_anniversary(metadata):
    text = normalizar(
        str(metadata.get("productName") or "") + " " + str(metadata.get("brand") or "")
    )
    return bool(
        re.search(r"\b(tcg|trading card game)\b", text)
        and re.search(r"\b30\s*th\b|\b30\s*[.oº°]*\s*(aniversario|anniversary)\b", text)
    )


def relevant_card(raw):
    text = normalizar(
        str(raw.get("displayName") or "") + " " + str(raw.get("brand") or "")
    )
    text = re.sub(r"\btcg(?=30(?:th)?\b)", "tcg ", text)
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


class Falabella:
    retailer = "Falabella"

    def __init__(self, settings=None, client=None):
        self.settings = settings or Settings().for_retailer("falabella")
        self.client = client or Client(self.settings)
        self.client.session.headers.update(
            {"Accept": "text/html,application/xhtml+xml"}
        )

    def normalize(self, raw):
        url = normalize_url(urljoin(BASE_URL, raw.get("url", "")), self.retailer)
        identifier = urlsplit(url).path.split("/")[3]
        metadata, aliases = falabella_evidence(raw)
        decision = assess(metadata, self.settings.exclusions)
        product = restrict_falabella(
            Product(
                identifier,
                str(raw.get("name") or ""),
                url,
                decision["classification"],
                retailer=self.retailer,
                raw=annotated_raw({"schema": raw, "_needs_details": True}, decision),
            )
        )
        offers = raw.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        product.raw["_falabella_watch"] = (
            product.classification != "DESCARTADO"
            and near_anniversary(metadata)
            and isinstance(offers, list)
            and any(falabella_offer_seller(x) == "falabella" for x in offers)
        )
        if aliases:
            product.raw["_metadata_aliases"] = aliases
        return product

    def discovery_terms(self):
        return list(dict.fromkeys(self.settings.terms + list(EXTRA_SEARCH_TERMS)))

    def tracking_stub(self, url):
        product = self.normalize({"url": normalize_url(url, self.retailer), "name": ""})
        product.name = "Producto Falabella pendiente de verificar"
        product.raw["_falabella_watch"] = True
        product.raw["_explicit_watch"] = True
        return product

    def native_listing(self, page):
        data = page.next_data
        if not isinstance(data, dict):
            return None
        props = data.get("props", {}).get("pageProps", {})
        if not isinstance(props, dict) or "results" not in props:
            return None
        results, pagination = props["results"], props.get("pagination")
        if not isinstance(results, list) or not isinstance(pagination, dict):
            raise ValueError("Listado Next.js Falabella incompleto")
        for name in ("count", "perPage", "currentPage"):
            value = pagination.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("Paginación Falabella no interpretable")
        if (
            pagination["count"] < 0
            or pagination["perPage"] < 1
            or pagination["currentPage"] < 1
            or len(results) > pagination["perPage"]
            or len(results) > pagination["count"]
        ):
            raise ValueError("Paginación Falabella contradictoria")
        if not results and pagination["count"] != 0:
            raise ValueError("Resultados ausentes con conteo no vacío")
        return results, pagination

    def normalize_native(self, raw):
        if not isinstance(raw, dict):
            raise ValueError("Tarjeta Falabella inválida")
        schema = {
            "url": raw.get("url"),
            "name": raw.get("displayName", ""),
            "brand": raw.get("brand", ""),
        }
        seller_id, seller_name = raw.get("sellerId"), raw.get("sellerName")
        # These identifiers were observed in the public Peruvian listing.
        # The search filter alone does not establish the card's seller.
        if (
            seller_id == "FALABELLA_PERU"
            and isinstance(seller_name, str)
            and seller_name.strip().casefold() in ("falabella", "saga falabella")
        ):
            schema["offers"] = {"seller": {"name": "Falabella"}}
        elif isinstance(seller_id, str) and seller_id and seller_id != "FALABELLA_PERU":
            schema["offers"] = {"seller": {"name": "Marketplace"}}
        product = self.normalize(schema)
        identifier = raw.get("productId")
        if not isinstance(identifier, str) or identifier != product.id:
            raise ValueError("Identificador no coincide con la URL Falabella")
        product.raw["native_listing"] = {
            k: raw.get(k) for k in ("productId", "skuId", "sellerId", "sellerName")
        }
        return product

    def details(self, product):
        if product.retailer != self.retailer:
            raise ValueError("Producto de otra tienda")
        url = normalize_url(product.url, self.retailer)
        page = CatalogHTML(self.client.get(url, as_text=True))
        matches = []
        for raw in page.products:
            data = dict(raw)
            if not data.get("url"):
                identity = data.get("@id")
                data["url"] = (
                    identity.split("#", 1)[0]
                    if isinstance(identity, str)
                    else page.canonical
                )
            try:
                candidate = self.normalize(data)
            except (ValueError, TypeError, AttributeError):
                continue
            if candidate.id == product.id or candidate.url == url:
                matches.append(candidate)
        if not matches:
            raise ValueError(
                "Ficha Falabella sin identidad/ofertas públicas interpretables"
            )
        # Duplicate conflicting product nodes must not select an arbitrary offer.
        if any(p.raw["schema"] != matches[0].raw["schema"] for p in matches[1:]):
            raise ValueError("Ficha Falabella con datos contradictorios")
        p = matches[0]
        p.raw["_needs_details"] = False
        if p.classification != "DESCARTADO" and product.raw.get("_falabella_watch"):
            p.raw["_falabella_watch"] = True
        if product.raw.get("_explicit_watch"):
            p.raw["_explicit_watch"] = True
        return p

    def search(self):
        result = SearchResult()
        found, cache = {}, {}
        for term in self.discovery_terms():
            params = {"Ntt": term, "facetSelected": "true", SELLER_PARAM: "FALABELLA"}
            seen_pages = set()
            for number in range(1, self.settings.pages + 1):
                params["page"] = number
                try:
                    page = CatalogHTML(
                        self.client.get(SEARCH_URL, params=params, as_text=True)
                    )
                    native = self.native_listing(page)
                    if native is not None and native[1]["currentPage"] != number:
                        raise ValueError("Falabella devolvió otra página")
                    cards = (
                        list(native[0]) if native is not None else list(page.products)
                    )
                    for href, name in [] if native is not None else page.links:
                        try:
                            link = normalize_url(urljoin(BASE_URL, href), self.retailer)
                        except (ValueError, TypeError):
                            continue
                        if re.search(
                            r"\b(pokemon|tcg)\b",
                            normalizar(name + " " + link.replace("-", " ")),
                        ):
                            cards.append({"url": link, "name": name})
                    urls = set()
                    for raw in cards:
                        if (
                            native is not None
                            and isinstance(raw, dict)
                            and not relevant_card(raw)
                        ):
                            continue
                        try:
                            p = (
                                self.normalize_native(raw)
                                if native is not None
                                else self.normalize(raw)
                            )
                        except (ValueError, TypeError, AttributeError):
                            if native is not None:
                                result.complete = False
                                if (
                                    "Tarjeta Falabella sin identidad confirmable"
                                    not in result.errors
                                ):
                                    result.errors.append(
                                        "Tarjeta Falabella sin identidad confirmable"
                                    )
                            continue
                        urls.add(p.url)
                        if p.classification == "DESCARTADO":
                            continue
                        # Retain a discovered URL even if the detail request fails.
                        listed = p
                        found.setdefault(p.key, p)
                        if p.url not in cache:
                            try:
                                cache[p.url] = self.details(p)
                            except RemoteError:
                                pending_detail(listed)
                                cache[listed.url] = None
                                raise
                            except (ValueError, TypeError, AttributeError):
                                cache[p.url] = None
                        p = cache[p.url]
                        if p is None:
                            pending_detail(listed)
                            result.complete = False
                            if (
                                "Ficha Falabella sin datos confirmables"
                                not in result.errors
                            ):
                                result.errors.append(
                                    "Ficha Falabella sin datos confirmables"
                                )
                            continue
                        if p.classification != "DESCARTADO":
                            found[p.key] = p
                        else:
                            found.pop(p.key, None)
                    empty = re.search(
                        r"no encontramos (?:resultados|productos)|no se encontraron (?:resultados|productos)|\b0 resultados\b|sin resultados",
                        normalizar(" ".join(page.text)),
                    )
                    if native is not None:
                        empty = native[1]["count"] == 0
                    if not urls and not empty and native is None:
                        raise ValueError(
                            "HTML sin listado interpretable; no se confirma búsqueda vacía"
                        )
                    fingerprint = (
                        tuple(
                            sorted(
                                str(x.get("url", ""))
                                for x in native[0]
                                if isinstance(x, dict)
                            )
                        )
                        if native is not None
                        else tuple(sorted(urls))
                    )
                    if fingerprint and fingerprint in seen_pages:
                        raise ValueError("Paginación repetida; cobertura incompleta")
                    seen_pages.add(fingerprint)
                    next_page = (
                        (native[1]["count"] > number * native[1]["perPage"])
                        if native is not None
                        else False
                    )
                    for href in (
                        []
                        if native is not None
                        else page.next_links + [href for href, _ in page.links]
                    ):
                        parsed = urlsplit(urljoin(SEARCH_URL, href))
                        query = parse_qs(parsed.query)
                        if (
                            parsed.scheme == "https"
                            and parsed.netloc == "www.falabella.com.pe"
                            and parsed.path == "/falabella-pe/search"
                            and query.get("page") == [str(number + 1)]
                            and query.get("Ntt", [term]) == [term]
                        ):
                            next_page = True
                            break
                    if not next_page:
                        break
                    if number == self.settings.pages:
                        raise ValueError("Límite de páginas alcanzado")
                    # Rebuild query locally so pagination cannot drop the seller filter.
                except RemoteError as error:
                    result.complete = False
                    result.errors.append(
                        f"Falabella HTTP {error.status}"
                        if error.status
                        else "Falabella: error de conexión"
                    )
                    if error.status in (401, 403, 429):
                        result.products = list(found.values())
                        return result
                    break
                except (ValueError, TypeError, AttributeError):
                    result.complete = False
                    result.errors.append(
                        "Listado/ficha Falabella no interpretable o incompleto; no se infiere agotado"
                    )
                    break
        result.products = list(found.values())
        return result

    def stock(self, product):
        if product.retailer != self.retailer:
            return Stock(detail="Producto de otra tienda")
        if not self.settings.context_confirmed:
            return Stock(detail="Interpretación del catálogo Falabella deshabilitada")
        if any(
            (
                self.settings.postal_code,
                self.settings.sales_channel,
                self.settings.seller_id,
                self.settings.sku_id,
            )
        ):
            return Stock(detail="Filtros logísticos/SKU no soportados por schema.org")
        if product.raw.get("_needs_details", True):
            return Stock(detail="Se requieren detalles confirmados de la ficha")
        offers = product.raw.get("schema", {}).get("offers", [])
        if isinstance(offers, dict):
            offers = [offers]
        if not isinstance(offers, list):
            return Stock(detail="Sin ofertas interpretables")
        own = [x for x in offers if falabella_offer_seller(x) == "falabella"]
        if not own:
            return Stock(
                detail="Vendedor Falabella no confirmado; marketplace excluido"
            )
        statuses = [
            str(x.get("availability", "")).rstrip("/").rsplit("/", 1)[-1] for x in own
        ]
        available = [x for x, state in zip(own, statuses) if state == "InStock"]
        if available:
            prices = []
            for offer in available:
                try:
                    value = offer.get("price")
                    if isinstance(value, bool):
                        continue
                    value = float(value)
                    currency = offer.get("priceCurrency")
                    if math.isfinite(value) and value > 0:
                        prices.append(
                            (
                                value,
                                (
                                    currency
                                    if isinstance(currency, str)
                                    and re.fullmatch(r"[A-Z]{3}", currency)
                                    else None
                                ),
                            )
                        )
                except (ValueError, TypeError):
                    pass
            price, currency = (
                min(prices, key=lambda x: x[0]) if prices else (None, None)
            )
            return Stock(
                "DISPONIBLE",
                price,
                currency,
                "Oferta de Falabella InStock; sin garantía de entrega por ubicación",
            )
        if all(state == "OutOfStock" for state in statuses) and not any(
            falabella_offer_seller(x) == "unknown" for x in offers
        ):
            return Stock(
                "AGOTADO", detail="Todas las ofertas propias confirman OutOfStock"
            )
        return Stock(detail="Disponibilidad propia ausente, ambigua o preventa")
