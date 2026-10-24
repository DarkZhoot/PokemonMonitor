from uuid import uuid4
import os
import json
import logging
from datetime import datetime
from pathlib import Path

from config import PRODUCTOS
from tiendas.plaza_vea import consultar_producto
from tiendas.descubridor import buscar_productos, BUSQUEDAS


BASE = Path(__file__).resolve().parent
ARCHIVO_ESTADO = BASE / "datos" / "productos.json"
ARCHIVO_LOG = BASE / "logs" / "monitor.log"
ARCHIVO_EVENTOS = BASE / "datos" / "eventos_alertas.jsonl"
MARCA_INICIAL = BASE / "datos" / "descubrimiento_iniciado.flag"

ESTADOS_VALIDOS = {"DISPONIBLE", "AGOTADO"}


def fecha_actual():
    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def configurar_logging():
    ARCHIVO_LOG.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                ARCHIVO_LOG,
                encoding="utf-8"
            ),
        ],
    )


def cargar_estado():
    if not ARCHIVO_ESTADO.exists():
        return {}

    with ARCHIVO_ESTADO.open("r", encoding="utf-8") as archivo:
        datos = json.load(archivo)

    if not isinstance(datos, dict):
        raise ValueError("Formato JSON incorrecto.")

    return datos


def guardar_estado(datos):
    ARCHIVO_ESTADO.parent.mkdir(parents=True, exist_ok=True)

    temporal = ARCHIVO_ESTADO.with_suffix(".tmp")

    with temporal.open("w", encoding="utf-8") as archivo:
        json.dump(
            datos,
            archivo,
            indent=4,
            ensure_ascii=False
        )

    temporal.replace(ARCHIVO_ESTADO)


def obtener_productos(estados):
    productos = {}

    # 1. Conservar todos los productos registrados.
    for url, datos in estados.items():
        if not isinstance(datos, dict):
            continue

        if datos.get("tienda") == "Plaza Vea":
            productos[url] = datos.copy()

    # 2. Incorporar los productos configurados manualmente.
    for producto in PRODUCTOS:
        if (
            producto["idioma"] == "ingles"
            and producto["edicion"] == "30_aniversario"
        ):
            productos[producto["url"]] = producto.copy()

    # 3. Buscar nuevos productos automáticamente.
    logging.info("Iniciando descubrimiento de productos...")

    encontrados, errores = buscar_productos()

    for termino, error in errores:
        logging.warning(
            "Búsqueda fallida (%s): %s",
            termino,
            error
        )

    busqueda_util = len(errores) < len(BUSQUEDAS)

    nuevos = 0

    for encontrado in encontrados:
        if encontrado["clasificacion"] != "CANDIDATO":
            continue

        url = encontrado["url"]

        if url not in productos:
            productos[url] = {
                "id": encontrado["id"],
                "tienda": "Plaza Vea",
                "nombre": encontrado["nombre"],
                "idioma": "ingles",
                "edicion": "30_aniversario",
                "url": url,
                "origen": "descubrimiento",
            }

        if url not in estados:
            nuevos += 1

    logging.info(
        "Productos nuevos respecto al JSON: %s",
        nuevos
    )

    return productos, busqueda_util



def procesar_producto(producto, estados, avisar_nuevos):
    url = producto["url"]
    nombre = producto["nombre"]

    logging.info("-----------------------------------")
    logging.info("Producto: %s", nombre)

    resultado = consultar_producto(url)
    estado_actual = resultado["estado"]

    logging.info("Estado: %s", estado_actual)
    logging.info("Detalle: %s", resultado["detalle"])

    if estado_actual not in ESTADOS_VALIDOS:
        logging.warning(
            "Consulta no concluyente. "
            "Se conserva el estado anterior."
        )
        return 0

    anterior = estados.get(url)
    momento = fecha_actual()
    tipo_alerta = None

    if anterior is None:
        logging.info("Registrando producto por primera vez.")

        if (
            avisar_nuevos
            and producto.get("origen") == "descubrimiento"
            and estado_actual == "DISPONIBLE"
        ):
            tipo_alerta = "PRODUCTO_NUEVO"

        estados[url] = {
            "id": producto.get("id"),
            "tienda": producto["tienda"],
            "nombre": nombre,
            "idioma": producto["idioma"],
            "edicion": producto["edicion"],
            "url": url,
            "origen": producto.get("origen", "manual"),
            "estado": estado_actual,
            "primera_consulta": momento,
            "ultima_consulta": momento,
            "avisos_pendientes": [],
        }

    else:
        anterior.setdefault("avisos_pendientes", [])
        estado_anterior = anterior.get("estado")


        if (
            estado_anterior == "AGOTADO"
            and estado_actual == "DISPONIBLE"
        ):
            tipo_alerta = "REPOSICION"

        elif estado_anterior != estado_actual:
            logging.info(
                "Cambio de estado: %s -> %s",
                estado_anterior,
                estado_actual
            )

        else:
            logging.info("Sin cambios de disponibilidad.")

        anterior["estado"] = estado_actual
        anterior["ultima_consulta"] = momento

    if tipo_alerta is None:
        return 0

    aviso = {
        "id": str(uuid4()),
        "tipo": tipo_alerta,
        "nombre": nombre,
        "tienda": producto["tienda"],
        "url": url,
        "fecha": momento,
    }

    estados[url].setdefault(
        "avisos_pendientes", []
    ).append(aviso)

    logging.info(
        "Alerta preparada: %s",
        tipo_alerta
    )

    return 1

def cargar_ids_registrados():
    ids = set()

    if not ARCHIVO_EVENTOS.exists():
        return ids

    with ARCHIVO_EVENTOS.open(
        "r", encoding="utf-8"
    ) as archivo:
        for numero, linea in enumerate(archivo, start=1):
            if not linea.strip():
                continue

            try:
                evento = json.loads(linea)
                identificador = evento["id"]

                if not isinstance(identificador, str):
                    raise ValueError("ID incorrecto")

                ids.add(identificador)

            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(
                    f"Registro de eventos dañado "
                    f"en línea {numero}: {error}"
                ) from error

    return ids


def registrar_avisos_pendientes(estados):
    ARCHIVO_EVENTOS.parent.mkdir(
        parents=True, exist_ok=True
    )

    ids_registrados = cargar_ids_registrados()
    avisos_registrados = 0

    for datos in estados.values():
        if not isinstance(datos, dict):
            continue

        pendientes = datos.get("avisos_pendientes", [])

        for aviso in pendientes:
            identificador = aviso["id"]

            # Evitar registrar nuevamente un evento conocido.
            if identificador in ids_registrados:
                logging.info(
                    "Alerta ya registrada: %s",
                    identificador
                )
                continue

            # Guardar permanentemente el evento.
            with ARCHIVO_EVENTOS.open(
                "a", encoding="utf-8"
            ) as archivo:
                archivo.write(
                    json.dumps(
                        aviso,
                        ensure_ascii=False
                    ) + "\n"
                )

                archivo.flush()
                os.fsync(archivo.fileno())

            ids_registrados.add(identificador)

            logging.warning(
                "ALERTA | %s | %s | %s | %s",
                aviso["tipo"],
                aviso["tienda"],
                aviso["nombre"],
                aviso["url"],
            )

            avisos_registrados += 1

        # Se confirmará al guardar productos.json.
        datos["avisos_pendientes"] = []

    return avisos_registrados




def main():
    configurar_logging()
    logging.info("========== POKEMON MONITOR ==========")

    try:
        estados = cargar_estado()
    except (OSError, ValueError) as error:
        logging.error("No se pudo cargar JSON: %s", error)
        return

    inicializado = MARCA_INICIAL.exists()

    productos, busqueda_util = obtener_productos(estados)

    avisos_nuevos = 0

    for producto in productos.values():
        try:
            avisos_nuevos += procesar_producto(
                producto,
                estados,
                avisar_nuevos=inicializado and busqueda_util
            )
        except Exception:
            logging.exception(
                "Error procesando producto: %s",
                producto.get("url", "Desconocido")
            )

    # PASO 1: Guardar productos y alertas pendientes.
    try:
        guardar_estado(estados)
    except OSError as error:
        logging.error(
            "No se pudo guardar el estado. "
            "No se registrarán alertas: %s",
            error
        )
        raise

    # PASO 2: Registrar los avisos pendientes.

    try:
        avisos_registrados = registrar_avisos_pendientes(
            estados
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        logging.error(
            "No se pudieron registrar las alertas: %s",
            error
        )
        raise

    # PASO 3: Guardar el estado actualizado.
    try:
        guardar_estado(estados)

        if busqueda_util and not inicializado:
            MARCA_INICIAL.write_text(
                fecha_actual(),
                encoding="utf-8"
            )

    except OSError as error:
        logging.error(
            "Error guardando confirmación de avisos: %s",
            error
        )
        return

    logging.info(
        "Productos monitoreados: %s",
        len(estados)
    )
    logging.info(
        "Alertas nuevas detectadas: %s",
        avisos_nuevos
    )
    logging.info(
        "Alertas registradas: %s",
        avisos_registrados
    )
    logging.info("========== FIN ==========")


if __name__ == "__main__":
    main()
