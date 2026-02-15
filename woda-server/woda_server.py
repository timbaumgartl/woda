#!/usr/bin/env python3
"""
Winlaufen Online Data Addon Server (WODA Server)
=================================================

Der WODA Server empfaengt Live-Ergebnisse vom WODA Client und
erzeugt daraus eine HTML-Seite. Diese wird in einen Ordner geschrieben,
den ein vorhandener Webserver (z.B. Caddy, nginx) ausliefert.

Voraussetzungen: pip install fastapi uvicorn

HINWEIS: Unabhaengiges Open-Source-Projekt, keine Verbindung zu Winlaufen.
Keine Garantie, kein Support. Nutzung auf eigene Verantwortung.

Erstellt mit Claude Opus 4.6 (claude-opus-4-6) von Anthropic, Februar 2025.
Projekt: https://github.com/timbaumgartl/woda

Beispiele:
    python3 woda_server.py --path /var/www/ergebnisse
    python3 woda_server.py --path /var/www/ergebnisse --port 8443
    python3 woda_server.py --path /var/www/ergebnisse --token MEIN_TOKEN
"""

# =============================================================================
# Imports
# =============================================================================
import argparse
import json
import os
import secrets
import sys
import threading
import hmac
from datetime import datetime

DISCLAIMER = (
    "Dieses Programm ist ein unabhaengiges Projekt und steht in keiner "
    "Verbindung zu Winlaufen oder dessen Entwicklern. Keine Garantie, "
    "kein Support. Nutzung auf eigene Verantwortung."
)


# =============================================================================
# Live-Log - Konsolenausgabe mit Zeitstempel und Farbe
# =============================================================================

class LiveLog:
    """Thread-sicheres Live-Log fuer die Konsole."""

    def __init__(self):
        self._lock = threading.Lock()

    def _print(self, color, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            if color:
                print(f"  [{ts}] {color}{msg}\033[0m")
            else:
                print(f"  [{ts}] {msg}")

    def info(self, msg):
        self._print(None, msg)

    def update(self, msg):
        """Gruen: Erfolgreiches Update."""
        self._print("\033[32m", msg)

    def warn(self, msg):
        """Gelb: Warnung."""
        self._print("\033[33m", f"WARNUNG: {msg}")

    def error(self, msg):
        """Rot: Fehler."""
        self._print("\033[31m", f"FEHLER: {msg}")

    def debug(self, msg):
        """Grau: Debug-Detail."""
        self._print("\033[90m", f"  {msg}")


# =============================================================================
# RaceState - Speichert alle empfangenen Ergebnisse (thread-sicher)
# =============================================================================

class RaceState:
    """
    Speichert Ergebnisse pro Kategorie.
    Neue Kategorien werden hinzugefuegt, bestehende aktualisiert.
    """

    MAX_CATEGORIES = 50

    def __init__(self):
        self._lock = threading.Lock()
        # Hauptspeicher: {kategoriename: {rows, headers, updated, count}}
        self.all_categories = {}
        # Reihenfolge in der Kategorien eingegangen sind
        self._category_order = []
        self.update_count = 0
        self.last_updated = ""

    def apply_update(self, data):
        """
        Speichert ein Update. Gibt (gespeichert: bool, info: str) zurueck.
        """
        category = data.get("category", "").strip()
        rows = data.get("rows", [])
        headers = data.get("headers", [])
        ts = datetime.now().strftime("%H:%M:%S")

        with self._lock:
            self.update_count += 1
            self.last_updated = datetime.now().isoformat()

            if not rows:
                return False, "Keine Ergebniszeilen im Update"

            # Kategorie-Fallback: Immer einen Namen vergeben
            if not category:
                category = "Ergebnisse"

            # Neue Kategorie registrieren
            if category not in self.all_categories:
                if len(self._category_order) >= self.MAX_CATEGORIES:
                    oldest = self._category_order.pop(0)
                    del self.all_categories[oldest]
                self._category_order.append(category)

            # Ergebnisse speichern
            self.all_categories[category] = {
                "rows": rows,
                "headers": headers if headers else [
                    "Rang", "StNr", "Name, Vorname", "Verein",
                    "Vbd", "Laufzeit", "Rueckstand"
                ],
                "updated": ts,
                "count": len(rows),
            }

            # Sofort verifizieren dass die Daten gespeichert sind
            verify = self.all_categories.get(category, {})
            saved_count = len(verify.get("rows", []))
            if saved_count != len(rows):
                return False, (f"Verifikation fehlgeschlagen: "
                               f"{len(rows)} gesendet, {saved_count} gespeichert")

            return True, f"{category}: {len(rows)} Zeilen gespeichert"

    def get_all(self):
        """Gibt eine Kopie aller Ergebnisse zurueck."""
        with self._lock:
            result = []
            for cat_name in self._category_order:
                data = self.all_categories.get(cat_name)
                if data is None:
                    continue
                result.append({
                    "category": cat_name,
                    "rows": [list(r) for r in data.get("rows", [])],
                    "headers": list(data.get("headers", [])),
                    "updated": data.get("updated", ""),
                    "count": data.get("count", 0),
                })
            return {
                "categories": result,
                "update_count": self.update_count,
                "last_updated": self.last_updated,
            }

    def get_summary(self):
        """Kurze Zusammenfassung fuer Status-Anzeige."""
        with self._lock:
            lines = [f"Updates: {self.update_count}"]
            lines.append(f"Kategorien: {len(self._category_order)}")
            for cat_name in self._category_order:
                data = self.all_categories.get(cat_name, {})
                lines.append(f"  {cat_name}: {data.get('count', 0)} Zeilen"
                             f" (Stand: {data.get('updated', '?')})")
            return "\n".join(lines)


# =============================================================================
# HTML-Generator (Waldgruen-Farbschema)
# =============================================================================

def _esc(s):
    """HTML-Escaping: Verhindert XSS-Angriffe."""
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
    Erzeugt die HTML-Seite mit allen Kategorien.
    Gibt (html_string, diagnose_string) zurueck.
    """
    categories = state_data.get("categories", [])
    now = datetime.now().strftime("%H:%M:%S")

    # Diagnose sammeln
    diag = []
    diag.append(f"generate_html: {len(categories)} Kategorien")

    cat_blocks = []
    total_rows = 0
    for cat in categories:
        cat_name = _esc(cat["category"])
        rows = cat["rows"]
        updated = _esc(cat.get("updated", ""))
        count = len(rows)
        total_rows += count

        diag.append(f"  '{cat['category']}': {count} Zeilen")

        count_info = f"{count} Teilnehmer" if count else "Keine Ergebnisse"
        if rows:
            cat_headers = cat.get("headers", [])
            table_html = _build_table(rows, cat_headers if cat_headers else None)
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
        diag.append("  (keine Kategorien - Wartetext)")

    categories_html = "\n".join(cat_blocks)

    html = f"""<!DOCTYPE html>
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

    diag.append(f"  HTML: {len(html)} Bytes, {total_rows} Zeilen total")
    has_tbody = "<tbody>" in html
    diag.append(f"  <tbody> vorhanden: {has_tbody}")

    return html, "\n".join(diag)


def _build_table(rows, headers=None):
    """
    Erzeugt eine HTML-Tabelle aus Ergebniszeilen.
    Spaltenanzahl wird dynamisch aus den Headers bestimmt.
    Langlauf: 7 Spalten (Rang, StNr, Name, Verein, Vbd, Laufzeit, Rueckstand)
    Biathlon: 8 Spalten (+Schiessen)
    """
    if not headers:
        headers = ["Rang", "StNr", "Name, Vorname", "Verein",
                   "Vbd", "Laufzeit", "Rckst."]
    ncols = len(headers)

    # Spaltentypen bestimmen: Name/Verein links, Rest rechts
    # Spalte 2 = Name, Spalte 3 = Verein (immer linksbuendig)
    right_cols = set(range(ncols)) - {2, 3}  # Alles ausser Name/Verein
    # Vbd-Spalte (Index 4) hat eigene CSS-Klasse fuer responsive Ausblendung
    vbd_col = 4

    lines = ['<div class="table-wrap"><table>']

    # Thead
    lines.append("<thead><tr>")
    for i, h in enumerate(headers):
        css_classes = []
        if i in right_cols:
            css_classes.append("num")
        if i == vbd_col:
            css_classes.append("col-vbd")
        cls = f' class="{" ".join(css_classes)}"' if css_classes else ""
        lines.append(f"<th{cls}>{_esc(h)}</th>")
    lines.append("</tr></thead>")

    # Tbody
    lines.append("<tbody>")
    for row in rows:
        rang = str(row[0]).strip() if len(row) > 0 else ""
        tr_cls = ' class="leader"' if rang == "1" else ""
        lines.append(f"<tr{tr_cls}>")
        for i in range(ncols):
            val = _esc(row[i]) if i < len(row) else ""
            css_classes = []
            if i in right_cols:
                css_classes.append("num")
            if i == 2:
                css_classes.append("name")
            elif i == 3:
                css_classes.append("club")
            elif i == vbd_col:
                css_classes.append("col-vbd")
            cls = f' class="{" ".join(css_classes)}"' if css_classes else ""
            lines.append(f"<td{cls}>{val}</td>")
        lines.append("</tr>")
    lines.append("</tbody></table></div>")
    return "\n".join(lines)


def write_html(path, html_content, log):
    """
    Schreibt die HTML-Datei atomar und verifiziert das Ergebnis.
    Gibt (erfolg: bool, diagnose: str) zurueck.
    """
    target = os.path.join(path, "index.html")
    tmp = target + f".tmp.{os.getpid()}"

    # Pruefen ob die HTML-Daten Tabellenzeilen enthalten
    has_tbody = "<tbody>" in html_content
    html_size = len(html_content)

    try:
        # Schritt 1: In temporaere Datei schreiben
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html_content)
            f.flush()
            os.fsync(f.fileno())

        # Schritt 2: Temporaere Datei verifizieren (zuruecklesen)
        with open(tmp, "r", encoding="utf-8") as f:
            verify = f.read()
        if len(verify) != html_size:
            msg = f"Tmp-Datei hat falsche Groesse: {len(verify)} statt {html_size}"
            log.error(msg)
            os.remove(tmp)
            return False, msg
        if has_tbody and "<tbody>" not in verify:
            msg = "Tmp-Datei hat <tbody> verloren!"
            log.error(msg)
            os.remove(tmp)
            return False, msg

        # Schritt 3: Atomar umbenennen
        os.replace(tmp, target)

        # Schritt 4: Zieldatei verifizieren
        with open(target, "r", encoding="utf-8") as f:
            final = f.read()
        if len(final) != html_size:
            msg = (f"Zieldatei hat falsche Groesse nach replace: "
                   f"{len(final)} statt {html_size}")
            log.error(msg)
            return False, msg
        if has_tbody and "<tbody>" not in final:
            msg = "Zieldatei hat <tbody> verloren nach replace!"
            log.error(msg)
            return False, msg

        return True, f"{target}: {html_size} Bytes, tbody={has_tbody}"

    except OSError as e:
        msg = f"Dateifehler: {e}"
        log.error(msg)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, msg


# =============================================================================
# FastAPI Application
# =============================================================================

def create_app(state, token, web_path, log):
    """
    Erstellt die FastAPI-App mit drei Endpunkten:
      POST /api/update  - Ergebnis-Updates empfangen (Auth noetig)
      GET  /api/health  - Serverstatus (Auth noetig)
      GET  /api/status  - Kurzstatus fuer Diagnose (ohne Auth)
    """
    try:
        from fastapi import FastAPI, Request, HTTPException
        from fastapi.responses import PlainTextResponse
    except ImportError:
        print("FEHLER: FastAPI nicht installiert.")
        print("  pip install fastapi uvicorn")
        sys.exit(1)

    MAX_BODY_SIZE = 2 * 1024 * 1024
    MAX_COLS = 10
    MAX_CELL_LEN = 200
    MAX_ROWS = 500

    app = FastAPI(
        title="WODA Server", version="1.0.0",
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    def _check_auth(request: Request):
        """Token-Pruefung (timing-sicher)."""
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
            client_ip = request.client.host if request.client else "?"
            log.warn(f"Ungueltiges Token von {client_ip}")
            raise HTTPException(status_code=401, detail="Ungueltiges API-Token")

    def _validate(data):
        """Validiert und bereinigt Eingabedaten."""
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

        clean_rows = []
        for row in rows:
            if not isinstance(row, list):
                continue
            clean_rows.append([
                str(val)[:MAX_CELL_LEN] if val is not None else ""
                for val in row[:MAX_COLS]
            ])

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

    # --- Ergebnis-Update empfangen ---
    @app.post("/api/update")
    async def receive_update(request: Request):
        _check_auth(request)

        # Body lesen
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

        # === PIPELINE: Validieren -> Speichern -> HTML -> Datei ===

        # Schritt 1: Validieren
        clean = _validate(data)
        cat_name = clean["category"] or "(leer)"
        row_count = len(clean["rows"])

        log.debug(f"Empfangen: '{cat_name}' mit {row_count} Zeilen, "
                  f"{len(body)} Bytes JSON")

        # Schritt 2: Speichern
        stored, store_info = state.apply_update(clean)
        log.debug(f"Speicherung: {store_info}")

        # Schritt 3: State auslesen
        state_data = state.get_all()
        cat_count = len(state_data["categories"])
        total_rows = sum(c["count"] for c in state_data["categories"])

        log.debug(f"State: {cat_count} Kategorien, {total_rows} Zeilen total")

        # Schritt 4: HTML erzeugen
        html, html_diag = generate_html(state_data)
        log.debug(html_diag)

        # Schritt 5: Datei schreiben + verifizieren
        write_ok, write_info = write_html(web_path, html, log)
        log.debug(f"Datei: {write_info}")

        # === Zusammenfassung im Live-Log ===
        if stored and write_ok:
            log.update(f"#{state_data['update_count']}: {cat_name} "
                       f"({row_count} Teilnehmer) -> HTML "
                       f"({cat_count} Kat., {len(html)} Bytes)")
        elif stored and not write_ok:
            log.error(f"Update #{state_data['update_count']}: Daten gespeichert, "
                      f"aber HTML-Datei konnte nicht geschrieben werden!")
        elif not stored:
            log.warn(f"Update #{state_data['update_count']}: {store_info}")

        return {
            "status": "ok",
            "update_count": state_data["update_count"],
            "categories": cat_count,
            "stored": stored,
            "html_written": write_ok,
        }

    # --- Serverstatus (Auth noetig) ---
    @app.get("/api/health")
    async def health(request: Request):
        _check_auth(request)
        state_data = state.get_all()
        return {
            "status": "ok",
            "version": "1.0.0",
            "update_count": state_data["update_count"],
            "categories": len(state_data["categories"]),
            "last_updated": state_data["last_updated"],
        }

    # --- Kurzstatus fuer Diagnose (ohne Auth) ---
    @app.get("/api/status")
    async def status():
        """
        Zeigt den aktuellen Serverstatus als Klartext.
        Kein Auth noetig - enthaelt keine sensiblen Daten.
        Hilft bei Fehlersuche: Sind die Daten im Server angekommen?
        """
        summary = state.get_summary()
        html_path = os.path.join(web_path, "index.html")
        try:
            html_size = os.path.getsize(html_path)
            with open(html_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            has_tbody = "<tbody>" in html_content
            tr_count = html_content.count("<tr")
        except OSError:
            html_size = 0
            has_tbody = False
            tr_count = 0

        text = (
            f"WODA Server Status\n"
            f"==================\n"
            f"{summary}\n"
            f"\n"
            f"HTML-Datei: {html_path}\n"
            f"  Groesse: {html_size} Bytes\n"
            f"  <tbody> vorhanden: {has_tbody}\n"
            f"  <tr> Elemente: {tr_count}\n"
        )
        return PlainTextResponse(text)

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

Diagnose:
  Waehrend der Server laeuft, zeigt http://SERVER:PORT/api/status
  den aktuellen Zustand als Klartext (ohne Auth).

{DISCLAIMER}
""",
    )

    p.add_argument("--path", required=True,
                   help="Pfad zum Webserver-Verzeichnis")
    p.add_argument("--port", type=int, default=8443,
                   help="Port fuer die API (default: 8443)")
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind-Adresse (default: 0.0.0.0)")
    p.add_argument("--token", default=os.environ.get("WODA_API_TOKEN", ""),
                   help="API-Token (oder WODA_API_TOKEN, sonst automatisch)")

    args = p.parse_args()

    # Abhaengigkeiten pruefen
    try:
        import uvicorn
    except ImportError:
        print("FEHLER: uvicorn nicht installiert.")
        print("  pip install fastapi uvicorn")
        sys.exit(1)

    # Pfad pruefen
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
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
    except OSError as e:
        print(f"FEHLER: Keine Schreibrechte in {web_path}: {e}")
        sys.exit(1)

    # Token
    token = args.token or secrets.token_urlsafe(32)

    # Initialisierung
    log = LiveLog()
    state = RaceState()

    # Initiale leere HTML-Seite erzeugen
    init_html, _ = generate_html(state.get_all())
    write_ok, write_info = write_html(web_path, init_html, log)
    if not write_ok:
        print(f"FEHLER: Initiale HTML-Datei konnte nicht geschrieben werden")
        sys.exit(1)

    app = create_app(state, token, web_path, log)

    # Startbanner
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
    print(f"  Status-URL:   http://localhost:{args.port}/api/status")
    print(f"  (zeigt Serverstatus ohne Auth, fuer Diagnose)")
    print()
    print(f"  Dieses Token im WODA Client als --api-token verwenden.")
    print()
    print("-" * 60)
    print(f"  {DISCLAIMER}")
    print("-" * 60)
    print()
    print("  Live-Log:")
    print()

    # Server starten
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        print("\nServer beendet.")


if __name__ == "__main__":
    main()
