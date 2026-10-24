# PokemonMonitor

Monitor de **Pokémon TCG, 30.º aniversario, inglés**, inicialmente para Plaza Vea (Perú). Consulta solo el catálogo público y envía avisos por Telegram con enlaces para compra manual. No inicia sesión en cuentas de clientes, no añade al carrito y no compra ni paga.

## Estado de la integración

La implementación está probada con respuestas simuladas y consultas reales de catálogo. Tras el bloqueo inicial CONNECT 403, el acceso autorizado empezó a responder: una búsqueda devolvió 159 publicaciones y se comprobaron detalles por `fq=productId:...` y por `/slug/p`. Se observó una oferta del sobre 30.º aniversario con `IsAvailable=false` y cantidad cero; esto no certifica la entrega para una ubicación. La API consultada omitió `CurrencyCode`. Quedan pendientes la disponibilidad logística real, la exhaustividad del catálogo y el envío real de Telegram con tus credenciales. No hay evasión de bloqueos, CAPTCHA ni cambios para desactivar TLS.

Por seguridad, `PLAZAVEA_CONTEXT_CONFIRMED=false` mantiene stock `DESCONOCIDO`. Antes de habilitar alertas de disponibilidad, sigue la verificación de integración al final. Establecerlo en `true` declara que has comprobado que las ofertas del catálogo en el canal/vendedor/variante elegidos representan tu contexto de compra. **No realiza por sí mismo una consulta de entrega.** Si necesitas disponibilidad por código postal, déjalo sin confirmar: esa integración no está implementada. Configurar `PLAZAVEA_POSTAL_CODE` mantiene estado desconocido para evitar inferencias falsas.

## Instalación

Python **3.12 recomendado** (mínimo 3.10). Desde el directorio del repositorio:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edita `.env` localmente; nunca lo subas. Las variables ya exportadas prevalecen sobre `.env`. No pegues tokens en comandos, capturas, issues ni logs. En Linux limita el acceso con `chmod 600 .env`.

## Seguimiento rápido con descubrimiento manual

`--once` y `--loop` **ya no hacen búsquedas generales**. Solo actualizan detalles y stock de los candidatos guardados, además de productos configurados explícitamente. Los cientos de publicaciones pendientes se conservan pero no se consultan en cada ciclo.

```powershell
# Solo cuando quieras descubrir productos nuevos (primera vez si no hay candidatos)
.\.venv\Scripts\python.exe main.py --discover
# Ver nombres y enlaces directos guardados; no usa Internet
.\.venv\Scripts\python.exe main.py --list-products
# Seguir solo los objetivos guardados
.\.venv\Scripts\python.exe main.py --loop
```

Detén el bucle con Ctrl+C antes de ejecutar `--discover`, y reinícialo después. El bloqueo impide ejecutar ambos simultáneamente. La búsqueda manual utiliza los términos configurados, guarda candidatos y pendientes, y puede enviar alertas de candidatos nuevos disponibles. Reutiliza los detalles completos de búsqueda para evitar otra consulta por cada resultado. Los pendientes solo se vuelven a evaluar si reaparecen en un descubrimiento o están configurados explícitamente. No borres la base existente: sus candidatos y transiciones se reutilizan sin reenviar avisos por disponibilidad continua.

Para revisar aproximadamente cada dos minutos **después de terminar el ciclo**, usa `MONITOR_INTERVAL=120`. Se mantiene la pausa entre peticiones y la gestión de límites; no garantiza un tiempo fijo ni ausencia de bloqueos. `product_checked.duration_seconds` mide el procesamiento del producto (incluye red, pausas, persistencia y un eventual envío); `cycle_finished.duration_seconds` mide el ciclo completo. No son tiempos exclusivos de la respuesta HTTP.

En Linux usa los mismos argumentos con `.venv/bin/python`. Los ejemplos de cron y Programador de tareas ejecutan ahora solamente seguimiento. Para detectar nuevas publicaciones debes lanzar expresamente `--discover`.

## Configuración

| Variable | Uso / valor predeterminado |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token secreto de BotFather |
| `TELEGRAM_CHAT_ID` | Identificador del chat que recibirá avisos |
| `MONITOR_DATA_DIR` | `runtime`, relativo al repositorio; mismo directorio para todas las ejecuciones |
| `MONITOR_INTERVAL` | 600 segundos, mínimo 60; solo para `--loop` |
| `MONITOR_TIMEOUT` | 20 segundos por petición |
| `MONITOR_REQUEST_DELAY` | 1 segundo mínimo entre peticiones; configurable desde 0.5 |
| `MONITOR_RETRIES` | 2 reintentos adicionales de red/5xx; máximo 5 |
| `TELEGRAM_RETRIES` | 3 intentos totales por evento |
| `SEARCH_MAX_PAGES` | 15 páginas de 20 resultados por término |
| `SEARCH_TERMS` | Términos separados por `\|` (ver `.env.example`) |
| `PRODUCT_EXCLUSIONS` | Términos excluidos separados por `\|`; se comparan sin acentos |
| `MONITOR_PRODUCTS_JSON` | Lista JSON de productos adicionales con `url`, opcionalmente `id` y `nombre` |
| `PLAZAVEA_CONTEXT_CONFIRMED` | `false`; ver limitaciones de disponibilidad |
| `PLAZAVEA_SALES_CHANNEL` | Canal VTEX opcional (`sc`), sin inventar un valor |
| `PLAZAVEA_SELLER_ID` | Filtra ofertas al vendedor configurado |
| `PLAZAVEA_SKU_ID` | Filtra ofertas a la variante configurada |
| `PLAZAVEA_POSTAL_CODE` | Reservado: su presencia fuerza desconocido hasta implementar logística |

Ejemplo de productos específicos en `.env`:

```dotenv
MONITOR_PRODUCTS_JSON='[{"id":"ID_REAL_DEL_CATALOGO","nombre":"Nombre orientativo","url":"https://www.plazavea.com.pe/slug-real/p"}]'
```

`config.py` conserva el producto manual original. Ni sus etiquetas antiguas ni las del archivo histórico se consideran prueba del idioma: los detalles actuales deben confirmar los criterios. Los identificadores antiguos que eran SKU se resuelven por URL exacta al ID del catálogo.

## Crear el bot y configurar Telegram

1. Abre **@BotFather** en Telegram y ejecuta `/newbot`.
2. Elige nombre y usuario terminado en `bot`. Copia el token únicamente al archivo `.env`, en `TELEGRAM_BOT_TOKEN`.
3. Abre tu nuevo bot y pulsa **Iniciar** o envía `/start`. El bot no puede iniciar la conversación contigo.
4. Obtén el chat ID ejecutando este fragmento local desde el repositorio. Solo imprime IDs, nunca el token ni la respuesta completa:

```bash
.venv/bin/python - <<'PY'
import requests
from monitor.settings import Settings
s = Settings.load()
if not s.token:
    raise SystemExit('Configura TELEGRAM_BOT_TOKEN en .env')
try:
    r = requests.get(f'https://api.telegram.org/bot{s.token}/getUpdates', timeout=s.timeout)
    data = r.json()
    if not r.ok or not data.get('ok'):
        raise SystemExit('Telegram rechazó getUpdates; revisa el token y la conexión')
    ids = {u['message']['chat']['id'] for u in data.get('result', []) if 'message' in u}
    print('Chat IDs:', sorted(ids))
except (requests.RequestException, ValueError):
    raise SystemExit('No se pudo consultar Telegram; no se muestran credenciales')
PY
```

En Windows ejecuta el mismo código con `.venv\Scripts\python.exe` en un intérprete Python. Si no hay IDs, vuelve a enviar `/start`. Un webhook activo u otro consumidor de `getUpdates` puede impedir recuperar actualizaciones: utiliza un bot dedicado, sin otro consumidor. Para grupos, añade el bot y envía un comando dirigido a él; configura el ID negativo del grupo.

5. Guarda el ID en `TELEGRAM_CHAT_ID`.
6. Ejecuta la prueba explícita:

```bash
.venv/bin/python main.py --telegram-test
```

Debe llegar **PRUEBA — SIN COMPRA**, con fecha de Lima y botón para abrir Plaza Vea. El mensaje no afirma que haya stock. El comando devuelve 0 solo cuando Telegram confirma aceptación; revisa también la recepción en tu dispositivo. No requiere que funcione Plaza Vea. Si falla, consulta `runtime/monitor.jsonl`: no contiene el token. `401/404`: token; `400`: chat o formato; `403`: inicia/desbloquea el bot o revisa permisos; `429`: respeta la espera indicada. Si el token se expone, revócalo con BotFather.

## Ejecución

```bash
# Un solo ciclo, apropiado para cron; también es el modo predeterminado
.venv/bin/python main.py --once
# Proceso continuo; consultas cada MONITOR_INTERVAL y reintentos Telegram entre ciclos
.venv/bin/python main.py --loop
# Tras corregir la causa de un fallo permanente o intentos agotados:
.venv/bin/python main.py --retry-failed
.venv/bin/python main.py --once
```

En Windows sustituye `.venv/bin/python` por `.venv\Scripts\python.exe`.

Salidas: **0** ciclo sin incidencias detectadas; **1** errores, stock desconocido, envío fallido o configuración incompleta; **2** otra ejecución mantiene el bloqueo. Un resultado parcial puede haber actualizado productos y enviado algunos avisos; revisa el log. Los candidatos pendientes explícitamente consultados también degradan el estado del ciclo. `--loop` continúa ante fallos de consultas/envíos, con intervalo; errores de configuración o historial impiden el inicio y deben corregirse.

Si cambias el contexto de vendedor, variante o entrega, utiliza un directorio de datos independiente: los estados de contextos distintos no deben compararse como una reposición.

No ejecutes `--loop` y cron simultáneamente. Usa siempre el mismo `MONITOR_DATA_DIR` para compartir el bloqueo. El bloqueo del sistema operativo se libera tras una caída; no hace falta borrar el archivo `.lock`.

### Linux cron

En una máquina encendida permanentemente, con rutas absolutas:

```cron
*/10 * * * * cd /ruta/PokemonMonitor && /ruta/PokemonMonitor/.venv/bin/python main.py --once >/dev/null 2>&1
```

Los eventos y errores quedan en `runtime/monitor.jsonl`. Configura supervisión externa de códigos de salida/logs para detectar que cron deja de ejecutarse. No hay notificación de salud por un canal independiente si Telegram falla.

### Linux con systemd (alternativa a cron)

Unidad de ejemplo `/etc/systemd/system/pokemonmonitor.service`; ajusta usuario y rutas antes de instalarla:

```ini
[Unit]
Description=PokemonMonitor
Wants=network-online.target
After=network-online.target

[Service]
User=monitor
WorkingDirectory=/ruta/PokemonMonitor
ExecStart=/ruta/PokemonMonitor/.venv/bin/python /ruta/PokemonMonitor/main.py --loop
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Activa con `sudo systemctl daemon-reload` y `sudo systemctl enable --now pokemonmonitor`. El usuario debe poder leer `.env` y escribir `runtime`. No se instala este servicio automáticamente. Un portátil suspendido y una tarea de Codex cerrada no constituyen un servidor permanente.

### Programador de tareas de Windows

Crea una tarea con desencadenador diario, repetición **cada 10 minutos**, duración indefinida. Programa: `C:\ruta\PokemonMonitor\.venv\Scripts\python.exe`. Argumentos: `C:\ruta\PokemonMonitor\main.py --once`. Iniciar en: `C:\ruta\PokemonMonitor`. Activa «Ejecutar lo antes posible si se omitió el inicio» y «No iniciar una instancia nueva» si ya está ejecutándose. Selecciona una cuenta con permisos y una política de energía que mantenga la máquina disponible. No hace falta ejecutar como administrador.

## Persistencia, eventos y recuperación

`runtime/monitor.sqlite3` almacena productos, el último intento, el último estado concluyente y una cola de notificaciones. La actualización de estado y la creación del evento comparten una transacción. SQLite WAL y el bloqueo evitan escrituras competidoras. En despliegues distintos no compartas SQLite por una unidad de red; usa una única instancia.

En el primer inicio se importan `datos/productos.json` y `datos/eventos_alertas.jsonl` de forma idempotente. **Los originales, el flag y los logs antiguos no se modifican.** Los eventos históricos y antiguos pendientes se archivan como `historical`, sin reenviarse automáticamente para evitar avisos obsoletos. El formato JSONL antiguo deja de recibir eventos nuevos: los nuevos se guardan en SQLite. Un historial corrupto impide migrar en lugar de descartarlo silenciosamente; corrígelo sobre una copia y conserva el original.

Tipos de alerta:

- `PRODUCTO_NUEVO`: primera observación de un candidato con disponibilidad confirmada, incluido el primer arranque sin historial.
- `REPOSICION`: último estado concluyente agotado y ahora disponible, aunque haya errores intermedios.
- `DISPONIBILIDAD_CONFIRMADA`: producto registrado sin estado concluyente previo y ahora disponible.

Una pérdida temporal de conectividad no borra el último estado concluyente ni genera por sí sola otro aviso al recuperar una disponibilidad ya conocida. Los candidatos nuevos con stock desconocido y los pendientes de idioma/edición se conservan para futuras revisiones. Cambiar palabras clave solo afecta al próximo `--discover` y no borra el histórico. Para retirar definitivamente un producto se necesita una futura opción de exclusión por ID o gestión de bajas; por ahora las exclusiones de texto impiden sus alertas al revalidar detalles.

Las alertas se intentan enviar al detectar el evento, sin esperar a terminar de revisar todos los productos. La cola registra `pending`, `sent`, `failed`, número de intentos, próxima fecha y error redactado. `--loop` procesa reintentos aproximadamente cada 5 segundos; en cron, solo en las próximas ejecuciones. Los 429 respetan `retry_after`; fallos permanentes o intentos agotados requieren `--retry-failed`. No se consumen intentos si faltan credenciales. Una espera impuesta por Telegram se comparte entre todos los eventos para no saltarse el límite enviando otro mensaje. Un evento pendiente puede enviarse después de que cambie el stock: el mensaje muestra la **fecha de detección**, no una garantía de disponibilidad actual.

Telegram no proporciona una clave de idempotencia para `sendMessage`: si acepta el mensaje y se pierde la respuesta, o el proceso cae antes de guardar la confirmación, un reintento **puede duplicarlo**. Los avisos incluyen un ID estable para reconocerlo. No se promete entrega exactamente una vez.

Consulta local del estado (sin tokens):

```bash
.venv/bin/python - <<'PY'
import sqlite3
from monitor.settings import Settings
s = Settings.load()
with sqlite3.connect(s.data_dir / 'monitor.sqlite3') as db:
    print('Productos:', db.execute('SELECT key,state,last_known,checked FROM products').fetchall())
    print('Eventos:', db.execute('SELECT id,status,attempts,error FROM events').fetchall())
PY
```

Haz copias con la API `sqlite3.Connection.backup()` o deteniendo el monitor antes de copiar la base; no copies solo el archivo principal mientras WAL está activo. Los logs rotan a 5 MB con tres copias. La base no tiene purga automática: conserva el historial y vigila espacio disponible.

## Pruebas automatizadas

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

Las pruebas usan respuestas simuladas y almacenamiento temporal/en memoria, no envían Telegram ni consultan la tienda. Cubren clasificación, metadatos, variantes, stock parcial, identidades, paginación/416, 403/429/5xx, transiciones, importación, transacciones, bloqueo y cola de envío. Windows y systemd requieren validación en sus plataformas; la ejecución actual es Linux.

## Verificar Plaza Vea desde una conexión autorizada

1. Instala las dependencias y deja `PLAZAVEA_CONTEXT_CONFIRMED=false`.
2. Desde una conexión que tenga acceso legítimo al dominio, ejecuta desde el repositorio:

```bash
.venv/bin/python - <<'PY'
from monitor.settings import Settings
from tiendas.plaza_vea import PlazaVea
s = Settings.load()
a = PlazaVea(s)
r = a.search()
print('Completa:', r.complete, 'Errores:', r.errors, 'Productos:', len(r.products))
for candidate in r.products[:5]:
    p = a.details(candidate)
    print(p.id, p.name, p.url, p.classification)
    for item in p.raw.get('items', []):
        for seller in item.get('sellers', []):
            offer = seller.get('commertialOffer') or {}
            print('SKU/vendedor:', item.get('itemId'), seller.get('sellerId'),
                  'Disponible:', offer.get('IsAvailable'), 'Cantidad:', offer.get('AvailableQuantity'),
                  'Precio:', offer.get('Price'), 'Moneda:', offer.get('CurrencyCode'))
PY
```

3. Contrasta nombres, IDs, URL, idioma y edición con las fichas reales, incluyendo un caso agotado y otro disponible cuando existan. Verifica la búsqueda por ID `fq=productId:...`, resolución por slug, canal `sc`, paginación, respuestas vacías y límites del catálogo. Un 403/429 no es stock cero: detén las verificaciones y respeta los límites; no cambies de proxy para eludirlos.
4. Comprueba manualmente la modalidad, tienda y ubicación de entrega deseadas en la web, sin comprar. Si el catálogo no representa ese contexto, **no actives** `PLAZAVEA_CONTEXT_CONFIRMED`: se necesita una integración logística documentada y pruebas adicionales. No se ha implementado ni inventado una simulación de carrito para resolverlo.
5. Solo si esa comprobación permite usar las ofertas públicas como señal válida, configura canal/vendedor/SKU si procede y `PLAZAVEA_CONTEXT_CONFIRMED=true`. Ejecuta `--once` y revisa el estado guardado. La moneda no se supone PEN: solo se muestra si viene explícita en la oferta seleccionada.
6. Ejecuta primero `--telegram-test` para aislar la entrega. Para una prueba funcional completa, espera una transición real o utiliza un **directorio temporal independiente** con respuestas simuladas; no alteres el historial de producción ni inventes una reposición para probar el envío.

El clasificador usa títulos, descripciones, especificaciones y variantes; sigue siendo heurístico. Información ambigua queda pendiente, idiomas excluidos prevalecen y variantes mezcladas se descartan conservadoramente. Revisa exclusiones y muestras reales: una descripción de marketing puede mencionar varias colecciones. No se garantiza cobertura total del catálogo ni detección de reposiciones que ocurran y terminen entre revisiones.

## Arquitectura y revisión

`monitor/models.py` define `Retailer` (búsqueda, detalles, stock y normalización). `tiendas/plaza_vea.py` es el único adaptador implementado. `monitor/app.py` coordina el ciclo; `store.py` conserva la lógica de transiciones/pendientes del monitor original mediante transacciones, `telegram.py` entrega avisos y `http.py` controla red. `tiendas/descubridor.py` conserva las entradas básicas de búsqueda/clasificación originales.

Los cambios se preparan en `feature/telegram-stock-monitor`; no se fusionan ni se publican automáticamente. Revisa el diff y valida la integración autorizada antes de operar alertas de stock en producción. Agregar otro retailer requerirá su adaptador, configuración y pruebas; Falabella y Ripley no están implementados.
