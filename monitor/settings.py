import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    data_dir: Path = ROOT / "runtime"
    interval: int = 600
    timeout: int = 20
    request_delay: float = 1.0
    retries: int = 2
    telegram_retries: int = 3
    pages: int = 15
    token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    terms: list = field(
        default_factory=lambda: [
            "pokemon tcg 30",
            "pokemon 30 aniversario",
            "pokemon 30th anniversary",
            "pokemon tcg ingles",
        ]
    )
    exclusions: list = field(
        default_factory=lambda: [
            "japanese",
            "japones",
            "korean",
            "coreano",
            "spanish",
            "espanol",
            "french",
            "frances",
            "german",
            "aleman",
            "italian",
            "italiano",
            "portugues",
            "portuguese",
            "chinese",
            "chino",
        ]
    )
    products: list = field(default_factory=list)
    context_confirmed: bool = False
    postal_code: str = ""
    sales_channel: str = ""
    seller_id: str = ""
    sku_id: str = ""

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env", override=False)
        s = cls()
        for attr, env, minimum in [
            ("interval", "MONITOR_INTERVAL", 60),
            ("timeout", "MONITOR_TIMEOUT", 1),
            ("retries", "MONITOR_RETRIES", 0),
            ("telegram_retries", "TELEGRAM_RETRIES", 1),
            ("pages", "SEARCH_MAX_PAGES", 1),
        ]:
            value = int(os.getenv(env, getattr(s, attr)))
            if (
                value < minimum
                or value
                > {
                    "interval": 86400,
                    "timeout": 120,
                    "retries": 5,
                    "telegram_retries": 10,
                    "pages": 50,
                }[attr]
            ):
                raise ValueError(f"{env}: fuera de rango")
            setattr(s, attr, value)
        s.request_delay = float(os.getenv("MONITOR_REQUEST_DELAY", "1"))
        if not 0.5 <= s.request_delay <= 60:
            raise ValueError("MONITOR_REQUEST_DELAY: fuera de rango")
        s.data_dir = Path(os.getenv("MONITOR_DATA_DIR", "runtime"))
        if not s.data_dir.is_absolute():
            s.data_dir = ROOT / s.data_dir
        s.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        s.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        s.terms = [
            x.strip()
            for x in os.getenv("SEARCH_TERMS", "|".join(s.terms)).split("|")
            if x.strip()
        ]
        s.exclusions = [
            x.strip()
            for x in os.getenv("PRODUCT_EXCLUSIONS", "|".join(s.exclusions)).split("|")
            if x.strip()
        ]
        if not s.terms:
            raise ValueError("SEARCH_TERMS vacío")
        s.products = json.loads(os.getenv("MONITOR_PRODUCTS_JSON", "[]"))
        if not isinstance(s.products, list) or any(
            not isinstance(p, dict) or not p.get("url") for p in s.products
        ):
            raise ValueError("MONITOR_PRODUCTS_JSON debe contener objetos con url")
        s.context_confirmed = (
            os.getenv("PLAZAVEA_CONTEXT_CONFIRMED", "false").lower() == "true"
        )
        for attr in ["postal_code", "sales_channel", "seller_id", "sku_id"]:
            setattr(s, attr, os.getenv("PLAZAVEA_" + attr.upper(), ""))
        return s
