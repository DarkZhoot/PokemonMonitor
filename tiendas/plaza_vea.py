"""Adaptador Plaza Vea y entradas compatibles con el monitor original."""

from monitor.models import Product, normalize_url
from monitor.settings import Settings
from monitor.http import RemoteError
from tiendas.vtex import VtexRetailer, classify, normalizar

BASE_URL = "https://www.plazavea.com.pe"
API = BASE_URL + "/api/catalog_system/pub/products/search"


class PlazaVea(VtexRetailer):
    base_url = BASE_URL
    retailer = "Plaza Vea"


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
