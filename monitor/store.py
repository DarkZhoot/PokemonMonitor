import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo
from monitor.models import Product, Stock, normalize_url


def now():
    return datetime.now(ZoneInfo("America/Lima")).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS products(key TEXT PRIMARY KEY, data TEXT NOT NULL, state TEXT NOT NULL, last_known TEXT, checked TEXT, detail TEXT, price REAL, currency TEXT);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, product_key TEXT, payload TEXT NOT NULL, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, error TEXT, sent_at TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        """
        )

    def close(self):
        self.db.close()

    def products(self):
        return [
            Product(**json.loads(r["data"]))
            for r in self.db.execute("SELECT data FROM products")
        ]

    def discovery_due(self, interval, retry_interval, timestamp=None):
        timestamp = time.time() if timestamp is None else timestamp
        rows = dict(
            self.db.execute(
                "SELECT key,value FROM meta WHERE key IN ('discovery_attempt', 'discovery_success')"
            ).fetchall()
        )
        success = (
            float(rows["discovery_success"]) if "discovery_success" in rows else None
        )
        attempt = (
            float(rows["discovery_attempt"]) if "discovery_attempt" in rows else None
        )
        if success is not None and timestamp - success < interval:
            return False
        if success is not None and (attempt is None or attempt <= success):
            return True
        return attempt is None or timestamp - attempt >= retry_interval

    def mark_discovery(self, successful=False):
        key = "discovery_success" if successful else "discovery_attempt"
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(time.time()))
            )

    def migrate(self, root):
        if self.db.execute("SELECT 1 FROM meta WHERE key='migrated'").fetchone():
            return
        path = root / "datos/productos.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict):
            raise ValueError("Historial inválido")
        event_path = root / "datos/eventos_alertas.jsonl"
        events = (
            [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if event_path.exists()
            else []
        )
        with self.db:
            for url, row in state.items():
                if not isinstance(row, dict) or row.get("estado") not in (
                    "DISPONIBLE",
                    "AGOTADO",
                    "DESCONOCIDO",
                ):
                    raise ValueError("Producto histórico inválido")
                p = Product(
                    str(row.get("id") or ""), row.get("nombre", ""), normalize_url(url)
                )
                if not p.id:
                    p.id = "url:" + p.url
                self.db.execute(
                    "INSERT OR IGNORE INTO products VALUES(?,?,?,?,?,?,?,?)",
                    (
                        p.key,
                        json.dumps(asdict(p), ensure_ascii=False),
                        row["estado"],
                        row["estado"] if row["estado"] != "DESCONOCIDO" else None,
                        row.get("ultima_consulta"),
                        "Importado; requiere revalidación",
                        None,
                        None,
                    ),
                )
                # Historical alerts are archived, never automatically resent.
                for event in row.get("avisos_pendientes", []):
                    events.append(event)
            for event in events:
                if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                    raise ValueError("Evento histórico inválido")
                self.db.execute(
                    "INSERT OR IGNORE INTO events(id,payload,status) VALUES(?,?,'historical')",
                    (event["id"], json.dumps(event, ensure_ascii=False)),
                )
            self.db.execute("INSERT INTO meta VALUES('migrated',?)", (now(),))

    def merge_identity(self, product):
        # Preserve imported state when the old ID was actually a SKU.
        rows = self.db.execute(
            "SELECT key,data FROM products WHERE key<>?", (product.key,)
        ).fetchall()
        for row in rows:
            if json.loads(row["data"])["url"] == product.url:
                if not self.db.execute(
                    "SELECT 1 FROM products WHERE key=?", (product.key,)
                ).fetchone():
                    self.db.execute(
                        "UPDATE products SET key=? WHERE key=?",
                        (product.key, row["key"]),
                    )
                self.db.execute(
                    "UPDATE events SET product_key=? WHERE product_key=?",
                    (product.key, row["key"]),
                )
                self.db.execute("DELETE FROM products WHERE key=?", (row["key"],))

    def observe(self, product, stock):
        with self.db:
            self.merge_identity(product)
            old = self.db.execute(
                "SELECT * FROM products WHERE key=?", (product.key,)
            ).fetchone()
            kind = None
            previous = old["last_known"] if old else None
            if product.classification == "CANDIDATO" and stock.state == "DISPONIBLE":
                if old is None:
                    kind = "PRODUCTO_NUEVO"
                elif previous == "AGOTADO":
                    kind = "REPOSICION"
                elif previous is None:
                    kind = "DISPONIBILIDAD_CONFIRMADA"
            known = stock.state if stock.state != "DESCONOCIDO" else previous
            stamp = now()
            self.db.execute(
                "INSERT OR REPLACE INTO products VALUES(?,?,?,?,?,?,?,?)",
                (
                    product.key,
                    json.dumps(asdict(product), ensure_ascii=False),
                    stock.state,
                    known,
                    stamp,
                    stock.detail,
                    stock.price,
                    stock.currency,
                ),
            )
            if kind:
                event = dict(
                    id=str(uuid4()),
                    tipo=kind,
                    nombre=product.name,
                    tienda=product.retailer,
                    url=product.url,
                    estado=stock.state,
                    precio=stock.price,
                    moneda=stock.currency,
                    fecha=stamp,
                )
                self.db.execute(
                    "INSERT INTO events(id,product_key,payload) VALUES(?,?,?)",
                    (event["id"], product.key, json.dumps(event, ensure_ascii=False)),
                )
            return kind
