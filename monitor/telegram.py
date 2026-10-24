import json
import time
import requests
from monitor.http import retry_delay
from monitor.store import now


class Telegram:
    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()

    def send(self, event):
        if not self.settings.token or not self.settings.chat_id:
            return False, 300, "Credenciales Telegram no configuradas", False
        price = "No informado"
        if event.get("precio") is not None:
            price = f"{event['precio']:.2f} {event.get('moneda') or '(moneda no confirmada)'}"
        text = (
            f"{event['tipo']}\n{event['nombre']}\nTienda: {event['tienda']}\n"
            f"Precio: {price}\nEstado: {event['estado']}\n{event['url']}\n"
            f"Detección (America/Lima): {event['fecha']}\nEvento: {event['id']}"
        )
        # No parse_mode: retailer text cannot inject Telegram formatting.
        payload = {
            "chat_id": self.settings.chat_id,
            "text": text[:4000],
            "reply_markup": {
                "inline_keyboard": [[{"text": "Abrir producto", "url": event["url"]}]]
            },
        }
        try:
            r = self.session.post(
                f"https://api.telegram.org/bot{self.settings.token}/sendMessage",
                json=payload,
                timeout=self.settings.timeout,
            )
        except requests.RequestException:
            return False, 60, "Error de transporte; entrega incierta", False
        try:
            body = r.json()
        except ValueError:
            return False, 60, "Respuesta Telegram inválida", False
        if not isinstance(body, dict):
            return False, 60, "Respuesta Telegram inválida", False
        if r.status_code == 200 and body.get("ok") is True:
            return True, 0, "", False
        code = body.get("error_code", r.status_code)
        if code == 429:
            params = body.get("parameters") or {}
            delay = retry_delay(
                params.get("retry_after") if isinstance(params, dict) else None, 60
            )
            return False, delay, "Telegram limitado (429)", False
        return False, 60, f"Telegram HTTP {code}", code in (400, 401, 403, 404)


def deliver(store, telegram, log):
    if not telegram.settings.token or not telegram.settings.chat_id:
        pending = store.db.execute(
            "SELECT count(*) FROM events WHERE status='pending'"
        ).fetchone()[0]
        if pending:
            log("telegram_unconfigured", pending=pending)
        return False
    cooldown = store.db.execute(
        "SELECT value FROM meta WHERE key='telegram_cooldown'"
    ).fetchone()
    if cooldown and float(cooldown[0]) > time.time():
        return False
    success = True
    rows = store.db.execute(
        "SELECT * FROM events WHERE status='pending' AND next_try<=? ORDER BY rowid LIMIT 20",
        (time.time(),),
    ).fetchall()
    for row in rows:
        event = json.loads(row["payload"])
        ok, delay, error, permanent = telegram.send(event)
        attempts = row["attempts"] + 1
        status = (
            "sent"
            if ok
            else (
                "failed"
                if permanent or attempts >= telegram.settings.telegram_retries
                else "pending"
            )
        )
        delay = max(delay, min(3600, 30 * 2 ** min(attempts, 7)))
        with store.db:
            store.db.execute(
                "UPDATE events SET status=?,attempts=?,next_try=?,error=?,sent_at=? WHERE id=?",
                (
                    status,
                    attempts,
                    time.time() + delay,
                    error,
                    now() if ok else None,
                    row["id"],
                ),
            )
        log(
            "telegram_attempt",
            event_id=row["id"],
            attempt=attempts,
            status=status,
            error=error,
        )
        success = success and ok
        # Do not flood Telegram or retry other events during a shared rate limit/error.
        if not ok:
            with store.db:
                store.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES('telegram_cooldown',?)",
                    (str(time.time() + delay),),
                )
            break
        time.sleep(1)
    return success
