"""Adaptador Plaza Vea y entradas compatibles con el monitor original."""

import re

from monitor.models import Product, normalize_url
from monitor.settings import Settings
from monitor.http import RemoteError
from tiendas.vtex import VtexRetailer, classify, normalizar

BASE_URL = "https://www.plazavea.com.pe"
API = BASE_URL + "/api/catalog_system/pub/products/search"


# Product models verified from the user's catalog metadata and packaging images.
# Match model AND family, not product URLs/IDs or a blanket "TCG = Pokémon" rule.
VERIFIED_POKEMON_MODELS = {
    "10-10448-120": "ultra premium",
    "10-10452-120": "coleccion con figura",
}


def pokemon_identity(raw):
    if normalizar(raw.get("brand", "")).strip() != "tcg":
        return None
    name = normalizar(raw.get("productName", ""))
    description = normalizar(raw.get("description", ""))
    if re.search(
        r"\b(yu[ -]*gi[ -]*oh|yugioh|magic|lorcana|digimon|dragon ball|one piece)\b",
        name + " " + description,
    ):
        return None
    variants = raw.get("Detalle De Variante", [])
    variants = variants if isinstance(variants, list) else [variants]
    if any(
        isinstance(value, str) and re.search(r"\b(mewtwo|mew)\b", normalizar(value))
        for value in variants
    ):
        return {
            "source": "pokemon_variant",
            "characters": [
                character
                for character in ("mewtwo", "mew")
                if any(
                    isinstance(value, str)
                    and re.search(r"\b" + character + r"\b", normalizar(value))
                    for value in variants
                )
            ],
        }
    models = raw.get("Modelo", [])
    models = models if isinstance(models, list) else [models]
    for model in models:
        if not isinstance(model, str):
            continue
        model = model.strip()
        family = VERIFIED_POKEMON_MODELS.get(model)
        if family and family in name:
            return {
                "source": "verified_pokemon_model",
                "model": model,
                "family": family,
            }
    return None


class PlazaVea(VtexRetailer):
    base_url = BASE_URL
    retailer = "Plaza Vea"

    def normalize(self, raw):
        product = super().normalize(raw)
        if product.classification != "POR_VERIFICAR":
            return product
        proof = pokemon_identity(raw)
        if not proof:
            return product
        # Add identity evidence only to the classifier's temporary view. Preserve
        # the retailer's title/description and all edition/language exclusions.
        evidence = dict(raw)
        evidence["description"] = str(raw.get("description") or "") + " Pokemon"
        classification = classify(evidence, self.settings.exclusions)
        if classification == "CANDIDATO":
            product.classification = classification
            product.raw = dict(raw, _identity_evidence=proof)
        return product


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
