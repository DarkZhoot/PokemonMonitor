"""Adaptador de solo lectura para el catálogo público VTEX."""

import math
import re
import unicodedata
from urllib.parse import urljoin
from monitor.http import Client, RemoteError
from monitor.models import Product, SearchResult, Stock, normalize_url
from monitor.settings import Settings

BASE_URL = "https://www.plazavea.com.pe"
API = BASE_URL + "/api/catalog_system/pub/products/search"


def normalizar(text):
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", str(text).lower())
        if not unicodedata.combining(c)
    )


def classify(raw, exclusions):
    fields = [
        raw.get(k, "") for k in ("productName", "description", "brand", "categories")
    ]
    for key in raw.get("allSpecifications", []) or []:
        if isinstance(key, str):
            fields.extend([key, raw.get(key, "")])
    for item in raw.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        fields.extend(
            item.get(k, "") for k in ("name", "nameComplete", "complementName")
        )
        for key in item.get("variations", []) or []:
            if isinstance(key, str):
                fields.extend([key, item.get(key, "")])
    text = normalizar(" ".join(str(x) for x in fields))
    if any(
        re.search(r"(?<!\w)" + re.escape(normalizar(x)) + r"(?!\w)", text)
        for x in exclusions
    ):
        return "DESCARTADO"
    if not re.search(r"\bpokemon\b", text) or not re.search(
        r"\b(tcg|trading card game)\b", text
    ):
        return "POR_VERIFICAR"
    if not re.search(
        r"\b30\s*[.oº°]*\s*(aniversario|anniversary)\b|\b30th\s+anniversary\b", text
    ):
        return "POR_VERIFICAR"
    return "CANDIDATO" if re.search(r"\b(ingles|english)\b", text) else "POR_VERIFICAR"


class PlazaVea:
    def __init__(self, settings=None, client=None):
        self.settings = settings or Settings()
        self.client = client or Client(self.settings)

    def normalize(self, raw):
        if not isinstance(raw, dict) or not raw.get("productId"):
            raise ValueError("Falta productId")
        link = raw.get("link") or (
            "/" + raw["linkText"] + "/p" if raw.get("linkText") else ""
        )
        if not link:
            raise ValueError("Falta URL")
        return Product(
            str(raw["productId"]),
            str(raw.get("productName") or ""),
            normalize_url(urljoin(BASE_URL, link)),
            classify(raw, self.settings.exclusions),
            raw=raw,
        )

    def params(self):
        return (
            {"sc": self.settings.sales_channel} if self.settings.sales_channel else {}
        )

    def search(self):
        result = SearchResult()
        found = {}
        for term in self.settings.terms:
            seen = set()
            for page in range(self.settings.pages):
                try:
                    data = self.client.get(
                        API + "/" + term,
                        dict(self.params(), _from=page * 20, _to=page * 20 + 19),
                    )
                    if data is None or data == []:
                        break
                    if not isinstance(data, list):
                        raise ValueError("Respuesta no es lista")
                    ids = set()
                    for raw in data:
                        p = self.normalize(raw)
                        ids.add(p.id)
                        if p.classification != "DESCARTADO":
                            found[p.id] = p
                    if not ids - seen:
                        result.complete = False
                        result.errors.append("Resultados repetidos")
                        break
                    seen.update(ids)
                    if len(data) < 20:
                        break
                    if page == self.settings.pages - 1:
                        result.complete = False
                        result.errors.append("Límite de páginas alcanzado")
                except (RemoteError, ValueError, TypeError, AttributeError):
                    result.complete = False
                    result.errors.append(f"{term}: consulta incompleta")
                    break
        result.products = list(found.values())
        return result

    def details(self, product):
        data = (
            self.client.get(API, dict(self.params(), fq="productId:" + product.id))
            if product.id
            else []
        )
        if isinstance(data, list):
            matches = [
                self.normalize(x)
                for x in data
                if isinstance(x, dict) and str(x.get("productId")) == product.id
            ]
            if len(matches) == 1 and (
                matches[0].url == product.url
                or str(product.raw.get("productId")) == product.id
            ):
                return matches[0]
        slug = product.url.split("www.plazavea.com.pe/", 1)[1][:-2].rstrip("/")
        data = self.client.get(API + "/" + slug + "/p", self.params())
        if not isinstance(data, list):
            raise ValueError("Detalles inválidos")
        matches = [self.normalize(x) for x in data]
        matches = [p for p in matches if p.url == product.url]
        if len(matches) != 1:
            raise ValueError("Identidad no confirmada")
        return matches[0]

    def stock(self, product):
        if not self.settings.context_confirmed:
            return Stock(detail="Contexto de entrega/catálogo no validado")
        if self.settings.postal_code:
            return Stock(
                detail="Consulta logística por código postal pendiente de implementar"
            )
        items = product.raw.get("items")
        if not isinstance(items, list) or not items:
            return Stock(detail="Sin variantes")
        offers = []
        incomplete = False
        for item in items:
            if not isinstance(item, dict):
                incomplete = True
                continue
            if self.settings.sku_id and str(item.get("itemId")) != self.settings.sku_id:
                continue
            sellers = item.get("sellers")
            if not isinstance(sellers, list) or not sellers:
                incomplete = True
                continue
            for seller in sellers:
                if not isinstance(seller, dict):
                    incomplete = True
                    continue
                if (
                    self.settings.seller_id
                    and str(seller.get("sellerId")) != self.settings.seller_id
                ):
                    continue
                offer = seller.get("commertialOffer")
                if not isinstance(offer, dict):
                    incomplete = True
                    continue
                available = offer.get("IsAvailable")
                quantity = offer.get("AvailableQuantity")
                numeric = (
                    isinstance(quantity, (int, float))
                    and not isinstance(quantity, bool)
                    and math.isfinite(quantity)
                    and quantity >= 0
                )
                if available is True and numeric and quantity > 0:
                    offers.append(offer)
                elif available is False and numeric and quantity == 0:
                    offers.append(None)
                else:
                    incomplete = True
        positive = [o for o in offers if o is not None]
        if positive:
            prices = [
                o.get("Price")
                for o in positive
                if isinstance(o.get("Price"), (int, float))
                and not isinstance(o.get("Price"), bool)
                and math.isfinite(o["Price"])
                and o["Price"] > 0
            ]
            price = min(prices) if prices else None
            currencies = {
                o.get("CurrencyCode") for o in positive if o.get("Price") == price
            }
            currency = next(iter(currencies)) if len(currencies) == 1 else None
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                currency = None
            return Stock(
                "DISPONIBLE",
                price,
                currency,
                detail="Oferta positiva en contexto validado",
            )
        if offers and not incomplete:
            return Stock(
                "AGOTADO", detail="Todas las ofertas seleccionadas confirman cero stock"
            )
        return Stock(detail="Ofertas incompletas o contradictorias")


def consultar_producto(url):
    adapter = PlazaVea(Settings.load())
    try:
        p = adapter.details(Product("", "", normalize_url(url)))
        s = adapter.stock(p)
        return dict(
            url=p.url, nombre=p.name, precio=s.price, estado=s.state, detalle=s.detail
        )
    except (RemoteError, ValueError, TypeError, AttributeError):
        return dict(
            url=url,
            nombre=None,
            precio=None,
            estado="DESCONOCIDO",
            detalle="Consulta no confirmada",
        )
