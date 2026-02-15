#!/usr/bin/env python3
"""
Winlaufen Online Data Addon Server (WODA Server)
=================================================

Was macht dieses Programm?
--------------------------
Der WODA Server empfaengt Live-Ergebnisse vom WODA Client und
erzeugt daraus eine HTML-Seite. Diese Seite wird in einen Ordner
geschrieben, den ein vorhandener Webserver (z.B. Caddy, nginx)
an die Besucher ausliefert.

Ablauf:
  1. WODA Client -> sendet JSON per HTTP an /api/update
  2. WODA Server -> speichert die Daten, erzeugt HTML
  3. Webserver   -> liefert die HTML-Datei an Browser aus

Der Server zeigt in der Konsole ein Live-Log an, das jedes
empfangene Update protokolliert.

Voraussetzungen:
  pip install fastapi uvicorn

HINWEIS: Dieses Programm ist ein unabhaengiges Open-Source-Projekt und
steht in keiner Verbindung zu Winlaufen oder dessen Entwicklern. Es
wird ohne Support, Garantie oder Gewaehrleistung bereitgestellt.

Erstellt von Claude (Anthropic) im Auftrag des Nutzers.

Beispiele:
    python3 woda_server.py --path /var/www/ergebnisse
    python3 woda_server.py --path /var/www/ergebnisse --port 8443
    python3 woda_server.py --path /var/www/ergebnisse --token MEIN_TOKEN
"""

# =============================================================================
# Imports
# =============================================================================
import argparse        # Kommandozeilenargumente
import json            # JSON-Verarbeitung
import os              # Dateisystem-Operationen
import secrets         # Kryptographisch sichere Zufallswerte
import sys             # Programmsteuerung
import threading       # Thread-Sicherheit
import hmac            # Timing-sichere Token-Pruefung
from datetime import datetime  # Zeitstempel

# Hinweistext
DISCLAIMER = (
    "Dieses Programm ist ein unabhaengiges Projekt und steht in keiner "
    "Verbindung zu Winlaufen oder dessen Entwicklern. Keine Garantie, "
    "kein Support. Nutzung auf eigene Verantwortung."
)


# =============================================================================
# Live-Log - Zeigt empfangene Updates in der Konsole an
# =============================================================================

class LiveLog:
    """
    Protokolliert alle Serveraktivitaeten in der Konsole.
    Thread-sicher, damit parallele Requests sich nicht in die Quere kommen.
    """

    def __init__(self):
        self._lock = threading.Lock()

    def info(self, msg):
        """Normale Logmeldung (weiss)."""
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            print(f"  [{ts}] {msg}")

    def update(self, category, row_count, update_num):
        """Logmeldung fuer ein empfangenes Ergebnis-Update (gruen)."""
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            print(f"  [{ts}] \033[32mUpdate #{update_num}\033[0m: "
                  f"{category} ({row_count} Teilnehmer)")

    def warn(self, msg):
        """Warnmeldung (gelb)."""
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            print(f"  [{ts}] \033[33mWARNUNG: {msg}\033[0m")

    def error(self, msg):
        """Fehlermeldung (rot)."""
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            print(f"  [{ts}] \033[31mFEHLER: {msg}\033[0m")


# =============================================================================
# RaceState - Speichert alle empfangenen Ergebnisse
# =============================================================================

class RaceState:
    """
    Thread-sicherer Zustand aller empfangenen Ergebnisse.

    Speichert die Ergebnisse pro Kategorie (z.B. "U16 w", "Herren").
    Wenn eine neue Kategorie kommt, wird sie hinzugefuegt.
    Wenn eine bestehende Kategorie aktualisiert wird (z.B. durch
    Korrekturen), werden die alten Daten ueberschrieben.
    Abgeschlossene Kategorien bleiben sichtbar.
    """

    MAX_CATEGORIES = 50  # Schutz gegen Speichererschoepfung

    def __init__(self):
        self._lock = threading.Lock()
        self.all_categories = {}    # {name: {rows, headers, updated, count}}
        self._category_order = []   # Reihenfolge der Kategorien
        self.update_count = 0       # Zaehler fuer empfangene Updates
        self.last_updated = ""      # Zeitstempel des letzten Updates

    def apply_update(self, data):
        """
        Verarbeitet ein Update vom WODA Client.
        Speichert oder aktualisiert die Ergebnisse der Kategorie.
        """
        category = data.get("category", "")
        rows = data.get("rows", [])
        headers = data.get("headers", [])
        ts = datetime.now().strftime("%H:%M:%S")

        with self._lock:
            if category and rows:
                # Neue Kategorie? Zur Reihenfolge hinzufuegen
                if category not in self.all_categories:
                    # Wenn zu viele Kategorien: aelteste entfernen
                    if len(self._category_order) >= self.MAX_CATEGORIES:
                        oldest = self._category_order.pop(0)
                        del self.all_categories[oldest]
                    self._category_order.append(category)

                # Ergebnisse speichern (ueberschreibt vorherige)
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
        """Gibt eine thread-sichere Kopie aller Ergebnisse zurueck."""
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


# =============================================================================
# HTML-Generator - Erzeugt die Webseite (Waldgruen-Farbschema)
# =============================================================================

def _esc(s):
    """
    HTML-Escaping: Wandelt Sonderzeichen in HTML-Entities um.
    Verhindert XSS-Angriffe (Cross-Site-Scripting).
    """
    return (
        str(s)
        .replace("&", "&amp;")     # & muss zuerst ersetzt werden!
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def generate_html(state_data):
    """
    Erzeugt die komplette HTML-Seite mit allen Kategorien.
    Zeigt nur Daten an die tatsaechlich empfangen wurden.
    Farbschema: Waldgruen (#1b4332, #2d6a4f, #e8f5e9).
    Auto-Refresh: Alle 30 Sekunden.
    """
    categories = state_data.get("categories", [])
    now = datetime.now().strftime("%H:%M:%S")

    # HTML-Bloecke fuer jede Kategorie erzeugen
    cat_blocks = []
    for cat in categories:
        cat_name = _esc(cat["category"])
        rows = cat["rows"]
        updated = _esc(cat.get("updated", ""))
        count = len(rows)

        count_info = f"{count} Teilnehmer" if count else "Keine Ergebnisse"
        table_html = _build_table(rows) if rows else '<p class="empty">Noch keine Ergebnisse.</p>'

        cat_blocks.append(
            f'<section class="category">\n'
            f'  <h2>{cat_name}</h2>\n'
            f'  <div class="cat-meta">{count_info} (Stand: {updated})</div>\n'
            f'  {table_html}\n'
            f'</section>'
        )

    # Falls noch keine Ergebnisse: Wartetext anzeigen
    if not cat_blocks:
        cat_blocks.append(
            '<section class="category">\n'
            '  <p class="empty">Warte auf Ergebnisse...</p>\n'
            '</section>'
        )

    categories_html = "\n".join(cat_blocks)

    # Die komplette HTML-Seite (CSS ist inline, keine externen Dateien)
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
        header h1 {{ font-size: 1.25rem; font-weight: 600; }}
        header .status {{ font-size: 0.8rem; opacity: 0.65; }}
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
    """Erzeugt eine HTML-Tabelle aus Ergebniszeilen (je 7 Spalten)."""
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
        # Rang 1 = Fuehrender, wird farblich hervorgehoben
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

    "Atomar" bedeutet: Erst in eine temporaere Datei schreiben,
    dann umbenennen. So sieht ein Besucher nie eine halb geschriebene
    Datei, sondern immer die alte oder die neue Version.
    """
    target = os.path.join(path, "index.html")
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html_content)
        os.replace(tmp, target)  # Atomares Umbenennen
        return True
    except OSError as e:
        print(f"FEHLER: Konnte {target} nicht schreiben: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# =============================================================================
# FastAPI Application - REST-API fuer den WODA Client
# =============================================================================

def create_app(state, token, web_path, log):
    """
    Erstellt die FastAPI-App mit zwei Endpunkten:
      POST /api/update   - Empfaengt Ergebnis-Updates (Auth noetig)
      GET  /api/health   - Prueft ob der Server laeuft (Auth noetig)

    Die Authentifizierung erfolgt per API-Token im Authorization-Header.
    """
    try:
        from fastapi import FastAPI, Request, HTTPException
    except ImportError:
        print("FEHLER: FastAPI nicht installiert.")
        print("  pip install fastapi uvicorn")
        sys.exit(1)

    # --- Limits fuer Eingabedaten ---
    MAX_BODY_SIZE = 2 * 1024 * 1024  # 2 MB maximale Request-Groesse
    MAX_COLS = 10                     # Max Spalten pro Zeile
    MAX_CELL_LEN = 200                # Max Zeichen pro Zellenwert
    MAX_ROWS = 500                    # Max Zeilen pro Update

    # --- FastAPI-App erstellen ---
    # docs_url/redoc_url/openapi_url deaktiviert:
    # Keine API-Dokumentation oeffentlich exponieren
    app = FastAPI(
        title="WODA Server", version="1.0.0",
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    def _check_auth(request: Request):
        """
        Prueft das API-Token im Authorization-Header.
        Verwendet hmac.compare_digest fuer timing-sichere Pruefung
        (verhindert dass man das Token per Zeitmessung erraten kann).
        """
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
            log.warn(f"Ungueltiges Token von {request.client.host if request.client else '?'}")
            raise HTTPException(status_code=401, detail="Ungueltiges API-Token")

    def _validate(data):
        """
        Validiert und bereinigt die Eingabedaten.
        Kuerzt zu lange Strings, entfernt ungueltige Zeilen.
        """
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Erwartet JSON-Objekt")

        category = data.get("category", "")
        if not isinstance(category, str) or len(category) > MAX_CELL_LEN:
            raise HTTPException(status_code=400, detail="Ungueltiger Kategoriename")

        rows = data.get("rows", [])
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail="'rows' muss Liste sein")
        if len(rows) > MAX_ROWS:
            raise HTTPException(status_code=400, detail=f"Zu viele Zeilen ({len(rows)})")

        # Zeilen bereinigen: Nur Listen, max Spalten, Strings kuerzen
        clean_rows = []
        for row in rows:
            if not isinstance(row, list):
                continue
            clean_rows.append([
                str(val)[:MAX_CELL_LEN] if val is not None else ""
                for val in row[:MAX_COLS]
            ])

        # Headers bereinigen
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

    # --- Endpunkt: Ergebnis-Update empfangen ---
    @app.post("/api/update")
    async def receive_update(request: Request):
        """Empfaengt ein Ergebnis-Update vom WODA Client."""
        _check_auth(request)

        # Request-Groesse pruefen
        try:
            body = await request.body()
        except Exception:
            raise HTTPException(status_code=400, detail="Konnte Body nicht lesen")
        if len(body) > MAX_BODY_SIZE:
            raise HTTPException(status_code=413, detail="Request zu gross")

        # JSON parsen
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Ungueltiges JSON")

        # Validieren, speichern, HTML erzeugen
        clean = _validate(data)
        state.apply_update(clean)
        state_data = state.get_all()
        write_html(web_path, generate_html(state_data))

        # Live-Log: Update protokollieren
        log.update(clean["category"], len(clean["rows"]), state_data["update_count"])

        return {
            "status": "ok",
            "update_count": state_data["update_count"],
            "categories": len(state_data["categories"]),
        }

    # --- Endpunkt: Serverstatus ---
    @app.get("/api/health")
    async def health(request: Request):
        """Gibt den Serverstatus zurueck (auch fuer --api-test)."""
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


# =============================================================================
# Programmstart
# =============================================================================

def main():
    p = argparse.ArgumentParser(
        description="WODA Server - Winlaufen Online Data Addon Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Beispiele:
  %(prog)s --path /var/www/ergebnisse
  %(prog)s --path /var/www/ergebnisse --port 8443
  %(prog)s --path /var/www/ergebnisse --token MEIN_TOKEN

{DISCLAIMER}
""",
    )

    p.add_argument("--path", required=True,
                   help="Pfad zum Webserver-Verzeichnis (z.B. /var/www/ergebnisse)")
    p.add_argument("--port", type=int, default=8443,
                   help="Port fuer die API (default: 8443)")
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind-Adresse (default: 0.0.0.0)")
    p.add_argument("--token", default=os.environ.get("WODA_API_TOKEN", ""),
                   help="API-Token (oder WODA_API_TOKEN, sonst automatisch)")
    p.add_argument("--debug", "-d", action="store_true",
                   help="Debug-Ausgabe (ausfuehrliches HTTP-Log)")

    args = p.parse_args()

    # --- Abhaengigkeiten pruefen ---
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

    # Systemverzeichnisse blockieren
    FORBIDDEN = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/proc", "/sys", "/dev"]
    for fp in FORBIDDEN:
        if web_path == fp or web_path.startswith(fp + "/"):
            print(f"FEHLER: {web_path} ist ein Systemverzeichnis")
            sys.exit(1)

    # Schreibzugriff testen
    test_file = os.path.join(web_path, ".woda_write_test")
    try:
        with open(test_file, "w") as f: f.write("test")
        os.remove(test_file)
    except OSError as e:
        print(f"FEHLER: Keine Schreibrechte in {web_path}: {e}")
        sys.exit(1)

    # --- Token ---
    token = args.token or secrets.token_urlsafe(32)

    # --- Initialisierung ---
    log = LiveLog()
    state = RaceState()

    # Initiale leere HTML-Seite erzeugen
    if not write_html(web_path, generate_html(state.get_all())):
        sys.exit(1)

    app = create_app(state, token, web_path, log)

    # --- Startbanner ---
    print()
    print("=" * 60)
    print("  WODA Server")
    print("=" * 60)
    print()
    print(f"  API-Port:     {args.port}")
    print(f"  Web-Pfad:     {web_path}")
    print(f"  HTML-Datei:   {os.path.join(web_path, 'index.html')}")
    print()
    print(f"  API-Token:    {token}")
    print()
    print("  Dieses Token im WODA Client als --api-token verwenden.")
    print()
    print("-" * 60)
    print(f"  {DISCLAIMER}")
    print("-" * 60)
    print()
    print("  Live-Log (empfangene Updates erscheinen hier):")
    print()

    # --- Server starten ---
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
