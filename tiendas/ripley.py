"""Lectura de HTML público y datos estándar schema.org de Ripley.

No presupone una API VTEX ni ejecuta JavaScript. Si el HTML no expone
productos/ofertas interpretables, se informa integración incompleta.
"""

import json
import math
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, urlencode, quote

from monitor.http import Client, RemoteError
from monitor.models import Product, SearchResult, Stock, normalize_url
from monitor.settings import Settings
from monitor.sellers import restrict_ripley, ripley_seller, schema_seller
from tiendas.vtex import classify, normalizar

BASE_URL = "https://simple.ripley.com.pe"


def is_product(node):
    kind = node.get("@type", []) if isinstance(node, dict) else []
    return "Product" in ([kind] if isinstance(kind, str) else kind)


def product_nodes(node):
    if isinstance(node, list):
        for value in node:
            yield from product_nodes(value)
    elif isinstance(node, dict):
        if is_product(node):
            yield node
            return  # Variants belong to this product, not separate primary cards.
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from product_nodes(value)


class CatalogHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.products = []
        self.links = []
        self.next_links = []
        self.canonical = None
        self.script = None
        self.script_kind = None
        self.next_data = None
        self.anchor = None
        self.text = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.script = []
            self.script_kind = (
                "ld"
                if attrs.get("type", "").lower() == "application/ld+json"
                else "next" if attrs.get("id") == "__NEXT_DATA__" else "ignore"
            )
        if tag == "link":
            rel = attrs.get("rel", "").split()
            if "canonical" in rel:
                self.canonical = attrs.get("href")
            if "next" in rel and attrs.get("href"):
                self.next_links.append(attrs["href"])
        if tag == "a" and attrs.get("href"):
            self.anchor = [attrs["href"], []]
            if "next" in attrs.get("rel", "").split():
                self.next_links.append(attrs["href"])
        if tag == "img" and self.anchor and attrs.get("alt"):
            self.anchor[1].append(attrs["alt"])

    def handle_data(self, data):
        if self.script is not None:
            if self.script_kind != "ignore":
                self.script.append(data)
        else:
            self.text.append(data)
            if self.anchor:
                self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            if self.script_kind != "ignore":
                try:
                    data = json.loads("".join(self.script))
                    if self.script_kind == "ld":
                        self.products.extend(product_nodes(data))
                    else:
                        self.next_data = data
                except (ValueError, TypeError):
                    pass
            self.script = None
            self.script_kind = None
        if tag == "a" and self.anchor:
            href, parts = self.anchor
            self.links.append((href, " ".join(parts).strip()))
            self.anchor = None


def evidence(raw):
    brand = raw.get("brand", "")
    if isinstance(brand, dict):
        brand = brand.get("name", "")
    data = dict(
        productName=raw.get("name", ""),
        description=raw.get("description", ""),
        brand=brand,
    )
    specs = {}
    for prop in raw.get("additionalProperty", []) or []:
        if isinstance(prop, dict) and prop.get("name"):
            specs[str(prop["name"])] = prop.get("value", "")
    data.update(specs)
    data["allSpecifications"] = list(specs)
    variants = raw.get("hasVariant", []) or []
    if isinstance(variants, dict):
        variants = [variants]
    data["items"] = [
        {"name": x.get("name", "")} for x in variants if isinstance(x, dict)
    ]
    return data


class Ripley:
    base_url = BASE_URL
    retailer = "Ripley"

    def __init__(self, settings=None, client=None):
        self.settings = settings or Settings().for_retailer("ripley")
        self.client = client or Client(self.settings)
        self.client.session.headers.update(
            {"Accept": "text/html,application/xhtml+xml"}
        )

    def normalize(self, raw):
        if not isinstance(raw, dict) or not raw.get("url"):
            raise ValueError("Ficha sin URL verificable")
        url = normalize_url(urljoin(BASE_URL, raw["url"]), "Ripley")
        match = re.search(
            r"([0-9]{6,}p|pmp[a-z0-9]+)$", urlsplit(url).path, re.IGNORECASE
        )
        if not match:
            raise ValueError("Identificador no reconocido")
        return restrict_ripley(
            Product(
                match[1].upper(),
                str(raw.get("name") or ""),
                url,
                classify(evidence(raw), self.settings.exclusions),
                retailer="Ripley",
                raw={"schema": raw, "_needs_details": not bool(raw.get("offers"))},
            )
        )

    def native_listing(self, page):
        try:
            listing = page.next_data["props"]["pageProps"]["findabilityProps"]["data"]
        except (KeyError, TypeError):
            return None
        if not isinstance(listing, dict) or not isinstance(
            listing.get("products"), list
        ):
            raise ValueError("Datos de búsqueda Ripley incompletos")
        return listing

    def normalize_native(self, raw, page):
        parent = str(raw.get("parentProductID") or "").upper()
        if not re.fullmatch(r"(?:[0-9]{6,}P|PMP[A-Z0-9]+)", parent):
            raise ValueError("Falta identificador de producto")
        url = None
        for href, _ in page.links:
            try:
                candidate = normalize_url(urljoin(BASE_URL, href), "Ripley")
            except (ValueError, TypeError):
                continue
            if urlsplit(candidate).path.upper().endswith(parent):
                url = candidate
                break
        if not url:
            raise ValueError("Falta enlace directo verificable")
        return restrict_ripley(
            Product(
                parent,
                str(raw.get("name") or ""),
                url,
                classify(
                    dict(
                        productName=raw.get("name", ""),
                        description=raw.get("description", ""),
                        brand=raw.get("brand", ""),
                    ),
                    self.settings.exclusions,
                ),
                retailer="Ripley",
                raw={"native": raw, "_needs_details": False},
            )
        )

    def details(self, product):
        if product.retailer != "Ripley":
            raise ValueError("Producto de otra tienda")
        url = normalize_url(product.url, "Ripley")
        page = CatalogHTML(self.client.get(url, as_text=True))
        return self.details_from_page(product, page)

    def details_from_page(self, product, page):
        if product.retailer != "Ripley":
            raise ValueError("Producto de otra tienda")
        url = normalize_url(product.url, "Ripley")
        matches = {}
        for raw in page.products:
            # Review-only Product nodes share @id but contain no product/offer data.
            if (
                not raw.get("name")
                and not raw.get("offers")
                and not raw.get("description")
            ):
                continue
            data = dict(raw)
            # A missing URL is accepted only with an explicit canonical product URL.
            if not data.get("url"):
                identifier = data.get("@id")
                if isinstance(identifier, str):
                    data["url"] = identifier.split("#", 1)[0]
                elif len(page.products) == 1:
                    data["url"] = page.canonical
                else:
                    continue
            try:
                p = self.normalize(data)
            except (ValueError, TypeError):
                continue
            if p.id == product.id.upper() or p.url == url:
                if p.key in matches and matches[p.key].raw["schema"].get(
                    "offers"
                ) != p.raw["schema"].get("offers"):
                    raise ValueError("Ofertas contradictorias en la ficha")
                matches[p.key] = p
        if len(matches) != 1:
            raise ValueError(
                "HTML sin ficha schema.org identificable; stock desconocido"
            )
        result = next(iter(matches.values()))
        result.raw["_needs_details"] = False
        return result

    def search(self):
        result = SearchResult()
        found = {}
        visited = set()
        for term in self.settings.terms:
            url = (
                BASE_URL
                + "/search/"
                + quote(term, safe="")
                + "?"
                + urlencode(
                    {
                        "sort": "relevance_desc",
                        "page": 1,
                        "facets": "Vendido por:Ripley",
                    }
                )
            )
            for page_number in range(self.settings.pages):
                if url in visited:
                    break
                visited.add(url)
                try:
                    page = CatalogHTML(self.client.get(url, as_text=True))
                    native = self.native_listing(page)
                    native_more = False
                    if native is not None:
                        native_products = native["products"]
                        offset, total, limit = (
                            native.get("offset"),
                            native.get("total"),
                            native.get("limit"),
                        )
                        if (
                            any(
                                type(value) is not int or value < 0
                                for value in (offset, total, limit)
                            )
                            or limit == 0
                        ):
                            raise ValueError("Paginación nativa incompleta")
                        requested = int(
                            parse_qs(urlsplit(url).query).get("page", ["1"])[0]
                        )
                        if offset != (requested - 1) * limit:
                            raise ValueError(
                                "La página devuelta no coincide con la solicitada"
                            )
                        if not native_products and offset < total:
                            raise ValueError(
                                "Página vacía antes de terminar los resultados"
                            )
                        for raw in native_products:
                            product = self.normalize_native(raw, page)
                            if product.classification != "DESCARTADO":
                                found[product.key] = product
                        native_more = offset + len(native_products) < total
                        if not native_more:
                            break
                        if page_number + 1 == self.settings.pages:
                            result.complete = False
                            result.errors.append("Límite de páginas alcanzado")
                            break
                        parsed = urlsplit(url)
                        params = parse_qs(parsed.query)
                        params["page"] = [str(requested + 1)]
                        url = urlunsplit(
                            (
                                parsed.scheme,
                                parsed.netloc,
                                parsed.path,
                                urlencode(params, doseq=True),
                                "",
                            )
                        )
                        continue
                    # Standard JSON-LD/anchor fallback: do not guess application API fields.
                    cards = list(page.products)
                    for href, name in page.links:
                        try:
                            link = normalize_url(urljoin(url, href), "Ripley")
                        except (ValueError, TypeError):
                            continue
                        cards.append({"url": link, "name": name})
                    valid = 0
                    for raw in cards:
                        try:
                            product = self.normalize(raw)
                        except (ValueError, TypeError):
                            continue
                        valid += 1
                        if product.key in found:
                            continue
                        if product.classification == "DESCARTADO":
                            continue
                        if product.raw["_needs_details"] and re.search(
                            r"\b(pokemon|tcg)\b", normalizar(product.name)
                        ):
                            try:
                                product = self.details(product)
                            except (RemoteError, ValueError, TypeError):
                                result.complete = False
                                result.errors.append("Ficha sin detalles confirmables")
                        if product.classification != "DESCARTADO":
                            found[product.key] = product
                    empty = re.search(
                        r"no encontramos (?:productos|resultados)|no se encontraron resultados|\b0 resultados\b|sin resultados",
                        normalizar(" ".join(page.text)),
                    )
                    if not valid and not empty:
                        raise ValueError(
                            "HTML sin listado compatible ni indicador de búsqueda vacía"
                        )
                    current = urlsplit(url)
                    current_page = int(parse_qs(current.query).get("page", ["1"])[0])
                    next_urls = list(page.next_links)
                    next_urls.extend(href for href, _ in page.links if "page=" in href)
                    next_url = None
                    for href in next_urls:
                        candidate = urljoin(url, href)
                        parsed = urlsplit(candidate)
                        if (
                            parsed.scheme != "https"
                            or parsed.netloc != "simple.ripley.com.pe"
                            or parsed.path != current.path
                        ):
                            continue
                        try:
                            candidate_page = int(
                                parse_qs(parsed.query).get("page", ["0"])[0]
                            )
                        except ValueError:
                            continue
                        if candidate_page == current_page + 1:
                            filters = parse_qs(parsed.query)
                            filters["facets"] = ["Vendido por:Ripley"]
                            filters["sort"] = ["relevance_desc"]
                            next_url = urlunsplit(
                                (
                                    parsed.scheme,
                                    parsed.netloc,
                                    parsed.path,
                                    urlencode(filters, doseq=True),
                                    "",
                                )
                            )
                            break
                    if not next_url:
                        break
                    if page_number + 1 == self.settings.pages:
                        result.complete = False
                        result.errors.append("Límite de páginas alcanzado")
                        break
                    url = next_url
                except RemoteError as error:
                    result.complete = False
                    result.errors.append(
                        f"HTTP {error.status}" if error.status else "Error de conexión"
                    )
                    break
                except (ValueError, TypeError, AttributeError):
                    result.complete = False
                    result.errors.append(
                        "HTML/listado Ripley no compatible; integración pendiente"
                    )
                    break
        result.products = list(found.values())
        return result

    def stock(self, product):
        if ripley_seller(product) != "ripley":
            return Stock(detail="Vendedor Ripley no confirmado; marketplace excluido")
        if not self.settings.context_confirmed:
            return Stock(detail="Interpretación de catálogo Ripley deshabilitada")
        if any(
            (
                self.settings.postal_code,
                self.settings.sales_channel,
                self.settings.seller_id,
                self.settings.sku_id,
            )
        ):
            return Stock(
                detail="Filtros logísticos/vendedor/SKU no soportados por los datos schema.org"
            )
        if product.raw.get("_needs_details"):
            return Stock(detail="Se requieren detalles de la ficha")
        native = product.raw.get("native")
        if isinstance(native, dict):
            available = native.get("inStock")
            if available is False:
                return Stock("AGOTADO", detail="Catálogo Ripley inStock=false")
            if available is not True:
                return Stock(detail="Catálogo Ripley sin disponibilidad explícita")
            value = native.get("priceNumber")
            price = (
                value
                if isinstance(value, (float, int))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value > 0
                else None
            )
            currency = native.get("currency")
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                currency = None
            return Stock(
                "DISPONIBLE",
                price,
                currency,
                "Catálogo Ripley inStock=true; sin garantía de entrega",
            )
        raw = product.raw.get("schema", {})
        offers = raw.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        if not isinstance(offers, list) or not offers:
            return Stock(detail="Sin ofertas interpretables")
        unknown_seller = any(schema_seller(offer) == "unknown" for offer in offers)
        offers = [offer for offer in offers if schema_seller(offer) == "ripley"]
        if not offers:
            return Stock(detail="No hay ofertas confirmadas vendidas por Ripley")
        available = []
        out = 0
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            availability = (
                str(offer.get("availability", "")).rstrip("/").rsplit("/", 1)[-1]
            )
            if availability == "InStock":
                available.append(offer)
            elif availability == "OutOfStock":
                out += 1
        if available:
            prices = []
            for offer in available:
                try:
                    value = offer.get("price")
                    if isinstance(value, bool):
                        continue
                    price = float(value)
                    if not math.isfinite(price) or price <= 0:
                        continue
                    currency = offer.get("priceCurrency")
                    if not isinstance(currency, str) or not re.fullmatch(
                        r"[A-Z]{3}", currency
                    ):
                        currency = None
                    prices.append((price, currency))
                except (ValueError, TypeError):
                    pass
            price, currency = (
                min(prices, key=lambda x: x[0]) if prices else (None, None)
            )
            return Stock(
                "DISPONIBLE",
                price,
                currency,
                "Oferta schema.org InStock; sin garantía de entrega",
            )
        if out == len(offers) and not unknown_seller:
            return Stock("AGOTADO", detail="Todas las ofertas confirman OutOfStock")
        return Stock(detail="Disponibilidad ausente, ambigua o no confirmada")
