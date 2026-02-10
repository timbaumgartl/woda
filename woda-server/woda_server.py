#!/usr/bin/env python3
"""
Winlaufen Online Data Addon Server (WODA Server)
=================================================
Empfaengt Live-Ergebnisse vom WODA Client und generiert eine
statische HTML-Seite, die von einem vorhandenen Webserver (z.B.
Caddy, nginx, Apache) ausgeliefert wird.

Erstellt von:
    Claude (Anthropic) im Auftrag des Nutzers.

Grundlage:
    Empfaengt Daten vom WODA Client, der das Winlaufen Sprecher-PC
    Protokoll (Java ObjectOutputStream ueber TCP, analysiert aus
    pcapng-Mitschnitten) reverse-engineered hat.

HINWEIS: Dieses Programm ist ein unabhaengiges Open-Source-Projekt und
steht in keiner Verbindung zu Winlaufen oder dessen Entwicklern. Es
wird ohne Support, Garantie oder Gewaehrleistung bereitgestellt. Die
Nutzung erfolgt auf eigene Verantwortung. Winlaufen ist ein eingetragenes
Produkt seiner jeweiligen Rechteinhaber.

Voraussetzungen:
    pip install fastapi uvicorn

Usage:
    python3 woda_server.py --path /var/www/ergebnisse
    python3 woda_server.py --path /var/www/ergebnisse --port 8443
    python3 woda_server.py --path /var/www/ergebnisse --token MEIN_TOKEN
"""

import argparse
import json
import os
import secrets
import sys
import threading
from datetime import datetime

DISCLAIMER = (
    "Dieses Programm ist ein unabhaengiges Projekt und steht in keiner "
    "Verbindung zu Winlaufen oder dessen Entwicklern. Keine Garantie, "
    "kein Support. Nutzung auf eigene Verantwortung."
)


# ==============================================================================
# Race State - Akkumuliert Ergebnisse aller Kategorien
# ==============================================================================
class RaceState:
    """Thread-safe Zustand aller empfangenen Ergebnisse."""

    MAX_CATEGORIES = 50  # Schutz gegen Speichererschoepfung

    def __init__(self):
        self._lock = threading.Lock()
        self.all_categories = {}
        self._category_order = []
        self.update_count = 0
        self.last_updated = ""

    def apply_update(self, data):
        """
        Verarbeitet ein Update vom WODA Client.
        Speichert Ergebnisse pro Kategorie, damit abgeschlossene
        Altersklassen weiterhin angezeigt werden.
        """
        category = data.get("category", "")
        rows = data.get("rows", [])
        headers = data.get("headers", [])
        ts = datetime.now().strftime("%H:%M:%S")

        with self._lock:
            if category and rows:
                # Neue Kategorie nur wenn Limit nicht erreicht
                if category not in self.all_categories:
                    if len(self._category_order) >= self.MAX_CATEGORIES:
                        # Aelteste Kategorie entfernen
                        oldest = self._category_order.pop(0)
                        del self.all_categories[oldest]
                    self._category_order.append(category)
                self.all_categories[category] = {
                    "rows": rows,
                    "headers": headers if headers else [
                        "Rang", "StNr", "Name, Vorname", "Verein",
                        "Vbd", "Laufzeit", "Rueckstand"
                    ],
                    "updated": ts,
                    "count": len(rows),
                }
            self.update_count += 1
            self.last_updated = datetime.now().isoformat()

    def get_all(self):
        """Thread-safe Kopie aller Kategorie-Ergebnisse."""
        with self._lock:
            result = []
            for cat_name in self._category_order:
                data = self.all_categories.get(cat_name, {})
                result.append({
                    "category": cat_name,
                    "rows": [list(r) for r in data.get("rows", [])],
                    "updated": data.get("updated", ""),
                    "count": data.get("count", 0),
                })
            return {
                "categories": result,
                "update_count": self.update_count,
                "last_updated": self.last_updated,
            }


# ==============================================================================
# HTML Generator - Waldgruen Farbschema
# ==============================================================================
def _esc(s):
    """HTML-Escaping (alle 5 relevanten Zeichen)."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def generate_html(state_data):
    """
    Erzeugt die komplette HTML-Seite mit allen Kategorien.
    Zeigt nur Daten an die tatsaechlich empfangen wurden.
    Farbschema: Waldgruen.
    """
    categories = state_data.get("categories", [])
    update_count = state_data.get("update_count", 0)
    now = datetime.now().strftime("%H:%M:%S")

    cat_blocks = []
    for cat in categories:
        cat_name = _esc(cat["category"])
        rows = cat["rows"]
        updated = _esc(cat.get("updated", ""))
        count = len(rows)

        count_info = f"{count} Teilnehmer" if count else "Keine Ergebnisse"

        if rows:
            table_html = _build_table(rows)
        else:
            table_html = '<p class="empty">Noch keine Ergebnisse.</p>'

        cat_blocks.append(
            f'<section class="category">\n'
            f'  <h2>{cat_name}</h2>\n'
            f'  <div class="cat-meta">{count_info} (Stand: {updated})</div>\n'
            f'  {table_html}\n'
            f'</section>'
        )

    if not cat_blocks:
        cat_blocks.append(
            '<section class="category">\n'
            '  <p class="empty">Warte auf Ergebnisse...</p>\n'
            '</section>'
        )

    categories_html = "\n".join(cat_blocks)

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <title>Live-Ergebnisse</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         "Helvetica Neue", Arial, sans-serif;
            background: #f0f4f0;
            color: #1a1a1a;
            line-height: 1.5;
        }}

        .container {{
            max-width: 960px;
            margin: 0 auto;
            padding: 0.75rem;
        }}

        header {{
            background: #1b4332;
            color: #fff;
            padding: 1rem 0;
            margin-bottom: 1rem;
        }}

        header .container {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: baseline;
            gap: 0.5rem;
        }}

        header h1 {{
            font-size: 1.25rem;
            font-weight: 600;
        }}

        header .status {{
            font-size: 0.8rem;
            opacity: 0.65;
        }}

        .category {{
            background: #fff;
            border-radius: 6px;
            margin-bottom: 1rem;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }}

        .category h2 {{
            font-size: 1rem;
            font-weight: 600;
            padding: 0.6rem 0.75rem;
            background: #2d6a4f;
            color: #fff;
        }}

        .cat-meta {{
            font-size: 0.8rem;
            color: #666;
            padding: 0.25rem 0.75rem;
            background: #f5faf5;
            border-bottom: 1px solid #d8e8d8;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
        }}

        th {{
            background: #e8f0e8;
            color: #2d3748;
            padding: 0.4rem 0.5rem;
            text-align: left;
            font-weight: 600;
            border-bottom: 2px solid #b7d4b7;
            white-space: nowrap;
        }}

        th.num {{ text-align: right; }}

        td {{
            padding: 0.35rem 0.5rem;
            border-bottom: 1px solid #e2e8e2;
        }}

        td.num {{
            text-align: right;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }}

        tr.leader td {{
            background: #e8f5e9;
            font-weight: 600;
        }}

        tr:hover td {{ background: #f5faf5; }}
        tr.leader:hover td {{ background: #dcedc8; }}

        .empty {{
            padding: 1rem 0.75rem;
            color: #999;
            font-style: italic;
        }}

        footer {{
            text-align: center;
            padding: 1rem;
            font-size: 0.75rem;
            color: #999;
        }}

        .table-wrap {{
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}

        @media (max-width: 640px) {{
            header h1 {{ font-size: 1.1rem; }}
            .container {{ padding: 0.5rem; }}
            table {{ font-size: 0.78rem; }}
            th, td {{ padding: 0.3rem 0.35rem; }}
            .category h2 {{ font-size: 0.9rem; padding: 0.5rem 0.6rem; }}
            td.name, td.club {{
                max-width: 120px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }}
        }}

        @media (max-width: 400px) {{
            td.name, td.club {{ max-width: 90px; }}
            .col-vbd {{ display: none; }}
        }}
    </style>
</head>
<body>
    <header>
        <div class="container">
            <h1>Live-Ergebnisse</h1>
            <div class="status">{now}</div>
        </div>
    </header>
    <main class="container">
        {categories_html}
    </main>
    <footer>
        {_esc(DISCLAIMER)}<br>
        Seite aktualisiert sich automatisch alle 30 Sekunden.
    </footer>
</body>
</html>"""


def _build_table(rows):
    """Erzeugt eine HTML-Tabelle aus Ergebniszeilen."""
    lines = ['<div class="table-wrap"><table>']
    lines.append(
        "<thead><tr>"
        '<th class="num">Rang</th>'
        '<th class="num">StNr</th>'
        "<th>Name, Vorname</th>"
        "<th>Verein</th>"
        '<th class="col-vbd">Vbd</th>'
        '<th class="num">Laufzeit</th>'
        '<th class="num">Rckst.</th>'
        "</tr></thead>"
    )
    lines.append("<tbody>")
    for row in rows:
        rang   = _esc(row[0]) if len(row) > 0 else ""
        stnr   = _esc(row[1]) if len(row) > 1 else ""
        name   = _esc(row[2]) if len(row) > 2 else ""
        verein = _esc(row[3]) if len(row) > 3 else ""
        vbd    = _esc(row[4]) if len(row) > 4 else ""
        zeit   = _esc(row[5]) if len(row) > 5 else ""
        rueck  = _esc(row[6]) if len(row) > 6 else ""
        tr_cls = ' class="leader"' if rang == "1" else ""
        lines.append(
            f"<tr{tr_cls}>"
            f'<td class="num">{rang}</td>'
            f'<td class="num">{stnr}</td>'
            f'<td class="name">{name}</td>'
            f'<td class="club">{verein}</td>'
            f'<td class="col-vbd">{vbd}</td>'
            f'<td class="num">{zeit}</td>'
            f'<td class="num">{rueck}</td>'
            f"</tr>"
        )
    lines.append("</tbody></table></div>")
    return "\n".join(lines)


def write_html(path, html_content):
    """
    Schreibt die HTML-Datei atomar in den Webserver-Pfad.
    Verwendet eine temporaere Datei + Rename um Halbschreibungen
    zu vermeiden.
    """
    target = os.path.join(path, "index.html")
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html_content)
        os.replace(tmp, target)
        return True
    except OSError as e:
        print(f"FEHLER: Konnte {target} nicht schreiben: {e}")
        # Temp-Datei aufraeumen
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ==============================================================================
# FastAPI Application
# ==============================================================================
def create_app(state, token, web_path):
    """
    Erstellt die FastAPI-App fuer die API-Schnittstelle.

    Endpunkte:
        POST /api/update   - Ergebnis-Update vom Client (Auth erforderlich)
        GET  /api/health   - Serverstatus (Auth erforderlich)
    """
    try:
        from fastapi import FastAPI, Request, HTTPException
        from fastapi.responses import JSONResponse
    except ImportError:
        print("FEHLER: FastAPI nicht installiert.")
        print("  pip install fastapi uvicorn")
        sys.exit(1)

    import hmac
    import time as _time

    # Maximale Request-Body-Groesse: 2 MB
    MAX_BODY_SIZE = 2 * 1024 * 1024
    # Maximale Spaltenanzahl pro Zeile
    MAX_COLS = 10
    # Maximale Zeichenlaenge pro Zellenwert
    MAX_CELL_LEN = 200
    # Maximale Zeilenanzahl pro Update
    MAX_ROWS = 500

    # Einfacher Rate-Limiter (Schutz gegen Missbrauch)
    _rate_lock = threading.Lock()
    _rate_window = {}      # {ip: [timestamp, ...]}
    RATE_LIMIT = 60        # Max Requests pro Zeitfenster
    RATE_WINDOW_SEC = 60   # Zeitfenster in Sekunden

    def _check_rate(client_ip):
        """Einfacher IP-basierter Rate-Limiter."""
        now = _time.time()
        with _rate_lock:
            if client_ip not in _rate_window:
                _rate_window[client_ip] = []
            # Alte Eintraege entfernen
            _rate_window[client_ip] = [
                t for t in _rate_window[client_ip]
                if now - t < RATE_WINDOW_SEC
            ]
            if len(_rate_window[client_ip]) >= RATE_LIMIT:
                raise HTTPException(
                    status_code=429,
                    detail=f"Zu viele Anfragen (max {RATE_LIMIT}/{RATE_WINDOW_SEC}s)"
                )
            _rate_window[client_ip].append(now)

    app = FastAPI(
        title="WODA Server",
        description="Winlaufen Online Data Addon Server API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def _check_auth(request: Request):
        """Token-Pruefung mit konstantem Zeitverhalten (Timing-Attack-sicher)."""
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Ungueltig oder fehlendes API-Token")

    def _validate_update(data):
        """Validiert und bereinigt die Eingabedaten vom Client."""
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Erwartet JSON-Objekt")

        category = data.get("category", "")
        if not isinstance(category, str) or len(category) > MAX_CELL_LEN:
            raise HTTPException(status_code=400, detail="Ungueltiger Kategoriename")

        rows = data.get("rows", [])
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail="'rows' muss eine Liste sein")
        if len(rows) > MAX_ROWS:
            raise HTTPException(status_code=400,
                                detail=f"Zu viele Zeilen ({len(rows)} > {MAX_ROWS})")

        clean_rows = []
        for row in rows:
            if not isinstance(row, list):
                continue
            clean_row = []
            for val in row[:MAX_COLS]:
                s = str(val) if val is not None else ""
                clean_row.append(s[:MAX_CELL_LEN])
            clean_rows.append(clean_row)

        # Headers validieren
        raw_headers = data.get("headers", [])
        clean_headers = []
        if isinstance(raw_headers, list):
            for h in raw_headers[:MAX_COLS]:
                if isinstance(h, str):
                    clean_headers.append(h[:MAX_CELL_LEN])

        return {
            "category": category[:MAX_CELL_LEN],
            "rows": clean_rows,
            "headers": clean_headers,
        }

    @app.post("/api/update")
    async def receive_update(request: Request):
        """Empfaengt ein Ergebnis-Update vom WODA Client."""
        _check_rate(request.client.host if request.client else "unknown")
        _check_auth(request)

        # Body-Groesse pruefen (sicher gegen nicht-numerische Werte)
        content_length = request.headers.get("content-length", "")
        try:
            if content_length and int(content_length) > MAX_BODY_SIZE:
                raise HTTPException(status_code=413,
                                    detail=f"Request zu gross (max {MAX_BODY_SIZE} Bytes)")
        except ValueError:
            raise HTTPException(status_code=400, detail="Ungueltiger Content-Length Header")

        try:
            body = await request.body()
        except Exception:
            raise HTTPException(status_code=400, detail="Konnte Body nicht lesen")

        if len(body) > MAX_BODY_SIZE:
            raise HTTPException(status_code=413,
                                detail=f"Request zu gross (max {MAX_BODY_SIZE} Bytes)")

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Ungueltiges JSON")

        clean_data = _validate_update(data)
        state.apply_update(clean_data)

        # HTML neu generieren und schreiben
        state_data = state.get_all()
        html = generate_html(state_data)
        write_html(web_path, html)

        return {
            "status": "ok",
            "update_count": state_data["update_count"],
            "categories": len(state_data["categories"]),
        }

    @app.get("/api/health")
    async def health(request: Request):
        """Serverstatus - prueft auch Auth."""
        _check_rate(request.client.host if request.client else "unknown")
        _check_auth(request)
        state_data = state.get_all()
        return {
            "status": "ok",
            "version": "1.0.0",
            "update_count": state_data["update_count"],
            "categories": len(state_data["categories"]),
            "last_updated": state_data["last_updated"],
        }

    return app


# ==============================================================================
# Main
# ==============================================================================
def main():
    p = argparse.ArgumentParser(
        description="WODA Server - Winlaufen Online Data Addon Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Beispiele:
  %(prog)s --path /var/www/ergebnisse
  %(prog)s --path /var/www/ergebnisse --port 8443
  %(prog)s --path /var/www/ergebnisse --token MEIN_GEHEIMER_TOKEN

Das API-Token wird beim Start angezeigt. Der WODA Client benoetigt
dieses Token um Daten senden zu koennen (--api-token).

{DISCLAIMER}
""",
    )

    p.add_argument("--path", required=True,
                    help="Pfad zum veroeffentlichten Ordner des Webservers "
                         "(z.B. /var/www/ergebnisse)")
    p.add_argument("--port", type=int, default=8443,
                    help="Port fuer die API-Schnittstelle (default: 8443)")
    p.add_argument("--host", default="0.0.0.0",
                    help="Bind-Adresse (default: 0.0.0.0)")
    p.add_argument("--token", default=os.environ.get("WODA_API_TOKEN", ""),
                    help="API-Token festlegen (oder Umgebungsvariable WODA_API_TOKEN, "
                         "sonst wird eines generiert)")
    p.add_argument("--debug", "-d", action="store_true",
                    help="Debug-Ausgabe")

    args = p.parse_args()

    # --- Abhaengigkeiten ---
    try:
        import uvicorn
    except ImportError:
        print("FEHLER: uvicorn nicht installiert.")
        print("  pip install fastapi uvicorn")
        sys.exit(1)

    # --- Pfad pruefen ---
    web_path = os.path.realpath(args.path)
    if not os.path.isdir(web_path):
        print(f"FEHLER: Verzeichnis existiert nicht: {web_path}")
        print(f"  Bitte zuerst anlegen: mkdir -p {web_path}")
        sys.exit(1)

    # Sicherheitscheck: Pfad darf nicht in Systemverzeichnisse zeigen
    FORBIDDEN_PATHS = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/proc", "/sys", "/dev"]
    for fp in FORBIDDEN_PATHS:
        if web_path == fp or web_path.startswith(fp + "/"):
            print(f"FEHLER: Sicherheitscheck - {web_path} ist ein Systemverzeichnis")
            sys.exit(1)

    # Test-Schreibzugriff
    test_file = os.path.join(web_path, ".woda_write_test")
    try:
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
    except OSError as e:
        print(f"FEHLER: Keine Schreibrechte in {web_path}: {e}")
        sys.exit(1)

    # --- Token ---
    token = args.token or secrets.token_urlsafe(32)

    # --- State ---
    state = RaceState()

    # --- Initiale HTML-Seite schreiben ---
    initial_html = generate_html(state.get_all())
    if not write_html(web_path, initial_html):
        sys.exit(1)

    # --- App ---
    app = create_app(state, token, web_path)

    # --- Ausgabe ---
    print()
    print("=" * 60)
    print("  WODA Server gestartet")
    print("=" * 60)
    print()
    print(f"  API-Port:     {args.port}")
    print(f"  Web-Pfad:     {web_path}")
    print(f"  HTML-Datei:   {os.path.join(web_path, 'index.html')}")
    print()
    print(f"  API-Token:    {token}")
    print()
    print("  Dieses Token wird im WODA Client als --api-token benoetigt.")
    print()
    print("-" * 60)
    print(f"  {DISCLAIMER}")
    print("-" * 60)
    print()
    print("  Beenden: Ctrl+C")
    print()

    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning" if not args.debug else "info",
        )
    except KeyboardInterrupt:
        print("\nServer beendet.")


if __name__ == "__main__":
    main()
