from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit, unquote

DISPONIBLE = "DISPONIBLE"
AGOTADO = "AGOTADO"
DESCONOCIDO = "DESCONOCIDO"


RETAILERS = {
    "Plaza Vea": ("plazavea", "www.plazavea.com.pe", "plazavea.com.pe"),
    "Oechsle": ("oechsle", "www.oechsle.pe", "oechsle.pe"),
}


def normalize_url(url, retailer="Plaza Vea"):
    if retailer not in RETAILERS:
        raise ValueError("Retailer desconocido")
    _, host, bare_host = RETAILERS[retailer]
    p = urlsplit(url)
    if (
        p.scheme not in ("http", "https")
        or p.hostname not in (host, bare_host)
        or p.username
        or p.password
        or p.port not in (None, 80, 443)
    ):
        raise ValueError("URL fuera del retailer seleccionado")
    path = unquote(p.path).rstrip("/")
    if not path.endswith("/p"):
        raise ValueError("URL no es de producto")
    return urlunsplit(("https", host, path, "", ""))


@dataclass
class Product:
    id: str
    name: str
    url: str
    classification: str = "POR_VERIFICAR"
    retailer: str = "Plaza Vea"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def key(self):
        return RETAILERS[self.retailer][0] + ":" + self.id


@dataclass
class Stock:
    state: str = DESCONOCIDO
    price: float | None = None
    currency: str | None = None
    detail: str = ""


@dataclass
class SearchResult:
    products: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    complete: bool = True


class Retailer(Protocol):
    def search(self) -> SearchResult: ...
    def details(self, product: Product) -> Product: ...
    def stock(self, product: Product) -> Stock: ...
    def normalize(self, raw: dict) -> Product: ...
