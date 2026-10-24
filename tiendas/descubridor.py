"""Entradas compatibles para el descubridor original."""

from monitor.settings import Settings
from tiendas.plaza_vea import PlazaVea, classify, normalizar

BUSQUEDAS = Settings().terms


def clasificar_producto(nombre):
    return classify({"productName": nombre}, Settings().exclusions)


def buscar_productos():
    r = PlazaVea(Settings.load()).search()
    return [
        dict(id=p.id, nombre=p.name, url=p.url, clasificacion=p.classification)
        for p in r.products
    ], [("búsqueda", e) for e in r.errors]


if __name__ == "__main__":
    print(buscar_productos())
