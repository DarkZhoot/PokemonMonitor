"""Falabella: búsqueda pública filtrada y ofertas schema.org de venta propia.

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
                    cards = list(page.products)
                    for href, name in page.links:
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
                        try:
                            p = self.normalize(raw)
                        except (ValueError, TypeError, AttributeError):
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
                    if not urls and not empty:
                        raise ValueError(
                            "HTML sin listado interpretable; no se confirma búsqueda vacía"
                        )
                    fingerprint = tuple(sorted(urls))
                    if urls and fingerprint in seen_pages:
                        raise ValueError("Paginación repetida; cobertura incompleta")
                    seen_pages.add(fingerprint)
                    next_page = False
                    for href in page.next_links + [href for href, _ in page.links]:
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
