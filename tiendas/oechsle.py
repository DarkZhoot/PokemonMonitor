"""Catálogo VTEX de Oechsle: exclusivamente ventas propias confirmadas."""

from copy import deepcopy
from dataclasses import replace
from monitor.models import Stock
from monitor.settings import Settings
from monitor.sellers import restrict_oechsle, oechsle_seller
from tiendas.vtex import VtexRetailer


class Oechsle(VtexRetailer):
    base_url = "https://www.oechsle.pe"
    retailer = "Oechsle"

    def __init__(self, settings=None, client=None):
        super().__init__(settings or Settings().for_retailer("oechsle"), client)

    def params(self):
        # Validado en la API pública: limita productos al vendedor Oechsle (1).
        return dict(super().params(), fq="sellerId:1")

    def normalize(self, raw):
        return restrict_oechsle(super().normalize(raw))

    def stock(self, product):
        if oechsle_seller(product) != "oechsle":
            return Stock(
                detail="Vendedor Oechsle no confirmado; otros vendedores excluidos"
            )
        raw = deepcopy(product.raw)
        items = raw.get("items")
        if not isinstance(items, list):
            return Stock(detail="Sin variantes interpretables")
        for item in items:
            if not isinstance(item, dict):
                continue
            sellers = item.get("sellers")
            if not isinstance(sellers, list):
                continue
            own = []
            for seller in sellers:
                if not isinstance(seller, dict):
                    continue
                if str(seller.get("sellerId")) != "1":
                    continue
                name = seller.get("sellerName")
                if name is not None and (
                    not isinstance(name, str) or name.strip().casefold() != "oechsle"
                ):
                    # Identidad contradictoria: no convertirla en stock cero.
                    own.append({})
                else:
                    own.append(seller)
            item["sellers"] = own
        return super().stock(replace(product, raw=raw))
