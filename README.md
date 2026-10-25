# PokemonMonitor

Monitor de **Pokémon TCG, 30.º aniversario, inglés**, para Plaza Vea, Oechsle, Ripley, Ilahui, Tai Loy y Falabella (Perú). Consulta solo el catálogo público y envía avisos por Telegram con enlaces para compra manual. No inicia sesión en cuentas de clientes, no añade al carrito y no compra ni paga.

## Estado de la integración

La implementación está probada con respuestas simuladas y consultas reales de catálogo. Tras el bloqueo inicial CONNECT 403, el acceso autorizado empezó a responder: una búsqueda devolvió 159 publicaciones y se comprobaron detalles por `fq=productId:...` y por `/slug/p`. Se observó una oferta del sobre 30.º aniversario con `IsAvailable=false` y cantidad cero; esto no certifica la entrega para una ubicación. La API consultada omitió `CurrencyCode`. Quedan pendientes la disponibilidad logística real, la exhaustividad del catálogo y el envío real de Telegram con tus credenciales. No hay evasión de bloqueos, CAPTCHA ni cambios para desactivar TLS.

Por seguridad, `PLAZAVEA_CONTEXT_CONFIRMED=false` mantiene stock `DESCONOCIDO`. Antes de habilitar alertas de disponibilidad, sigue la verificación de integración al final. Establecerlo en `true` declara que has comprobado que las ofertas del catálogo en el canal/vendedor/variante elegidos representan tu contexto de compra. **No realiza por sí mismo una consulta de entrega.** Si necesitas disponibilidad por código postal, déjalo sin confirmar: esa integración no está implementada. Configurar `PLAZAVEA_POSTAL_CODE` mantiene estado desconocido para evitar inferencias falsas.

## Oechsle y varias tiendas

El monitor incluye adaptadores para Plaza Vea y Oechsle, con el mismo objetivo: Pokémon TCG del 30.º aniversario en inglés. Comparte criterios, cola de Telegram y pausa entre ciclos; conserva **productos, estados y fechas de descubrimiento independientes por tienda**. Un mismo ID en dos tiendas no comparte historial. Un fallo de una tienda no evita procesar la otra, aunque las consultas son secuenciales y un timeout puede alargar el ciclo.

**Validación real de catálogo:** tras el bloqueo inicial CONNECT 403 del proxy, Oechsle respondió a las consultas autorizadas. Las cuatro búsquedas configuradas completaron su paginación y devolvieron 40 publicaciones, sin candidatos que confirmaran simultáneamente Pokémon TCG + 30.º aniversario + inglés. Se verificaron los detalles por ID del producto `3207764` (Booster Bundle Chaos Rising Pokémon Tcg Inglés), sus variantes y una oferta positiva con precio `149.9`, sin moneda explícita. Esa muestra es de otra expansión: no generó una alerta. Estas observaciones no prueban cobertura total ni disponibilidad por dirección, y pueden cambiar. Se añadió `www.oechsle.pe` al borrador de red; guardarlo no equivale a publicar el entorno. No se eluden bloqueos ni se desactiva TLS.

Agrega estas líneas a tu `.env` existente (no cambies token ni chat del grupo):

```dotenv
RETAILERS=plazavea,oechsle
OECHSLE_CONTEXT_CONFIRMED=true
OECHSLE_POSTAL_CODE=
OECHSLE_SALES_CHANNEL=
OECHSLE_SELLER_ID=
OECHSLE_SKU_ID=
```

`OECHSLE_CONTEXT_CONFIRMED=true` habilita exclusivamente la interpretación de ofertas propias de Oechsle: **no verifica entregabilidad por dirección**. Puede dejarse en `false` para conservar `DESCONOCIDO`. Los filtros de Oechsle son independientes de `PLAZAVEA_*`; no se copia el vendedor/canal/SKU de otra tienda. Deja los filtros adicionales vacíos; solo se consideran las ofertas propias de Oechsle. Se conserva `PLAZAVEA_CONTEXT_CONFIRMED` tal como lo tengas.

Después de detener el bucle con Ctrl+C, comprueba Oechsle de forma aislada:

```powershell
.\.venv\Scripts\python.exe main.py --retailer oechsle --discover
.\.venv\Scripts\python.exe main.py --retailer oechsle --list-products
```

Revisa los eventos `discovery_finished` y `search_incomplete` de Oechsle: un 403/429 es un error, no una búsqueda sin resultados ni stock agotado. Contrasta nombres, edición, idioma, enlaces y ofertas con las fichas públicas. Si hay candidatos disponibles, `--discover` puede enviar notificaciones reales al chat configurado. Una búsqueda completada sin candidatos no es un error; se vuelve a descubrir cuando corresponda. Si falla la integración real, conserva `RETAILERS=plazavea` para continuar solo con Plaza Vea y comparte los errores sin credenciales.

Para ambas tiendas:

```powershell
.\.venv\Scripts\python.exe main.py --loop
```

Sin `--retailer` se usan las tiendas configuradas en `RETAILERS` (por defecto las seis). `--retailer plazavea` u `--retailer oechsle` permite limitar cualquier ejecución (también `--once`, `--discover`, `--list-products`, `--loop`). No ejecutes dos procesos contra la misma base. Al incorporar Oechsle se hace su primer descubrimiento, sin reiniciar el reloj guardado de Plaza Vea. Plaza Vea se redescubre cada cuatro horas; Oechsle, Ripley, Ilahui, Tai Loy y Falabella cada dos minutos. Los descubrimientos vencidos fallidos reintentan tras quince minutos. Las alertas incluyen el nombre de la tienda y su enlace directo.

Para configurar un producto específico de Oechsle en `MONITOR_PRODUCTS_JSON`, incluye `"tienda":"Oechsle"`, además de su URL e ID opcional. Si omites `tienda`, se interpreta como Plaza Vea por compatibilidad. La clasificación sigue exigiendo prueba de edición e idioma; un enlace manual no los confirma automáticamente.

### Oechsle: exclusivamente vendido por Oechsle

La API usa `fq=sellerId:1` en las búsquedas. Además, cada producto debe declarar **`Vendido por: Oechsle`** para ser candidato. No basta con que incluya un vendedor `1`: las publicaciones de Plaza Vea pueden contener una oferta vacía de Oechsle como marcador, y eso no convierte el producto en venta propia. Se descartan Plaza Vea y marketplace dentro de Oechsle, y la propiedad desconocida queda pendiente sin alertas.

El stock/precio se calculan únicamente con ofertas del vendedor propio (`sellerId=1`, nombre Oechsle cuando se informa). Ofertas de terceros disponibles no pueden hacer que se marque disponible una oferta propia agotada. Datos contradictorios o incompletos siguen como desconocidos. No se modifica el adaptador de Plaza Vea ni el de Ripley.

Al actualizar se retiran del seguimiento los productos externos previamente guardados y se cancelan sus avisos pendientes/fallidos sin borrar los registros ni las alertas enviadas. Los eventos nuevos conservan la confirmación del alcance. Solo se reinicia una vez el reloj de Oechsle para hacer inmediatamente una búsqueda filtrada; sus búsquedas siguen cada dos minutos. No borres `runtime` ni cambies token/chat, y no necesitas configurar `OECHSLE_SELLER_ID`.

Validación real: las cuatro búsquedas con el nuevo filtro terminaron en 3.6 segundos, conservaron 4 publicaciones y todas declararon `Vendido por: Oechsle`; no hubo candidatos confirmados del aniversario e idioma objetivo en esa ejecución. Se verificó una oferta positiva propia del producto `3207764` y se rechazó el producto `1002012431`, vendido por Plaza Vea pese a tener el marcador vacío del vendedor `1`.

## Ripley

Se añadió un adaptador separado en `tiendas/ripley.py`: **Ripley no usa el contrato VTEX** de las otras tiendas. Lee el HTML público devuelto por el servidor, los productos de `__NEXT_DATA__.props.pageProps.findabilityProps.data` y sus enlaces reales. Para los detalles lee schema.org `Product`/`Offer`. No ejecuta JavaScript, no inicia sesión y no intenta resolver CAPTCHA ni cambiar de proxy para evadir restricciones.

**Solo productos vendidos por Ripley.** Cada búsqueda incluye `sort=relevance_desc`, `page` y `facets=Vendido por:Ripley`, conservando la faceta en todas las páginas. Además se verifica `seller == RIPLEY` en el listado y `offers.seller.name == Ripley` en las fichas. Marketplace se descarta; vendedor ausente o ambiguo queda pendiente y no genera alertas. Cuando una ficha incluye ofertas de varios vendedores, solo se interpreta stock/precio de Ripley.

Validación real del filtro proporcionado por el usuario: la consulta `pokemon TCG 30 ingles` devolvió 7 publicaciones con `seller: RIPLEY`. Se contrastó una ficha propia (`2032371408855P`), con vendedor schema.org `Ripley`; no se aceptó como candidato porque corresponde a otra expansión. Las cuatro búsquedas configuradas conservaron 6 publicaciones no descartadas por idioma, **sin candidatos confirmados de 30.º aniversario en inglés** en esta comprobación. Las 12 publicaciones disponibles detectadas por la versión anterior eran del alcance sin filtrar y no deben interpretarse como ventas propias de Ripley. Los resultados son observaciones puntuales y pueden cambiar.

Al actualizar se retiran automáticamente del seguimiento los productos de marketplace previamente guardados, sin borrar sus registros ni los eventos enviados. Los avisos pendientes/fallidos de Ripley sin confirmación del nuevo alcance se marcan `cancelled` para impedir que se envíen o revivan con `--retry-failed`. Los nuevos eventos guardan la confirmación de alcance para su entrega. Solo se reinicia una vez el reloj de descubrimiento de Ripley para buscar inmediatamente con el filtro correcto; los de Plaza Vea y Oechsle se conservan. No necesitas borrar `runtime` ni cambiar tus credenciales.

La identidad utiliza el identificador nativo o marketplace del enlace (`203...P` o `PMP...`), no la URL de marca/SKU de algunos bloques SEO. Esto evita confundir enlaces de listado con la ficha de compra. La paginación comprueba `offset`, `limit` y `total`; no asume que una sola página contiene todo. Al alcanzar `SEARCH_MAX_PAGES`, se informa búsqueda incompleta. Si el HTML cambia, faltan metadatos o se recibe 403/429, conserva los productos y no inventa stock.

Añade a tu `.env` existente:

```dotenv
RETAILERS=plazavea,oechsle,ripley
RIPLEY_DISCOVERY_INTERVAL=120
RIPLEY_CONTEXT_CONFIRMED=true
RIPLEY_POSTAL_CODE=
RIPLEY_SALES_CHANNEL=
RIPLEY_SELLER_ID=
RIPLEY_SKU_ID=
```

`RIPLEY_CONTEXT_CONFIRMED=true` habilita la interpretación de disponibilidad pública **solo de ofertas vendidas por Ripley**. El filtro de vendedor Ripley es obligatorio y automático; no se configura mediante `RIPLEY_SELLER_ID`. **Los filtros adicionales de ubicación, canal, ID de vendedor y SKU no están implementados**; dejarlos vacíos. Si se configuran, devuelve desconocido en lugar de ignorarlos. Ripley considera `inStock` booleano explícito del listado, o `InStock`/`OutOfStock` en todas las ofertas interpretables de la ficha. Preventa, ausencia de disponibilidad y ofertas ambiguas no se convierten en agotado. Precio y moneda solo se muestran cuando están informados de forma interpretable. El estado corresponde al catálogo consultado y puede depender de la variante publicada; la entrega se comprueba manualmente al abrir la ficha.

Después de detener el proceso anterior con Ctrl+C:

```powershell
# Descubrir solo Ripley: puede enviar alertas reales si encuentra stock.
.\.venv\Scripts\python.exe main.py --retailer ripley --discover
# Mostrar enlaces guardados de Ripley, sin red.
.\.venv\Scripts\python.exe main.py --retailer ripley --list-products
# Continuar con las tres tiendas.
.\.venv\Scripts\python.exe main.py --loop
```

No cambies el token ni el chat ID del grupo. Ripley tiene sus propios estados y reloj persistente: el primer descubrimiento no reinicia el de Plaza Vea ni Oechsle. Los candidatos se consultan durante cada ciclo rápido. **Solo Ripley repite su búsqueda normal cada dos minutos** (`RIPLEY_DISCOVERY_INTERVAL=120`); Plaza Vea mantiene `DISCOVERY_INTERVAL=14400` (cuatro horas), y Oechsle usa `OECHSLE_DISCOVERY_INTERVAL=120`. Los reintentos y el bloqueo son compartidos por el proceso; las tiendas se procesan secuencialmente, sin prometer periodos exactos ni ausencia de límites. Para desactivar Ripley temporalmente, usa `RETAILERS=plazavea,oechsle`.

Un producto específico de Ripley puede configurarse en `MONITOR_PRODUCTS_JSON` con `"tienda":"Ripley"` y el enlace real a su ficha. Se revalida su identidad, vendedor Ripley, idioma y edición; tampoco se aceptan publicaciones de marketplace añadidas manualmente.

### Ripley: vuelta a la búsqueda normal

Se revirtió el descubrimiento ampliado y su proceso separado. Ripley vuelve a usar únicamente la búsqueda pública con `Vendido por:Ripley`, sin recorrer sitemaps ni categorías. No se modificaró la búsqueda de Plaza Vea. La búsqueda de Ripley usa ahora `RIPLEY_DISCOVERY_INTERVAL=120` por defecto, aunque no agregues esa variable a tu `.env`; `DISCOVERY_INTERVAL` sigue rigiendo Plaza Vea.

El vencimiento se comprueba al iniciar cada ciclo. Con `MONITOR_INTERVAL=120`, Ripley vuelve a buscar después de la pausa y del tiempo de procesamiento. No es instantáneo ni garantiza exactamente 120 segundos entre inicios. Los fallos y 429 conservan sus esperas/reintentos; no se fuerza una búsqueda cada dos minutos frente a errores. Los antiguos `RIPLEY_DISCOVERY_MAX_*` se ignoran si quedaron en `.env`. No borres la base ni cambies token/chat. Los archivos de intercambio del explorador anterior ya no se procesan.

Se conserva la limitación original: si Ripley no devuelve una ficha en su buscador, este modo no la descubrirá automáticamente, por muchas veces que se ejecute.

### Oechsle: búsqueda normal cada dos minutos

Oechsle usa ahora `OECHSLE_DISCOVERY_INTERVAL=120` por defecto. La medición real de las cuatro búsquedas, con pausa de 0.5 s entre peticiones, tardó 8.7 segundos y devolvió 40 publicaciones sin errores. Ese tiempo es orientativo y depende de la conexión y del servidor; no es instantáneo. No se cambia su API ni sus filtros. Plaza Vea conserva su API, historial y descubrimiento cada cuatro horas. Ripley conserva su búsqueda cada dos minutos y el filtro de ventas propias.

No necesitas modificar `.env` si quieres ese valor por defecto. `MONITOR_INTERVAL=120` sigue siendo la pausa después de cada ciclo completo: Oechsle, Ripley y el seguimiento de productos guardados se ejecutan secuencialmente, por lo que los inicios pueden estar separados por más de 120 segundos. Los errores y 429 conservan el backoff; no se eliminan pausas ni límites. Para personalizarlo se puede agregar `OECHSLE_DISCOVERY_INTERVAL=120` sin tocar `DISCOVERY_INTERVAL` de Plaza Vea.

## Ilahui: búsqueda directa cada dos minutos

`tiendas/ilahui.py` consulta únicamente `/search` con las opciones proporcionadas: `options[unavailable_products]=last`, `options[prefix]=last` y `options[fields]=title,vendor,product_type,variants.title`. Añade `type=product` y conserva esas opciones al paginar. Los agotados quedan al final de los resultados; se incluyen mientras estén dentro de las páginas devueltas y del límite configurado. **No usa sitemaps, rastreo ampliado ni procesos separados.** Si no hay resultados interpretables, termina esa búsqueda; un bloqueo o HTML no reconocido no se interpreta como catálogo vacío ni stock agotado.

Los enlaces `/products/{handle}` se obtienen del HTML y se normalizan quitando seguimiento y variantes; los alias `/collections/.../products/...` se convierten al mismo enlace. Los detalles se leen del endpoint público Shopify `/products/{handle}.js`. Se verifica el ID estable, título, descripción, tipo de producto, etiquetas y variantes para confirmar Pokémon TCG + 30.º aniversario + inglés. El idioma español sigue excluido. `vendor` es metadato del producto y no se confunde con un vendedor marketplace de otra plataforma.

Una variante con `available=true` indica disponibilidad para compra según Shopify. Todas las variantes seleccionadas con `false` confirman agotado; información incompleta o contradictoria da desconocido. El precio del JSON se convierte desde centavos y la moneda se toma de una declaración explícita de la tienda (no se presupone PEN). No se garantiza inventario físico o entrega por ubicación. Los filtros de ubicación/canal/vendedor no están implementados: déjalos vacíos. `ILAHUI_SKU_ID`, opcional, selecciona un **ID de variante Shopify**, no el código comercial SKU.

Validación real: la búsqueda configurada completó en **20.1 segundos** con pausa de 0.5 s entre peticiones; conservó 29 publicaciones pendientes, sin candidatos confirmados del 30.º aniversario en inglés. Se verificó una ficha de Mini Tin del 30.º aniversario en español, con `available=false`, variantes sin disponibilidad y precio JSON `5990` (59.90 en la moneda de la tienda); se descartó por idioma. Los resultados y tiempos son orientativos y cambian con la tienda y la conexión. No se envió Telegram real desde Codex por falta de credenciales de esta instancia.

Para activar Ilahui, **reemplaza** la línea `RETAILERS` de tu `.env` y agrega sus opciones, conservando el token y chat actuales:

```dotenv
RETAILERS=plazavea,oechsle,ripley,ilahui
ILAHUI_DISCOVERY_INTERVAL=120
ILAHUI_CONTEXT_CONFIRMED=true
ILAHUI_POSTAL_CODE=
ILAHUI_SALES_CHANNEL=
ILAHUI_SELLER_ID=
ILAHUI_SKU_ID=
```

Después de detener el monitor con Ctrl+C:

```powershell
# Una búsqueda solo de Ilahui, con duración total visible.
$tiempo = Measure-Command {
    .\.venv\Scripts\python.exe main.py --retailer ilahui --discover | Out-Host
}
"Tiempo total: {0:N1} segundos" -f $tiempo.TotalSeconds
# Ver candidatos aceptados y enlaces guardados.
.\.venv\Scripts\python.exe main.py --retailer ilahui --list-products
# Continuar con las cuatro tiendas.
.\.venv\Scripts\python.exe main.py --loop
```

No cambia el descubrimiento de Plaza Vea (cuatro horas), Oechsle o Ripley (dos minutos), ni sus filtros. Ilahui mantiene su propia fecha en SQLite. El intervalo es un mínimo entre búsquedas, comprobado al comenzar un ciclo: con `MONITOR_INTERVAL=120` se espera dos minutos **después del procesamiento del conjunto de tiendas**, sin prometer inicios exactos cada 120 segundos. Los errores/429 conservan el backoff; el sitio puede limitar acceso. Una ficha no publicada en los resultados de búsqueda no se descubrirá por esta vía; los candidatos guardados sí continúan consultándose por su enlace.

## Tai Loy: búsqueda directa cada dos minutos

`tiendas/tailoy.py` consulta únicamente `/catalogsearch/result/index/`, usando `q` y el filtro de marca `tailoyetl_999_marcav2=7212` del enlace proporcionado. En la página comprobada, ese filtro muestra **Marca: POKEMON TCG**; no es un filtro de vendedor. Se omite `_gsSearchId`, que es un identificador de búsqueda/sesión, y se conserva el filtro al seguir enlaces de paginación (`p`). No recorre sitemaps ni categorías, ni inicia procesos de exploración adicional.

Las fichas se identifican por su SKU verificado. Se interpretan JSON-LD o los microdatos del producto principal de Magento (nombre, descripción, SKU, precio y moneda); no se confunden títulos de recomendaciones con la ficha solicitada. Si es necesario, se consulta el endpoint público de **solo lectura** `/graphql` con `products(filter:{sku:{eq:...}})` para comprobar `stock_status`. No se consulta por un ID interno como si fuera un SKU comercial. No se envían formularios, no se ejecutan botones y no se realiza ninguna acción de carrito o cuenta.

**Discrepancia real detectada:** una ficha del 30.º aniversario en español mostraba “Pre-Venta / NO DISPONIBLE”, aunque GraphQL devolvía `IN_STOCK`. Por eso preventa, indisponibilidad explícita de la ficha o información contradictoria mantienen `DESCONOCIDO` en vez de generar una alerta. Para productos simples sin esas señales, `IN_STOCK` y `OUT_OF_STOCK` son señales públicas de catálogo. Los productos configurables/agrupados sin datos completos de variantes se mantienen desconocidos. No se garantiza entrega por ubicación ni disponibilidad física del almacén.

Validación real: las cuatro búsquedas configuradas terminaron en **33.8 segundos** con pausa de 0.5 s entre peticiones, conservaron 7 publicaciones pendientes de verificación y no confirmaron candidatos del 30.º aniversario en inglés. Las páginas de búsqueda y producto respondieron y se verificó la estructura del endpoint GraphQL público. Precio y moneda se toman de campos explícitos, sin suponer PEN ni convertirlos desde centavos (a diferencia de Shopify). Los resultados y tiempos pueden cambiar.

Para activar Tai Loy, reemplaza `RETAILERS` en tu `.env` y agrega sus opciones:

```dotenv
RETAILERS=plazavea,oechsle,ripley,ilahui,tailoy
TAILOY_DISCOVERY_INTERVAL=120
TAILOY_CONTEXT_CONFIRMED=true
TAILOY_BRAND_FILTER=7212
TAILOY_POSTAL_CODE=
TAILOY_SALES_CHANNEL=
TAILOY_SELLER_ID=
TAILOY_SKU_ID=
```

Conserva el token/chat y las opciones anteriores. Los filtros de ubicación, canal, vendedor y variante no están implementados: dejarlos vacíos. El filtro de marca es numérico; puede dejarse vacío para no aplicarlo, conservando los criterios del clasificador.

Después de detener el bucle con Ctrl+C, prueba únicamente Tai Loy:

```powershell
$tiempo = Measure-Command {
    .\.venv\Scripts\python.exe main.py --retailer tailoy --discover | Out-Host
}
"Tiempo total: {0:N1} segundos" -f $tiempo.TotalSeconds
.\.venv\Scripts\python.exe main.py --retailer tailoy --list-products
# Todas las tiendas
.\.venv\Scripts\python.exe main.py --loop
```

Cada retailer conserva sus propios candidatos, historial y reloj. Plaza Vea sigue descubriendo cada cuatro horas; los demás cada dos minutos por defecto. El procesamiento es secuencial y se añade a la pausa `MONITOR_INTERVAL`; dos minutos no significa un tiempo exacto entre inicios. Los fallos/bloqueos no significan stock cero y conservan el backoff. Las fichas que la búsqueda no publica no se descubrirán por este modo directo; los candidatos ya guardados sí continúan consultándose.

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

## Seguimiento rápido e intervalos de descubrimiento por tienda

`--once` y `--loop` consultan detalles y stock de los candidatos guardados y de los productos configurados explícitamente. Además, hacen una búsqueda general **solo cuando toca**: al primer arranque sin fecha guardada y después cada cuatro horas para Plaza Vea, y cada dos minutos para Oechsle/Ripley/Ilahui. Las publicaciones pendientes se conservan pero no se consultan en cada revisión de stock.

Configura en `.env` (los archivos existentes no se sobrescriben al actualizar):

```dotenv
MONITOR_INTERVAL=120
DISCOVERY_INTERVAL=14400
DISCOVERY_RETRY_INTERVAL=900
```

```powershell
# Arrancar seguimiento + descubrimiento automático
.\.venv\Scripts\python.exe main.py --loop
# Ver nombres y enlaces guardados; no usa Internet
.\.venv\Scripts\python.exe main.py --list-products
# Opcional: forzar un descubrimiento ahora, sin esperar cuatro horas
.\.venv\Scripts\python.exe main.py --discover
```

La fecha de la última búsqueda completada y la del último intento quedan en SQLite. Reiniciar el programa conserva los intervalos pendientes de cada tienda. Al actualizar desde la versión manual anterior, se hará un descubrimiento inicial porque todavía no existe esa fecha. Las búsquedas incompletas o fallidas conservan sus resultados parciales y los objetivos conocidos, sin marcar éxito; cuando corresponde un descubrimiento, el reintento espera 15 minutos por defecto, también tras reiniciar. Los fallos de stock o Telegram no invalidan por sí mismos una búsqueda completada.

El planificador se comprueba al comenzar cada ciclo: el intervalo de cada tienda (cuatro horas para Plaza Vea, dos minutos para Oechsle/Ripley/Ilahui) es un mínimo entre búsquedas completadas y la siguiente puede empezar algo después, según el ciclo y las pausas. Los 120 segundos se esperan **después de terminar cada revisión**, no son un periodo exacto entre inicios. No hay tareas simultáneas: cuando toca descubrir, ese ciclo puede tardar más y luego continúa el seguimiento habitual.

Para forzar `--discover` o listar mientras corre el bucle, deténlo con Ctrl+C y reinícialo después. El bloqueo impide ejecutar ambos simultáneamente. El descubrimiento guarda candidatos y pendientes y puede enviar alertas de nuevos candidatos disponibles. Reutiliza los detalles completos de búsqueda para evitar otra consulta por resultado. Los pendientes se reevalúan cuando reaparecen en un descubrimiento o están configurados explícitamente. No borres la base existente: conserva candidatos y transiciones sin repetir avisos de disponibilidad continua.

Se mantienen las pausas entre peticiones y la gestión de límites; la optimización no garantiza ausencia de bloqueos. `product_checked.duration_seconds` mide el procesamiento del producto (red, pausas, persistencia y eventual envío); `cycle_finished.duration_seconds` mide el ciclo completo. Los eventos `discovery_started` y `discovery_finished` permiten identificar cuándo se buscó y si terminó completamente.

En Linux usa los mismos argumentos con `.venv/bin/python`. Cron y Programador de tareas también evalúan el vencimiento del descubrimiento al ejecutar `--once`, utilizando la misma base y configuración.

## Configuración

| Variable | Uso / valor predeterminado |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token secreto de BotFather |
| `TELEGRAM_CHAT_ID` | Identificador del chat que recibirá avisos |
| `MONITOR_DATA_DIR` | `runtime`, relativo al repositorio; mismo directorio para todas las ejecuciones |
| `MONITOR_INTERVAL` | 120 segundos de espera tras cada ciclo, mínimo 60; solo para `--loop` |
| `DISCOVERY_INTERVAL` | 14400 segundos (4 horas) entre descubrimientos completados |
| `DISCOVERY_RETRY_INTERVAL` | 900 segundos (15 minutos) antes de reintentar un descubrimiento vencido fallido |
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
*/2 * * * * cd /ruta/PokemonMonitor && /ruta/PokemonMonitor/.venv/bin/python main.py --once >/dev/null 2>&1
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

Crea una tarea con desencadenador diario, repetición **cada 2 minutos**, duración indefinida. Programa: `C:\ruta\PokemonMonitor\.venv\Scripts\python.exe`. Argumentos: `C:\ruta\PokemonMonitor\main.py --once`. Iniciar en: `C:\ruta\PokemonMonitor`. Activa «Ejecutar lo antes posible si se omitió el inicio» y «No iniciar una instancia nueva» si ya está ejecutándose. Selecciona una cuenta con permisos y una política de energía que mantenga la máquina disponible. No hace falta ejecutar como administrador.

## Persistencia, eventos y recuperación

`runtime/monitor.sqlite3` almacena productos, el último intento, el último estado concluyente y una cola de notificaciones. La actualización de estado y la creación del evento comparten una transacción. SQLite WAL y el bloqueo evitan escrituras competidoras. En despliegues distintos no compartas SQLite por una unidad de red; usa una única instancia.

En el primer inicio se importan `datos/productos.json` y `datos/eventos_alertas.jsonl` de forma idempotente. **Los originales, el flag y los logs antiguos no se modifican.** Los eventos históricos y antiguos pendientes se archivan como `historical`, sin reenviarse automáticamente para evitar avisos obsoletos. El formato JSONL antiguo deja de recibir eventos nuevos: los nuevos se guardan en SQLite. Un historial corrupto impide migrar en lugar de descartarlo silenciosamente; corrígelo sobre una copia y conserva el original.

Tipos de alerta:

- `PRODUCTO_NUEVO`: primera observación de un candidato con disponibilidad confirmada, incluido el primer arranque sin historial.
- `REPOSICION`: último estado concluyente agotado y ahora disponible, aunque haya errores intermedios.
- `DISPONIBILIDAD_CONFIRMADA`: producto registrado sin estado concluyente previo y ahora disponible.

Una pérdida temporal de conectividad no borra el último estado concluyente ni genera por sí sola otro aviso al recuperar una disponibilidad ya conocida. Los candidatos nuevos con stock desconocido y los pendientes de idioma/edición se conservan para futuras revisiones. Cambiar palabras clave solo afecta al próximo descubrimiento automático o manual y no borra el histórico. Para retirar definitivamente un producto se necesita una futura opción de exclusión por ID o gestión de bajas; por ahora las exclusiones de texto impiden sus alertas al revalidar detalles.

Las alertas se intentan enviar al detectar el evento, sin esperar a terminar de revisar todos los productos. La cola registra `pending`, `sent`, `failed`, `cancelled` (avisos excluidos por alcance), número de intentos, próxima fecha y error redactado. `--loop` procesa reintentos aproximadamente cada 5 segundos; en cron, solo en las próximas ejecuciones. Los 429 respetan `retry_after`; fallos permanentes o intentos agotados requieren `--retry-failed`. No se consumen intentos si faltan credenciales. Una espera impuesta por Telegram se comparte entre todos los eventos para no saltarse el límite enviando otro mensaje. Un evento pendiente puede enviarse después de que cambie el stock: el mensaje muestra la **fecha de detección**, no una garantía de disponibilidad actual.

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

`monitor/models.py` define `Retailer` (búsqueda, detalles, stock y normalización). `tiendas/plaza_vea.py` y `tiendas/oechsle.py` configuran los dos adaptadores VTEX; `tiendas/vtex.py` comparte clasificación, catálogo y reglas conservadoras de stock. `tiendas/ripley.py` añade el adaptador de HTML/datos estructurados para Ripley. `monitor/app.py` coordina el ciclo; `store.py` conserva la lógica de transiciones/pendientes del monitor original mediante transacciones, `telegram.py` entrega avisos y `http.py` controla red. `tiendas/descubridor.py` conserva las entradas básicas de búsqueda/clasificación originales.

Los cambios se preparan en `feature/telegram-stock-monitor`; no se fusionan ni se publican automáticamente. Revisa el diff y valida la integración autorizada antes de operar alertas de stock en producción. Agregar otro retailer requerirá su adaptador, configuración y pruebas; Falabella no está implementado.


## Falabella: solo venta propia

Activar con `RETAILERS=plazavea,oechsle,ripley,ilahui,tailoy,falabella` y:

```dotenv
FALABELLA_DISCOVERY_INTERVAL=120
FALABELLA_CONTEXT_CONFIRMED=true
```

El adaptador consulta `/falabella-pe/search` con cada término configurado,
`facetSelected=true` y `f.derived.variant.sellerId=FALABELLA`. Reconstruye esos
filtros también al avanzar de página, sin seguir enlaces externos ni rastrear
sitemaps. Guarda enlaces de producto e identifica Pokémon TCG 30 aniversario
en inglés con las mismas reglas de clasificación existentes.

La disponibilidad requiere una ficha con identidad verificable y ofertas públicas
schema.org cuyo vendedor sea exactamente Falabella o Saga Falabella. Marketplace
se excluye y vendedor ausente queda por verificar. Una oferta de un tercero no
aporta stock ni precio a una oferta propia agotada. Preventa, disponibilidad
ambigua, bloqueo, HTML incompatible y errores conservan estado desconocido;
no se deduce agotamiento por ausencia de resultados. Un botón de compra por sí
solo no prueba stock. No se confirma entrega por ubicación y los filtros opcionales
`FALABELLA_POSTAL_CODE`, `FALABELLA_SALES_CHANNEL`, `FALABELLA_SELLER_ID` y
`FALABELLA_SKU_ID` deben permanecer vacíos.

**Validación pendiente contra el sitio real:** en este entorno la conexión HTTPS
fue rechazada por el proxy antes de recibir respuesta de Falabella. Las pruebas
son fixtures simuladas, no evidencia del formato HTML actual ni de cobertura
completa. No se implementó un parser especulativo de APIs privadas o de estados
JavaScript. Si Falabella solo expone los datos con JavaScript o en otro formato,
el ciclo informará búsqueda incompleta y será necesario adaptar el parser con
una respuesta real. No se garantiza descubrir publicaciones que el buscador omite.

Para comprobar solamente esta tienda en Windows, sin iniciar el bucle:

```powershell
$tiempo = Measure-Command {
    .\.venv\Scripts\python.exe main.py --retailer falabella --discover | Out-Host
}
"Tiempo total: {0:N1} segundos" -f $tiempo.TotalSeconds
```

Revisar `search_incomplete`, `discovery_finished` y `cycle_finished` antes de
considerar validada la integración. `--retailer falabella --list-products` muestra
los candidatos guardados sin consultar la red. `main.py --loop` ejecuta todas las
tiendas activadas. El reloj de descubrimiento de Falabella es independiente y
predetermina 120 segundos; el ciclo completo y la espera posterior hacen que la
frecuencia real no sea exactamente de dos minutos. Los demás adaptadores no cambian.
