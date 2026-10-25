"""Adaptador de catálogo público VTEX de Oechsle; sin compras ni carrito."""

from monitor.settings import Settings
from tiendas.vtex import VtexRetailer


class Oechsle(VtexRetailer):
    base_url = "https://www.oechsle.pe"
    retailer = "Oechsle"

    def __init__(self, settings=None, client=None):
        super().__init__(settings or Settings().for_retailer("oechsle"), client)
