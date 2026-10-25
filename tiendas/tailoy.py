"""Búsqueda directa de Tai Loy y datos públicos de sus fichas (sin carrito)."""

import math
import re
import json
from urllib.parse import urljoin, urlsplit, parse_qs
from monitor.http import Client, RemoteError
from monitor.models import Product, SearchResult, Stock, normalize_url
from monitor.settings import Settings
from tiendas.ripley import CatalogHTML, evidence
from tiendas.vtex import classify, normalizar

BASE_URL = "https://www.tailoy.com.pe"
SEARCH_PATH = "/catalogsearch/result/index/"


class TailoyHTML(CatalogHTML):
    def __init__(self, text):
        self.product_links = []
        self.product_id = None
        self.product_sku = None
        self.primary_name = []
        self.primary_description = []
        self.primary_button = []
        self.micro_offer = {}
        self.preorder = False
        self.frames = []
        self.in_product_form = False
        super().__init__(text)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        primary = any("product-info-main" in frame["classes"] for frame in self.frames)
        if tag == "form" and fields.get("id") == "product_addtocart_form":
            self.in_product_form = True
            self.product_sku = fields.get("data-product-sku")
        if tag == "input" and self.in_product_form and fields.get("name") == "product":
            self.product_id = fields.get("value")
        if (
            tag == "input"
            and fields.get("name") == "fmepreorder"
            and fields.get("data-preorder") == "1"
        ):
            self.preorder = True
        if tag == "button" and fields.get("id") == "product-addtocart-button":
            if (
                "preorder-product" in fields.get("class", "").split()
                or "pre" in fields.get("title", "").lower()
            ):
                self.preorder = True
        if tag in ("meta", "link") and primary:
            prop = fields.get("itemprop")
            if prop in ("price", "priceCurrency", "availability"):
                self.micro_offer[prop] = fields.get("content") or fields.get("href")
        if (
            tag == "a"
            and fields.get("href")
            and any(
                c in fields.get("class", "").split()
                for c in ("product-item-link", "product-item-photo")
            )
        ):
            self.product_links.append(fields["href"])
        if tag not in (
            "meta",
            "link",
            "input",
            "img",
            "br",
            "hr",
            "source",
            "area",
            "base",
            "embed",
            "param",
            "track",
            "wbr",
        ):
            classes = fields.get("class", "").split()
            capture = []
            if fields.get("data-ui-id") == "page-title-wrapper":
                capture.append("name")
            if primary and (
                "label-des" in classes
                or ("product" in classes and "description" in classes)
            ):
                capture.append("description")
            if tag == "button" and fields.get("id") == "product-addtocart-button":
                capture.append("button")
            self.frames.append({"tag": tag, "classes": classes, "capture": capture})
        super().handle_starttag(tag, attrs)

    def handle_data(self, data):
        if any(frame["tag"] in ("script", "style") for frame in self.frames):
            if self.script is not None:
                super().handle_data(data)
            return
        if self.script is None and not any(
            frame["tag"] in ("script", "style") for frame in self.frames
        ):
            captures = {item for frame in self.frames for item in frame["capture"]}
            if "name" in captures:
                self.primary_name.append(data)
            if "description" in captures:
                self.primary_description.append(data)
            if "button" in captures:
                self.primary_button.append(data)
        super().handle_data(data)

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_product_form = False
        for n in range(len(self.frames) - 1, -1, -1):
            if self.frames[n]["tag"] == tag:
                del self.frames[n:]
                break
        super().handle_endtag(tag)


class Tailoy:
    base_url = BASE_URL
    retailer = "Tai Loy"

    def __init__(self, settings=None, client=None):
        self.settings = settings or Settings().for_retailer("tailoy")
        self.client = client or Client(self.settings)
        self.client.session.headers.update(
            {"Accept": "text/html,application/xhtml+xml"}
        )

    def normalize(self, raw):
        if not isinstance(raw, dict) or not raw.get("url"):
            raise ValueError("Ficha sin URL")
        url = normalize_url(urljoin(BASE_URL, raw["url"]), "Tai Loy")
        identifier = raw.get("sku") or raw.get("productID")
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, (str, int))
            or not str(identifier).strip()
        ):
            raise ValueError("Ficha sin identificador verificable")
        return Product(
            str(identifier).strip(),
            str(raw.get("name") or ""),
            url,
            classify(evidence(raw), self.settings.exclusions),
            retailer="Tai Loy",
            raw={"schema": raw},
        )

    def details(self, product):
        if product.retailer != "Tai Loy":
            raise ValueError("Producto de otra tienda")
        url = normalize_url(product.url, "Tai Loy")
        page = TailoyHTML(self.client.get(url, as_text=True))
        candidates = {}
        nodes = [
            r
            for r in page.products
            if r.get("name") or r.get("offers") or r.get("description")
        ]
        if not nodes and page.primary_name and (page.product_sku or page.product_id):
            nodes = [
                {
                    "@type": "Product",
                    "sku": page.product_sku,
                    "productID": page.product_id,
                    "url": page.canonical or url,
                    "name": " ".join(page.primary_name).strip(),
                    "description": " ".join(page.primary_description),
                    "offers": page.micro_offer,
                }
            ]
        for node in nodes:
            raw = dict(node)
            if not raw.get("url"):
                identifier = raw.get("@id")
                if isinstance(identifier, str):
                    raw["url"] = identifier.split("#", 1)[0]
                elif len(nodes) == 1:
                    raw["url"] = page.canonical or url
                else:
                    continue
            if not raw.get("sku") and not raw.get("productID") and len(nodes) == 1:
                raw["productID"] = page.product_id
            try:
                p = self.normalize(raw)
            except (ValueError, TypeError):
                continue
            if p.url != url and p.url != page.canonical:
                continue
            if product.id and not product.id.startswith("url:") and p.id != product.id:
                raise ValueError("Identidad de producto no coincide")
            if p.key in candidates and candidates[p.key].raw["schema"].get(
                "offers"
            ) != p.raw["schema"].get("offers"):
                raise ValueError("Ofertas contradictorias")
            candidates[p.key] = p
        if len(candidates) != 1:
            raise ValueError("Ficha sin producto/ofertas identificables")
        product = next(iter(candidates.values()))
        product.raw["_preorder"] = page.preorder
        label = normalizar(" ".join(page.primary_button))
        product.raw["_ui_unavailable"] = bool(
            re.search(r"no disponible|agotado|sin stock", label)
        )
        if (
            product.classification == "CANDIDATO"
            and product.raw["schema"].get("sku")
            and not product.raw["_preorder"]
            and not product.raw["_ui_unavailable"]
        ):
            availability = product.raw["schema"].get("offers") or {}
            if not (
                isinstance(availability, dict) and availability.get("availability")
            ):
                query = "query($sku:String!){products(filter:{sku:{eq:$sku}}){items{sku name __typename stock_status price_range{minimum_price{final_price{value currency}}}}}}"
                try:
                    response = self.client.get(
                        BASE_URL + "/graphql",
                        {"query": query, "variables": json.dumps({"sku": product.id})},
                    )
                    entries = (
                        response.get("data", {}).get("products", {}).get("items", [])
                        if isinstance(response, dict) and not response.get("errors")
                        else []
                    )
                    matches = [
                        entry
                        for entry in entries
                        if isinstance(entry, dict)
                        and str(entry.get("sku")) == product.id
                    ]
                    if len(matches) == 1:
                        product.raw["_graphql_stock"] = matches[0]
                except (RemoteError, ValueError, TypeError, AttributeError):
                    product.raw["_stock_query_error"] = True
        return product

    def search(self):
        result = SearchResult()
        found = {}
        cache = {}
        for term in self.settings.terms:
            seen = set()
            for page_number in range(1, self.settings.pages + 1):
                try:
                    params = {"q": term, "p": page_number}
                    if self.settings.tailoy_brand_filter:
                        params["tailoyetl_999_marcav2"] = (
                            self.settings.tailoy_brand_filter
                        )
                    page = TailoyHTML(
                        self.client.get(BASE_URL + SEARCH_PATH, params, as_text=True)
                    )
                    links = list(page.product_links)
                    links.extend(p.get("url") for p in page.products if p.get("url"))
                    urls = []
                    for link in links:
                        try:
                            url = normalize_url(urljoin(BASE_URL, link), "Tai Loy")
                        except (ValueError, TypeError):
                            continue
                        if url not in urls:
                            urls.append(url)
                    if not urls:
                        text = normalizar(" ".join(page.text))
                        if not re.search(
                            r"no (?:se )?encontr(?:aron|amos|o).*?(?:resultados|productos)|sin resultados|no hay resultados|no arrojo resultados|your search returned no results|no products found",
                            text,
                        ):
                            result.complete = False
                            result.errors.append("HTML sin listado interpretable")
                        break
                    if not set(urls) - seen:
                        result.complete = False
                        result.errors.append("Resultados repetidos")
                        break
                    seen.update(urls)
                    for url in urls:
                        try:
                            if url not in cache:
                                cache[url] = self.details(
                                    Product("", "", url, retailer="Tai Loy")
                                )
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
                        u for u, _ in page.links if "p=" in u
                    ]:
                        parsed = urlsplit(urljoin(BASE_URL, href))
                        if (
                            parsed.hostname != "www.tailoy.com.pe"
                            or not parsed.path.startswith("/catalogsearch/result")
                        ):
                            continue
                        try:
                            n = int(parse_qs(parsed.query).get("p", ["0"])[0])
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
                    result.errors.append("Búsqueda Tai Loy no compatible")
                    break
        result.products = list(found.values())
        result.errors = list(dict.fromkeys(result.errors))
        return result

    def stock(self, product):
        if not self.settings.context_confirmed:
            return Stock(detail="Interpretación del catálogo Tai Loy deshabilitada")
        if any(
            (
                self.settings.postal_code,
                self.settings.sales_channel,
                self.settings.seller_id,
                self.settings.sku_id,
            )
        ):
            return Stock(
                detail="Filtros de ubicación/canal/vendedor/variante no implementados en Tai Loy"
            )
        if product.raw.get("_preorder") or product.raw.get("_ui_unavailable"):
            return Stock(
                detail="Preventa/no disponible en la ficha; no se confirma disponibilidad"
            )
        backend = product.raw.get("_graphql_stock")
        if isinstance(backend, dict):
            if backend.get("__typename") != "SimpleProduct":
                return Stock(detail="Variantes sin disponibilidad detallada confirmada")
            state = backend.get("stock_status")
            if state == "OUT_OF_STOCK":
                return Stock("AGOTADO", detail="Magento confirma OUT_OF_STOCK")
            if state == "IN_STOCK":
                price_data = (
                    backend.get("price_range", {})
                    .get("minimum_price", {})
                    .get("final_price", {})
                )
                value = price_data.get("value")
                currency = price_data.get("currency")
                price = (
                    value
                    if isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value > 0
                    else None
                )
                if not isinstance(currency, str) or not re.fullmatch(
                    r"[A-Z]{3}", currency
                ):
                    currency = None
                return Stock(
                    "DISPONIBLE",
                    price,
                    currency,
                    "Magento confirma IN_STOCK; sin garantía de entrega",
                )
            return Stock(detail="Estado Magento no interpretable")
        offers = product.raw.get("schema", {}).get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        if not isinstance(offers, list) or not offers:
            return Stock(detail="Sin ofertas interpretables")
        available = []
        out = 0
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            value = str(offer.get("availability", "")).rstrip("/").rsplit("/", 1)[-1]
            if value == "InStock":
                available.append(offer)
            elif value == "OutOfStock":
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
                "Oferta pública InStock; sin garantía de entrega",
            )
        if out == len(offers):
            return Stock("AGOTADO", detail="Todas las ofertas confirman OutOfStock")
        return Stock(detail="Disponibilidad incompleta, preventa o no confirmada")
