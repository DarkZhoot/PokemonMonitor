import time
from requests.exceptions import RequestException

import re
import unicodedata
from urllib.parse import urljoin

import requests

from tiendas.plaza_vea import consultar_producto


BASE_URL = "https://www.plazavea.com.pe"

API_BUSQUEDA = (
    BASE_URL + "/api/catalog_system/pub/products/search"
)

BUSQUEDAS = [
    "pokemon tcg 30",
    "pokemon 30 aniversario",
    "pokemon 30th anniversary",
    "pokemon tcg ingles",
]


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(
        caracter for caracter in texto
        if not unicodedata.combining(caracter)
    )




def clasificar_producto(nombre):
    texto = normalizar(nombre)

    # Debe mencionar Pokémon o TCG.
    es_pokemon = bool(
        re.search(r"\bpokemon\b", texto)
    )

    es_tcg = bool(
        re.search(r"\btcg\b", texto)
    )

    if not es_pokemon and not es_tcg:
        return "DESCARTADO"

    # Detectar el 30 aniversario en distintos formatos.
    # Incluye: 30.º, 30.o, 30º, 30, 30th.
    es_aniversario = bool(
        re.search(
            r"\b30\s*[.º°o]*\s*"
            r"(?:aniversario|anniversary)\b"
            r"|\b30th\b",
            texto
        )
    )

    if not es_aniversario:
        return "DESCARTADO"

    # Excluir idiomas distintos del inglés.
    idiomas_excluidos = (
        "espanol", "spanish",
        "japones", "japanese",
        "frances", "french",
        "coreano", "korean",
        "aleman", "german",
        "italiano", "italian"
    )

    if any(
        re.search(rf"\b{idioma}\b", texto)
        for idioma in idiomas_excluidos
    ):
        return "DESCARTADO"

    # El idioma debe estar explícitamente indicado.
    if re.search(r"\b(ingles|english)\b", texto):
        if es_tcg:
            return "CANDIDATO"

        return "POR_VERIFICAR"

    return "POR_VERIFICAR"




def consultar_pagina(sesion, url, parametros, max_intentos=3):
    """
    Consulta una página y reintenta ante errores temporales.
    """

    for intento in range(1, max_intentos + 1):
        try:
            respuesta = sesion.get(
                url,
                params=parametros,
                timeout=20
            )

            if respuesta.status_code in (429, 500, 502, 503, 504):
                print(
                    f"  Error temporal HTTP {respuesta.status_code}. "
                    f"Intento {intento}/{max_intentos}"
                )

                if intento < max_intentos:
                    espera = min(2 ** intento, 10)
                    time.sleep(espera)
                    continue

                respuesta.raise_for_status()

            respuesta.raise_for_status()
            return respuesta

        except RequestException as error:
            print(f"  Error en intento {intento}: {error}")

            if intento == max_intentos:
                raise

            time.sleep(min(2 ** intento, 10))


def buscar_productos():
    encontrados = {}
    errores = []

    TAMANO_PAGINA = 20
    MAX_PAGINAS = 15

    with requests.Session() as sesion:
        sesion.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })

        for termino in BUSQUEDAS:
            print(f"\nBuscando: {termino}")

            ids_vistos = set()

            for pagina in range(MAX_PAGINAS):
                inicio = pagina * TAMANO_PAGINA
                fin = inicio + TAMANO_PAGINA - 1

                url_api = API_BUSQUEDA + "/" + termino

                try:
                    respuesta = consultar_pagina(
                        sesion,
                        url_api,
                        {
                            "_from": inicio,
                            "_to": fin,
                        }
                    )

                    print(
                        f"  Página {pagina + 1} "
                        f"({inicio}-{fin}) "
                        f"HTTP {respuesta.status_code}"
                    )

                    if respuesta.status_code == 416:
                        print("  Fin de resultados.")
                        break

                    respuesta.raise_for_status()

                    productos = respuesta.json()

                    if not isinstance(productos, list):
                        raise ValueError(
                            "La API no devolvió una lista."
                        )

                    if not productos:
                        print("  No hay más productos.")
                        break

                    nuevos_en_pagina = 0

                    for producto in productos:
                        if not isinstance(producto, dict):
                            continue

                        identificador = str(
                            producto.get("productId") or ""
                        )

                        if not identificador:
                            continue

                        if identificador in ids_vistos:
                            continue

                        ids_vistos.add(identificador)
                        nuevos_en_pagina += 1

                        nombre = producto.get("productName") or ""
                        enlace = producto.get("link") or ""

                        if not enlace:
                            link_text = producto.get("linkText")
                            if link_text:
                                enlace = f"/{link_text}/p"

                        if not enlace:
                            continue

                        clasificacion = clasificar_producto(nombre)

                        if clasificacion == "DESCARTADO":
                            continue

                        encontrados[identificador] = {
                            "id": identificador,
                            "nombre": nombre,
                            "url": urljoin(BASE_URL, enlace),
                            "clasificacion": clasificacion,
                        }

                    print(
                        f"  Productos únicos en página: "
                        f"{nuevos_en_pagina}"
                    )

                    if nuevos_en_pagina == 0:
                        print("  Resultados repetidos. Deteniendo búsqueda.")
                        break

                    if len(productos) < TAMANO_PAGINA:
                        print("  Última página detectada.")
                        break

                    if pagina == MAX_PAGINAS - 1:
                        print("  Límite de páginas alcanzado.")

                except (requests.RequestException, ValueError) as error:
                    errores.append((termino, str(error)))
                    print(f"  ERROR: {error}")
                    break

    return list(encontrados.values()), errores




def main():
    print("=" * 55)
    print("PROYECTO POKEMON - DESCUBRIMIENTO")
    print("=" * 55)

    productos, errores = buscar_productos()

    candidatos = 0

    for producto in productos:
        print("\n" + "-" * 55)
        print(f"Nombre: {producto['nombre']}")
        print(f"ID: {producto['id']}")
        print(f"Clasificación: {producto['clasificacion']}")
        print(f"URL: {producto['url']}")

        if producto["clasificacion"] == "CANDIDATO":
            candidatos += 1

            resultado = consultar_producto(producto["url"])

            print(f"Stock API: {resultado['estado']}")
            print(f"Detalle: {resultado['detalle']}")
        else:
            print("Stock: pendiente de verificar idioma.")

    print("\n" + "=" * 55)
    print(f"Productos relevantes o por verificar: {len(productos)}")
    print(f"Candidatos en inglés: {candidatos}")
    print(f"Búsquedas con error: {len(errores)}")
    print("=" * 55)

    print("\nPrueba terminada. No se modificó el JSON.")


if __name__ == "__main__":
    main()
