
from urllib.parse import urlparse

import requests


def consultar_producto(url):
    """
    Consulta un producto en la API pública VTEX.
    No confunde errores de consulta con falta de stock.
    """

    resultado = {
        "url": url,
        "nombre": None,
        "precio": None,
        "estado": "DESCONOCIDO",
        "detalle": "",
    }

    try:
        ruta = urlparse(url).path.rstrip("/")

        if not ruta.endswith("/p"):
            resultado["detalle"] = "La URL no es de un producto."
            return resultado

        api_url = (
            "https://www.plazavea.com.pe"
            "/api/catalog_system/pub/products/search"
            + ruta
        )

        respuesta = requests.get(
            api_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
            timeout=15,
        )

        respuesta.raise_for_status()

        productos = respuesta.json()

        if not isinstance(productos, list) or not productos:
            resultado["detalle"] = "Producto no encontrado en la API."
            return resultado

        producto = productos[0]

        resultado["nombre"] = producto.get("productName")

        items = producto.get("items") or []

        if not items:
            resultado["detalle"] = "No hay variantes del producto."
            return resultado

        ofertas_disponibles = []
        ofertas_con_stock_cero = 0

        for item in items:
            for seller in item.get("sellers", []):
                oferta = seller.get("commertialOffer") or {}

                disponible = oferta.get("IsAvailable")
                cantidad = oferta.get("AvailableQuantity")

                if disponible is True and cantidad is not None and cantidad > 0:
                    ofertas_disponibles.append(oferta)

                elif disponible is False or cantidad == 0:
                    ofertas_con_stock_cero += 1

        if ofertas_disponibles:
            resultado["estado"] = "DISPONIBLE"

            precios = [
                oferta["Price"]
                for oferta in ofertas_disponibles
                if isinstance(oferta.get("Price"), (int, float))
            ]

            if precios:
                resultado["precio"] = min(precios)

            resultado["detalle"] = "Oferta con stock detectada."

        elif ofertas_con_stock_cero:
            resultado["estado"] = "AGOTADO"
            resultado["detalle"] = "No se encontró stock en las ofertas consultadas."

        else:
            resultado["detalle"] = "La API no proporcionó datos concluyentes de stock."

    except requests.RequestException as error:
        resultado["detalle"] = f"Error HTTP o de conexión: {error}"

    except ValueError:
        resultado["detalle"] = "La respuesta no contiene JSON válido."

    except (TypeError, KeyError, AttributeError) as error:
        resultado["detalle"] = f"Datos inesperados: {error}"

    return resultado
