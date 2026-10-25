"""Descubrimiento ampliado de Ripley; no modifica los adaptadores VTEX."""

import json
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import replace
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser

import requests
from monitor.http import RemoteError, retry_delay
from monitor.models import Product, SearchResult, normalize_url
from tiendas.ripley import Ripley, CatalogHTML, BASE_URL
from tiendas.vtex import normalizar


def broadened_settings(settings):
    # Copia independiente: no amplía SEARCH_TERMS de Plaza Vea ni Oechsle.
    return replace(
        settings, terms=list(dict.fromkeys(settings.terms + ["pokemon", "pokemon tcg"]))
    )


class BudgetExceeded(Exception):
    pass


class DiscoveryClient:
    """Cliente independiente: límites de tiempo, tráfico, memoria y redirecciones."""

    def __init__(self, settings, seconds=180, requests_limit=80):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "PokemonMonitor/1.0", "Accept": "text/html,application/xml"}
        )
        self.deadline = time.monotonic() + seconds
        self.limit = requests_limit
        self.requests = 0
        self.bytes = 0
        self.last = None
        self.blocked = None
        self.retry_after = 0

    def check(self):
        if self.requests >= self.limit or time.monotonic() >= self.deadline:
            raise BudgetExceeded("Límite de exploración alcanzado")
        if self.blocked is not None:
            raise RemoteError(self.blocked, self.retry_after)

    @staticmethod
    def allowed(url):
        p = urlsplit(url)
        return (
            p.scheme == "https"
            and p.netloc == "simple.ripley.com.pe"
            and not p.username
        )

    def get(self, url, params=None, *, as_text=False):
        if not self.allowed(url):
            raise ValueError("Destino fuera de Ripley")
        for redirect in range(4):
            self.check()
            if self.last is not None:
                wait = max(
                    0, self.settings.request_delay - (time.monotonic() - self.last)
                )
                if time.monotonic() + wait >= self.deadline:
                    raise BudgetExceeded("Tiempo de exploración agotado")
                if wait:
                    time.sleep(wait)
            remaining = self.deadline - time.monotonic()
            self.requests += 1
            self.last = time.monotonic()
            try:
                with self.session.get(
                    url,
                    params=params,
                    timeout=(min(5, remaining), min(self.settings.timeout, remaining)),
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, response.headers.get("Location", ""))
                        params = None
                        if not self.allowed(url):
                            raise ValueError("Redirección fuera de Ripley")
                        continue
                    if response.status_code in (401, 403, 429):
                        self.blocked = response.status_code
                        self.retry_after = (
                            retry_delay(response.headers.get("Retry-After"), 900)
                            if response.status_code == 429
                            else 900
                        )
                        raise RemoteError(self.blocked, self.retry_after)
                    if not response.ok:
                        raise RemoteError(response.status_code)
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(65536):
                        if time.monotonic() >= self.deadline:
                            raise BudgetExceeded("Tiempo agotado durante descarga")
                        size += len(chunk)
                        self.bytes += len(chunk)
                        if size > 32 * 1024 * 1024 or self.bytes > 96 * 1024 * 1024:
                            raise BudgetExceeded("Límite de descarga alcanzado")
                        chunks.append(chunk)
                    text = b"".join(chunks).decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                    return text if as_text else json.loads(text)
            except requests.RequestException:
                raise RemoteError() from None
        raise ValueError("Demasiadas redirecciones")


def relevant_product(url):
    path = normalizar(urlsplit(url).path)
    return (
        bool(re.fullmatch(r"/[^/]+[0-9]{6,}p?", path, re.IGNORECASE))
        and "pokemon" in path
        and any(
            word in path
            for word in (
                "tcg",
                "cartas",
                "coleccion",
                "booster",
                "trainer",
                "aniversario",
                "anniversary",
            )
        )
    )


def relevant_page(url):
    path = normalizar(urlsplit(url).path)
    return any(
        word in path
        for word in ("pokemon", "tcg", "cartas-pokemon", "jugueteria", "kidults")
    )


def expanded_discovery(adapter, *, known_urls=(), max_products=60):
    """Busqueda habitual + sitemaps 1P/categorías + enlaces públicos acotados.

    No enumera identificadores ni utiliza APIs privadas o buscadores externos.
    Los enlaces nuevos se verifican en sus fichas incluso si están agotados.
    """
    result = SearchResult()
    found = {}
    errors = []
    stats = {
        "sitemaps": 0,
        "source_pages": 0,
        "links": 0,
        "verified": 0,
        "not_target": 0,
        "known": 0,
    }
    known = set(known_urls)
    documents = set()
    pending = deque()
    pages = deque()
    products = deque()
    queued = set()
    robots = RobotFileParser()
    robots_ready = False
    retryable = False

    def error(message, *, retry=True):
        nonlocal retryable
        retryable = retryable or retry
        result.complete = False
        if message not in errors:
            errors.append(message)

    def enqueue_product(url):
        url = url.split("#", 1)[0]
        if not DiscoveryClient.allowed(url) or not relevant_product(url):
            return
        # Query strings from navigation do not define a new product.
        url = url.split("?", 1)[0]
        if url in known:
            stats["known"] += 1
            return
        if url not in queued:
            queued.add(url)
            products.append(url)
            stats["links"] += 1

    try:
        ordinary = adapter.search()
        result.complete = ordinary.complete
        retryable = not ordinary.complete
        errors.extend(ordinary.errors)
        for p in ordinary.products:
            found[p.key] = p
        robots.parse(
            adapter.client.get(BASE_URL + "/robots.txt", as_text=True).splitlines()
        )
        robots_ready = True
        roots = robots.site_maps() or [BASE_URL + "/sitemap.xml"]
        pending.extend(u for u in roots if DiscoveryClient.allowed(u))
        while pending and stats["sitemaps"] < 12:
            url = pending.popleft()
            if url in documents:
                continue
            if not robots.can_fetch("PokemonMonitor", url):
                error("Sitemap no permitido por robots.txt")
                continue
            documents.add(url)
            stats["sitemaps"] += 1
            content = adapter.client.get(url, as_text=True)
            if "<!DOCTYPE" in content.upper():
                raise ValueError("XML con DTD no admitido")
            root = ET.fromstring(content)
            locations = [
                e.text.strip()
                for e in root.iter()
                if e.tag.rsplit("}", 1)[-1] == "loc" and e.text
            ]
            if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
                for loc in locations:
                    path = normalizar(urlsplit(loc).path)
                    if not DiscoveryClient.allowed(loc) or "3p" in path:
                        continue
                    if any(
                        t in path
                        for t in (
                            "index",
                            "1p",
                            "categorias",
                            "landings",
                            "jugueteria",
                            "kidults",
                            "pokemon",
                            "tcg",
                        )
                    ):
                        if "1p" in path:
                            pending.appendleft(loc)
                        else:
                            pending.append(loc)
            else:
                for loc in locations:
                    if not DiscoveryClient.allowed(loc):
                        continue
                    if relevant_product(loc):
                        enqueue_product(loc)
                    elif relevant_page(loc):
                        pages.append(loc)
        if pending:
            error("Límite de sitemaps alcanzado", retry=False)
        pages = deque(
            sorted(
                pages,
                key=lambda u: not any(
                    t in u.lower() for t in ("pokemon", "tcg", "cartas")
                ),
            )
        )
        while pages and stats["source_pages"] < 8:
            url = pages.popleft()
            if url in documents:
                continue
            if not robots.can_fetch("PokemonMonitor", url):
                continue
            documents.add(url)
            stats["source_pages"] += 1
            page = CatalogHTML(adapter.client.get(url, as_text=True))
            for href, _ in page.links:
                target = urljoin(url, href)
                if not DiscoveryClient.allowed(target):
                    continue
                if relevant_product(target):
                    enqueue_product(target)
                elif relevant_page(target) and target not in documents:
                    pages.append(target)
        if pages:
            error("Límite de páginas de categorías/marcas alcanzado", retry=False)
        while products and stats["verified"] < max_products:
            url = products.popleft()
            if not robots_ready or not robots.can_fetch("PokemonMonitor", url):
                continue
            if url in {
                p.url for p in found.values() if p.classification == "CANDIDATO"
            }:
                continue
            stats["verified"] += 1
            try:
                page = CatalogHTML(adapter.client.get(url, as_text=True))
                canonical = normalize_url(page.canonical or url, "Ripley")
                skeleton = adapter.normalize({"url": canonical, "name": ""})
                product = adapter.details_from_page(skeleton, page)
                if product.classification != "DESCARTADO":
                    found[product.key] = product
                if product.classification != "CANDIDATO":
                    stats["not_target"] += 1
            except (ValueError, TypeError, RemoteError):
                error("Ficha no verificable; no se infiere stock")
        if products:
            error("Límite de fichas alcanzado", retry=False)
    except BudgetExceeded as exc:
        error(str(exc), retry=False)
    except (RemoteError, ValueError, TypeError, ET.ParseError):
        error("Fuente pública no disponible o no interpretable")
    result.products = list(found.values())
    result.errors = errors
    result.stats = stats
    result.retryable = retryable
    return result


class ExpandedRipley(Ripley):
    """Modo explícito de revisión; el bucle normal utiliza el proceso aislado."""

    def __init__(self, settings):
        settings = broadened_settings(settings)
        client = DiscoveryClient(
            settings,
            settings.ripley_discovery_seconds,
            settings.ripley_discovery_requests,
        )
        super().__init__(settings, client)

    def search(self):
        base = Ripley(self.settings, self.client)
        result = expanded_discovery(
            base, max_products=self.settings.ripley_discovery_products
        )
        for product in result.products:
            product.raw["_needs_details"] = True
        return result
