"""Private loopback supervision and incident journal."""

import hashlib
import hmac
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
OWNER = "propietario-local"
PAGE_SIZE = 100
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", re.I)
ISO_DATE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[\w.\-]{12,}|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|xox[baprs]-[\w-]{12,}|AKIA[A-Z0-9]{16})\b|"
    r"""(?:contrase(?:ñ|n)a|password|cookie|client[_ -]?secret|api[_ -]?key|(?:access|refresh)[_ -]?token)\s*["']?\s*[:=]\s*\S|"""
    r"://[^\s/@:]+:[^\s/@]+@|[?&](?:token|signature|sig|access_token)=[^\s&]+",
    re.I,
)
CONTRACT = json.loads((ROOT / "contract.json").read_text(encoding="utf-8"))
BASE_HEADERS = [
    ("Cache-Control", "private, no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    ),
]
ENTRY = "id, kind, host, category, state, title, detail, evidence, related_id AS relatedId, created_at AS createdAt"
ASSET_NAMES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
    "/panel.css": ("panel.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def open_db(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def initialize_db(path):
    with closing(open_db(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
            db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nPRAGMA user_version=1; COMMIT;")
        elif version != 1:
            raise RuntimeError("Versión de bitácora incompatible.")
        db.execute("SELECT id FROM journal LIMIT 1")


def respond(start, status, body, mime, method, headers=()):
    start(
        f"{status} {HTTPStatus(status).phrase}",
        BASE_HEADERS + [("Content-Type", mime), ("Content-Length", str(len(body))), *headers],
    )
    return [b"" if method == "HEAD" else body]


def json_reply(start, status, value, method):
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return respond(start, status, body, "application/json; charset=utf-8", method)


def list_entries(path, query):
    try:
        args = parse_qs(query, keep_blank_values=True, max_num_fields=8)
    except ValueError as error:
        raise RequestError(400, "La página solicitada no es válida.") from error
    before = args.get("before", [""])[0]
    before_id = args.get("id", [""])[0]
    sql = f"SELECT {ENTRY} FROM journal WHERE owner_id=?"
    params = [OWNER]
    if "before" in args or "id" in args:
        if not ISO_DATE.fullmatch(before) or not UUID.fullmatch(before_id):
            raise RequestError(400, "La página solicitada no es válida.")
        sql += " AND (created_at<? OR (created_at=? AND id<?))"
        params.extend([before, before, before_id])
    with closing(open_db(path)) as db:
        rows = db.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", [*params, PAGE_SIZE + 1]).fetchall()
    entries = [dict(row) for row in rows[:PAGE_SIZE]]
    last = entries[-1] if len(rows) > PAGE_SIZE else None
    return {"entries": entries, "next": {"before": last["createdAt"], "id": last["id"]} if last else None}


def parse_entry(environ, origin, hosts):
    content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" or environ.get("HTTP_ORIGIN") != origin or environ.get("HTTP_SEC_FETCH_SITE") == "cross-site":
        raise RequestError(415, "Formato u origen de registro no válido.")
    body = environ["wsgi.input"].read(65537)
    if len(body) > 65536:
        raise RequestError(413, "El registro supera el tamaño permitido.")
    try:
        text = body.decode("utf-8")
        if len(text) > 14000:
            raise RequestError(413, "El registro supera el tamaño permitido.")
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError()
    except (UnicodeError, ValueError) as error:
        raise RequestError(400, "No se pudo leer el registro.") from error
    fields = ("requestId", "kind", "host", "category", "state", "title", "detail", "evidence", "relatedId")
    entry = {key: raw[key].strip() if isinstance(raw.get(key), str) else "" for key in fields}
    kind, category, state = (entry[key] for key in ("kind", "category", "state"))
    title, detail, evidence = (entry[key] for key in ("title", "detail", "evidence"))
    if (
        not UUID.fullmatch(entry["requestId"]) or entry["host"] not in hosts
        or not title or len(title) > 100 or len(detail) > 2200 or len(evidence) > 600
    ):
        raise RequestError(400, "Revisa el equipo y los límites de los campos.")
    if SECRET.search("\n".join((title, detail, evidence))):
        raise RequestError(400, "Quita las credenciales o enlaces con firma del registro.")
    # Reject unpaired Unicode surrogates before writing to SQLite.
    try:
        for value in entry.values():
            value.encode("utf-8")
    except UnicodeError as error:
        raise RequestError(400, "El registro contiene caracteres no válidos.") from error
    incident = category in CONTRACT["incidentCategories"] and len(detail) >= 8
    valid = (
        (kind == "incident" and incident and state == "open")
        or (kind == "incident_update" and incident and state in CONTRACT["incidentStates"] and UUID.fullmatch(entry["relatedId"]))
        or (
            kind in ("verification", "change") and category in CONTRACT["components"] and state in CONTRACT["confidences"]
            and len(detail) >= 8 and (state != "confirmed" or len(evidence) >= 5)
        )
        or (kind == "checklist" and category in CONTRACT["actions"] and state == "reviewed" and len(detail) >= 20 and len(evidence) >= 5)
    )
    if not valid:
        raise RequestError(400, "Completa los campos requeridos y aporta una referencia si el resultado está confirmado.")
    entry["id"] = entry.pop("requestId")
    if kind != "incident_update":
        entry["relatedId"] = None
    return entry


def write_entry(path, entry):
    with closing(open_db(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if entry["relatedId"]:
            incident = db.execute(
                "SELECT host, category FROM journal WHERE owner_id=? AND kind='incident' AND id=?",
                (OWNER, entry["relatedId"]),
            ).fetchone()
            if not incident or any(incident[key] != entry[key] for key in ("host", "category")):
                raise RequestError(404, "No se encontró la incidencia asociada.")
        values = ("id", "kind", "host", "category", "state", "title", "detail", "evidence", "relatedId")
        at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        saved = db.execute(
            """
            INSERT INTO journal (id, kind, host, category, state, title, detail, evidence, related_id, owner_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING
            """,
            (*[entry[key] for key in values], OWNER, at),
        )
        if saved.rowcount == 1:
            return 201, {"entry": {**entry, "createdAt": at}}
        found = db.execute(f"SELECT {ENTRY} FROM journal WHERE id=? AND owner_id=?", (entry["id"], OWNER)).fetchone()
        if found and all(found[key] == entry[key] for key in values):
            return 200, {"entry": dict(found)}
        raise RequestError(409, "Ya existe otro registro con este identificador.")


def make_app(path, authorization_digest, origin, inventory):
    initialize_db(path)
    assets = {url: ((ROOT / "public" / file).read_bytes(), mime) for url, (file, mime) in ASSET_NAMES.items()}
    authority = origin.removeprefix("http://")
    hosts = {host["id"] for host in inventory["hosts"]}

    def application(environ, start):
        method = environ["REQUEST_METHOD"]
        route = environ.get("PATH_INFO", "")
        if environ.get("REMOTE_ADDR") != "127.0.0.1" or environ.get("HTTP_HOST") != authority:
            return json_reply(start, 403, {"error": "Acceso reservado al equipo local."}, method)
        provided = environ.get("HTTP_AUTHORIZATION", "").encode("utf-8")
        if not hmac.compare_digest(hashlib.sha256(provided).digest(), authorization_digest):
            return respond(start, 401, "Introduce la clave de esta sesión.".encode(), "text/plain; charset=utf-8",
                           method, [("WWW-Authenticate", 'Basic realm="FrameBridge", charset="UTF-8"')])
        try:
            if route == "/api/inventario" and method in ("GET", "HEAD"):
                return json_reply(start, 200, inventory, method)
            if route == "/api/bitacora" and method in ("GET", "HEAD"):
                return json_reply(start, 200, list_entries(path, environ.get("QUERY_STRING", "")), method)
            if route == "/api/bitacora" and method == "POST":
                status, data = write_entry(path, parse_entry(environ, origin, hosts))
                return json_reply(start, status, data, method)
            if method in ("GET", "HEAD") and route in assets:
                body, mime = assets[route]
                return respond(start, 200, body, mime, method)
            raise RequestError(404, "Ruta no encontrada.")
        except RequestError as error:
            return json_reply(start, error.status, {"error": str(error)}, method)
        except sqlite3.Error:
            logging.error("Error al consultar o guardar la bitácora local.")
            return json_reply(start, 503, {"error": "La bitácora local no está disponible en este momento."}, method)

    return application
