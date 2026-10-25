"""Búsqueda directa de Ilahui (Shopify) y fichas públicas de producto."""

import json
import re
from urllib.parse import urljoin, urlsplit, parse_qs

from monitor.http import Client, RemoteError
from monitor.models import Product, SearchResult, Stock, normalize_url
from monitor.settings import Settings
from tiendas.ripley import CatalogHTML
from tiendas.vtex import classify, normalizar

BASE_URL = "https://ilahuiperu.com"


class IlahuiHTML(CatalogHTML):
    def __init__(self, text):
        self.currency = None
        super().__init__(text)
        # Shopify declares the active storefront currency as JSON, without eval.
        match = re.search(r"Shopify\.currency\s*=\s*(\{[^;]+\})\s*;", text)
        if match:
            try:
                value = json.loads(match[1]).get("active")
                if isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value):
                    self.currency = value
            except (ValueError, AttributeError):
                pass

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "meta" and fields.get("property") in (
            "product:price:currency",
            "og:price:currency",
        ):
            value = fields.get("content")
            if isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value):
                self.currency = value
        super().handle_starttag(tag, attrs)


class Ilahui:
    base_url = BASE_URL
    retailer = "Ilahui"

    def __init__(self, settings=None, client=None):
        self.settings = settings or Settings().for_retailer("ilahui")
        self.client = client or Client(self.settings)
        self.client.session.headers.update(
            {"Accept": "text/html,application/json;q=0.9"}
        )

    def normalize(self, raw):
        if (
            not isinstance(raw, dict)
            or type(raw.get("id")) is not int
            or raw["id"] <= 0
        ):
            raise ValueError("Falta identificador Shopify")
        handle = raw.get("handle")
        if (
            not isinstance(handle, str)
            or not handle
            or "/" in handle
            or "?" in handle
            or "#" in handle
        ):
            raise ValueError("Handle inválido")
        url = normalize_url(BASE_URL + "/products/" + handle, "Ilahui")
        metadata = dict(
            productName=raw.get("title", ""),
            description=raw.get("description", ""),
            brand=raw.get("vendor", ""),
        )
        # Tags and variants can explicitly identify the collection/language.
        metadata["allSpecifications"] = ["Etiquetas", "Tipo de producto"]
        metadata["Tipo de producto"] = raw.get("type", "")
        metadata["Etiquetas"] = raw.get("tags", [])
        variants = raw.get("variants")
        metadata["items"] = (
            [{"name": v.get("title", "")} for v in variants if isinstance(v, dict)]
            if isinstance(variants, list)
            else []
        )
        return Product(
            str(raw["id"]),
            str(raw.get("title") or ""),
            url,
            classify(metadata, self.settings.exclusions),
            retailer="Ilahui",
            raw=raw,
        )

    def details(self, product):
        if product.retailer != "Ilahui":
            raise ValueError("Producto de otra tienda")
        url = normalize_url(product.url, "Ilahui")
        data = self.client.get(url + ".js")
        p = self.normalize(data)
        if product.id.isdigit():
            if p.id != product.id:
                raise ValueError("Identificador de producto no coincide")
        elif p.url != url:
            raise ValueError("Ficha distinta del enlace solicitado")
        currency = data.get("currency") or product.raw.get("_store_currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            page = IlahuiHTML(self.client.get(p.url, as_text=True))
            currency = page.currency
        p.raw["_store_currency"] = currency
        return p

    def search(self):
        result = SearchResult()
        found = {}
        cache = {}
        for term in self.settings.terms:
            seen = set()
            for page_number in range(1, self.settings.pages + 1):
                try:
                    params = {
                        "q": term,
                        "type": "product",
                        "options[unavailable_products]": "last",
                        "options[prefix]": "last",
                        "options[fields]": "title,vendor,product_type,variants.title",
                        "page": page_number,
                    }
                    page = IlahuiHTML(
                        self.client.get(BASE_URL + "/search", params, as_text=True)
                    )
                    urls = []
                    for href, _ in page.links:
                        try:
                            url = normalize_url(urljoin(BASE_URL, href), "Ilahui")
                        except (ValueError, TypeError):
                            continue
                        if url not in urls:
                            urls.append(url)
                    if not urls:
                        text = normalizar(" ".join(page.text))
                        if not re.search(
                            r"\b0 resultados\b|no (?:se )?encontr(?:aron|amos|o).*resultados|sin resultados|no results|0 results",
                            text,
                        ):
                            result.complete = False
                            result.errors.append("Búsqueda sin listado interpretable")
                        break
                    if not set(urls) - seen:
                        result.complete = False
                        result.errors.append("Resultados repetidos")
                        break
                    seen.update(urls)
                    for url in urls:
                        try:
                            if url not in cache:
                                skeleton = Product(
                                    "",
                                    "",
                                    url,
                                    retailer="Ilahui",
                                    raw={"_store_currency": page.currency},
                                )
                                cache[url] = self.details(skeleton)
                            p = cache[url]
                            if p.classification != "DESCARTADO":
                                found[p.key] = p
                        except (RemoteError, ValueError, TypeError, AttributeError):
                            result.complete = False
                            result.errors.append(
                                "Ficha no verificable; no se infiere stock"
                            )
                    more = False
                    for href in page.next_links + [
                        u for u, _ in page.links if "page=" in u
                    ]:
                        target = urlsplit(urljoin(BASE_URL, href))
                        if (
                            target.hostname != "ilahuiperu.com"
                            or target.path.rstrip("/") != "/search"
                        ):
                            continue
                        query = parse_qs(target.query)
                        try:
                            n = int(query.get("page", ["0"])[0])
                        except ValueError:
                            continue
                        if n == page_number + 1:
                            more = True
                            break
                    if not more:
                        break
                    if page_number == self.settings.pages:
                        result.complete = False
                        result.errors.append("Límite de páginas alcanzado")
                except RemoteError as error:
                    result.complete = False
                    result.errors.append(
                        f"HTTP {error.status}" if error.status else "Error de conexión"
                    )
                    break
                except (ValueError, TypeError, AttributeError):
                    result.complete = False
                    result.errors.append("Respuesta de búsqueda no compatible")
                    break
        result.products = list(found.values())
        result.errors = list(dict.fromkeys(result.errors))
        return result

    def stock(self, product):
        if not self.settings.context_confirmed:
            return Stock(detail="Interpretación del catálogo Ilahui deshabilitada")
        if any(
            (
                self.settings.postal_code,
                self.settings.sales_channel,
                self.settings.seller_id,
            )
        ):
            return Stock(
                detail="Filtros de ubicación/canal/vendedor no soportados en Ilahui"
            )
        variants = product.raw.get("variants")
        if not isinstance(variants, list) or not variants:
            return Stock(detail="Sin variantes interpretables")
        selected = []
        unknown = False
        for variant in variants:
            if not isinstance(variant, dict):
                unknown = True
                continue
            if self.settings.sku_id and str(variant.get("id")) != self.settings.sku_id:
                continue
            selected.append(variant)
            if type(variant.get("available")) is not bool:
                unknown = True
        if not selected:
            return Stock(detail="No se encontró la variante solicitada")
        positive = [v for v in selected if v.get("available") is True]
        parent = product.raw.get("available")
        if positive and parent is False:
            return Stock(detail="Disponibilidad de producto y variantes contradictoria")
        if positive:
            prices = [
                v["price"] / 100
                for v in positive
                if type(v.get("price")) is int and v["price"] > 0
            ]
            currency = product.raw.get("_store_currency")
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                currency = None
            return Stock(
                "DISPONIBLE",
                min(prices) if prices else None,
                currency,
                "Variante Shopify disponible para compra; sin garantía de entrega",
            )
        if not unknown and all(v["available"] is False for v in selected):
            if not self.settings.sku_id and parent is True:
                return Stock(detail="Disponibilidad contradictoria")
            return Stock(
                "AGOTADO",
                detail="Todas las variantes seleccionadas confirman sin disponibilidad",
            )
        return Stock(detail="Disponibilidad incompleta o no confirmada")
