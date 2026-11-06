"""Auditable product identity rules shared by all retailer adapters."""

import re
from html import unescape
import unicodedata

# Product model references verified against user-supplied catalog and packaging.
# Require the matching product family as well; these are not merchant product IDs.
VERIFIED_POKEMON_MODELS = {
    "10-10448-120": "ultra premium",
    "10-10452-120": "coleccion con figura",
}
POKEMON_CHARACTERS = (
    "mewtwo",
    "mew",
    "pikachu",
    "charizard",
    "eevee",
    "gengar",
    "lugia",
    "umbreon",
    "espeon",
)
MODEL_FIELDS = {
    "modelo",
    "model",
    "mpn",
    "sku",
    "model number",
    "numero de modelo",
    "codigo de modelo",
    "codigo del modelo",
    "manufacturer part number",
}
OTHER_GAMES = (
    r"\b(yu[ -]*gi[ -]*oh|yugioh|magic|lorcana|digimon|dragon ball|one piece)\b"
)


def normalizar(text):
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", unescape(str(text)).lower())
        if not unicodedata.combining(c)
    )


def text_value(value):
    if isinstance(value, (list, tuple)):
        return " ".join(text_value(x) for x in value)
    if isinstance(value, dict):
        return " ".join(
            text_value(value[k]) for k in ("name", "title", "value") if k in value
        )
    return "" if value is None else str(value)


def metadata_fields(raw):
    fields = [
        raw.get(k, "") for k in ("productName", "description", "brand", "categories")
    ]
    for key in raw.get("allSpecifications", []) or []:
        if isinstance(key, str):
            fields.extend([key, raw.get(key, "")])
    for item in raw.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        fields.extend(
            item.get(k, "") for k in ("name", "nameComplete", "complementName")
        )
        for key in item.get("variations", []) or []:
            if isinstance(key, str):
                fields.extend([key, item.get(key, "")])
    return fields


def pokemon_identity(raw):
    text = normalizar(" ".join(text_value(x) for x in metadata_fields(raw)))
    if re.search(OTHER_GAMES, text):
        return None
    if re.search(r"\bpokemon\b", text):
        return {"source": "explicit_pokemon"}
    # Distinctive Pokémon names within a TCG context, never a generic TCG brand alone.
    if not re.search(r"\b(tcg|trading card game)\b", text):
        return None
    characters = [
        word for word in POKEMON_CHARACTERS if re.search(r"\b" + word + r"\b", text)
    ]
    if characters:
        return {"source": "pokemon_variant", "characters": characters}
    model_values = []
    for key, value in raw.items():
        if normalizar(key).strip() in MODEL_FIELDS:
            model_values.append(value)
    for item in raw.get("items", []) or []:
        if isinstance(item, dict):
            model_values.extend(item.get(key, "") for key in ("model", "mpn", "sku"))
    # Model codes in the product title are also explicit metadata.
    model_text = (
        " ".join(text_value(x) for x in model_values)
        + " "
        + text_value(raw.get("productName"))
    )
    name = normalizar(raw.get("productName", ""))
    for model, family in VERIFIED_POKEMON_MODELS.items():
        if family in name and re.search(
            r"(?<![\w-])" + re.escape(model) + r"(?![\w-])", model_text
        ):
            return {
                "source": "verified_pokemon_model",
                "model": model,
                "family": family,
            }
    return None


def canonical_metadata(raw):
    metadata = dict(raw)
    name = re.sub(
        r"\btcg(?=30(?:th)?\b)",
        "TCG ",
        str(metadata.get("productName") or ""),
        flags=re.IGNORECASE,
    )
    context = normalizar(name + " " + str(metadata.get("brand") or ""))
    family = re.search(
        r"\b(ultra\s+(?:pc|premium)|upc|mini\s+tin|booster|binder|elite\s+trainer|etb|figure|figura)\b",
        context,
    )
    aliases = []
    if family and re.search(r"\b(tcg|trading card game)\b", context):
        if re.search(r"\b30\s*th\b", name, re.IGNORECASE) and not re.search(
            r"anniversary|aniversario", name, re.IGNORECASE
        ):
            name = re.sub(r"\b30\s*th\b", "30th anniversary", name, flags=re.IGNORECASE)
            aliases.append("30th: aniversario en familia TCG")
        if re.search(r"\bing\b", name, re.IGNORECASE):
            name = re.sub(r"\bing\b", "ingles", name, flags=re.IGNORECASE)
            aliases.append("ing: ingles")
        # A bilingual abbreviation must not be silently treated as English-only.
        name = re.sub(r"\besp\b", "espanol", name, flags=re.IGNORECASE)
    metadata["productName"] = name
    return metadata, aliases


def assess(raw, exclusions):
    raw, aliases = canonical_metadata(raw)
    text = normalizar(" ".join(text_value(x) for x in metadata_fields(raw)))
    excluded = [
        x
        for x in exclusions
        if x and re.search(r"(?<!\w)" + re.escape(normalizar(x)) + r"(?!\w)", text)
    ]
    tcg = bool(re.search(r"\b(tcg|trading card game)\b", text))
    edition = bool(
        re.search(
            r"\b30\s*[.oº°]*\s*(aniversario|anniversary)\b|\b30th\s+anniversary\b", text
        )
    )
    language = bool(re.search(r"\b(ingles|english)\b", text))
    proof = pokemon_identity(raw)
    missing = []
    if proof is None:
        missing.append(
            "identidad Pokemon"
            if not re.search(OTHER_GAMES, text)
            else "identidad Pokemon sin conflicto con otros juegos"
        )
    if not tcg:
        missing.append("TCG")
    if not edition:
        missing.append("30 aniversario")
    if not language:
        missing.append("idioma ingles")
    status = "DESCARTADO" if excluded else ("POR_VERIFICAR" if missing else "CANDIDATO")
    return {
        "classification": status,
        "metadata_aliases": aliases,
        "watch_eligible": status != "DESCARTADO" and tcg and edition,
        "missing": missing,
        "excluded": excluded,
        "identity_evidence": proof,
        "review_needed": status == "POR_VERIFICAR"
        and tcg
        and (edition or proof is not None),
    }


def classify(raw, exclusions):
    return assess(raw, exclusions)["classification"]


def annotated_raw(raw, decision):
    data = dict(
        raw, _classification=decision, _watch=decision.get("watch_eligible", False)
    )
    if decision.get("metadata_aliases"):
        data["_metadata_aliases"] = decision["metadata_aliases"]
    proof = decision["identity_evidence"]
    if proof and proof["source"] != "explicit_pokemon":
        data["_identity_evidence"] = proof
    return data
