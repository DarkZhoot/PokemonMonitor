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
from monitor.sellers import restrict_falabella, falabella_offer_seller
from tiendas.ripley import CatalogHTML, evidence
from tiendas.vtex import classify, normalizar

BASE_URL = "https://www.falabella.com.pe"
SEARCH_URL = BASE_URL + "/falabella-pe/search"
SELLER_PARAM = "f.derived.variant.sellerId"


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
        return restrict_falabella(
            Product(
                identifier,
                str(raw.get("name") or ""),
                url,
                classify(evidence(raw), self.settings.exclusions),
                retailer=self.retailer,
                raw={"schema": raw, "_needs_details": True},
            )
        )

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
        return p

    def search(self):
        result = SearchResult()
        found, cache = {}, {}
        for term in self.settings.terms:
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
                        if native is not None and isinstance(raw, dict):
                            # Falabella may broaden a failed query into toys/clothing.
                            # Avoid fetching unrelated detail pages in every cycle.
                            text = normalizar(
                                str(raw.get("displayName", ""))
                                + " "
                                + str(raw.get("brand", ""))
                            )
                            if not re.search(r"\bpokemon\b", text) or not re.search(
                                r"\b(tcg|trading|cartas|booster|deck|etb|anniversary|aniversario)\b|elite trainer",
                                text,
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
                        if p.url not in cache:
                            try:
                                cache[p.url] = self.details(p)
                            except RemoteError:
                                raise
                            except (ValueError, TypeError, AttributeError):
                                cache[p.url] = None
                        p = cache[p.url]
                        if p is None:
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
