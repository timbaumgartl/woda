# WODA – Winlaufen Online Data Addon

Live-Ergebnisse aus [Winlaufen](https://www.winlaufen.de/) auf einer Webseite anzeigen.

WODA liest die Daten der SprecherPC-Schnittstelle von Winlaufen und erzeugt daraus eine HTML-Seite, die ein vorhandener Webserver (Caddy, nginx, Apache) an die Besucher ausliefert. So können Zuschauer die Ergebnisse live auf dem Smartphone verfolgen.

> **Hinweis:** Dieses Programm ist ein unabhängiges Open-Source-Projekt und steht in keiner Verbindung zu Winlaufen oder dessen Entwicklern. Es wird ohne Support, Garantie oder Gewährleistung bereitgestellt.

## Funktionsumfang

- **Langlauf** – 7 Spalten: Rang, StNr, Name, Verein, Vbd, Laufzeit, Rückstand
- **Biathlon** – 8 Spalten: zusätzlich Schießen
- **Auswertungsmodi** – Altersklassen, Kategorien (Herren/Damen), klassenunabhängig
- **Mehrkategorie-Anzeige** – Alle Altersklassen auf einer Seite, automatische Akkumulation
- **Auto-Refresh** – Browser aktualisiert alle 30 Sekunden
- **Responsive Design** – Optimiert für Desktop und Smartphone (Waldgrün-Farbschema)
- **Zuverlässige Übertragung** – Queue-basierter Sender mit automatischem Retry

## Architektur

```
┌─────────────┐   TCP 4444    ┌─────────────┐   HTTP/S    ┌─────────────┐
│             │──────────────>│ WODA Client │────────────>│ WODA Server │
│  Winlaufen  │  Java Object  │             │  REST API   │             │
│             │    Stream     │ woda_client │  (JSON)     │ woda_server │
└─────────────┘               │     .py     │             │    .py      │
                              └─────────────┘             └──────┬──────┘
                                                                 │
                                                           index.html
                                                                 │
                                                          ┌──────▼──────┐
                                                          │  Webserver  │
                                                          │  (Caddy,    │
                                                          │   nginx)    │
                                                          └──────┬──────┘
                                                                 │
                                                          ┌──────▼──────┐
                                                          │  Zuschauer  │
                                                          │  (Browser)  │
                                                          └─────────────┘
```

**WODA Client** (`woda_client.py`) läuft im gleichen Netzwerk wie der Winlaufen-PC. Er verbindet sich über TCP Port 4444 (SprecherPC-Schnittstelle), parst den Java-ObjectStream und sendet die Ergebnisse per REST-API an den WODA Server. Der Client kann auf dem selben Gerät wie Winlaufen selbst betrieben werden, muss es aber nicht. Es gelten die gleichen Vorrausetzungen wie für den SprecherPC.

**WODA Server** (`woda_server.py`) läuft auf dem Webserver. Er empfängt die Daten, speichert sie pro Kategorie und erzeugt eine statische `index.html`, die ein vorhandener Webserver ausliefert.

## Voraussetzungen

| Komponente  | Anforderung                                                     |
|-------------|-----------------------------------------------------------------|
| WODA Client | Python 3.6+ (nur Standardbibliothek)                            |
| WODA Server | Python 3.6+, `fastapi`, `uvicorn`                               |
| Webserver   | Caddy, nginx, Apache o.ä. (bereits vorhanden)                   |
| Netzwerk    | Client muss Winlaufen-PC (TCP 4444) und Server (HTTP) erreichen |

## Installation

### WODA Server (auf dem Webserver)

```bash
# Abhängigkeiten installieren (bsp. Debian)
apt install python3-fastapi

# Verzeichnis für die HTML-Datei anlegen (im Webserver Verzeichnis)
mkdir -p /var/www/ergebnisse

# Server starten
python3 ./woda_server.py --path /var/www/ergebnisse
```

Der Server gibt beim Start ein API-Token aus. Dieses Token wird im Client benötigt.

### WODA Client (im Netzwerk des Winlaufen-PC)

```cmd
# Python 3 auf Windows installieren
https://www.python.org/downloads/windows/
# Client starten
python woda_client.py --api-endpoint http://mein-web-server.tld
```

### Webserver-Konfiguration

Der Webserver muss das Verzeichnis mit der `index.html` ausliefern. Beispiel für **Caddy**:

```
mein-web-server.tld {
    root * /var/www/ergebnisse
    file_server
}
```

Beispiel für **nginx**:

```nginx
server {
    listen 80;
    server_name mein-web-server.tld;
    root /var/www/ergebnisse;
    index index.html;
}
```

## Nutzung

### Server starten

```bash
python3 woda_server.py --path /var/www/ergebnisse [--port 8443] [--token MEIN_TOKEN]
```

| Parameter | Standard      | Beschreibung                                        |
|-----------|---------------|-----------------------------------------------------|
| `--path`  | (pflicht)     | Pfad zum Webserver-Verzeichnis                      |
| `--port`  | 8443          | API-Port                                            |
| `--host`  | 0.0.0.0       | Bind-Adresse                                        |
| `--token` | (automatisch) | API-Token (oder Umgebungsvariable `WODA_API_TOKEN`) |

Der Server zeigt ein **Live-Log** in der Konsole:

```
  Live-Log:

  [17:58:21] #1: Herren 21 (4 Teilnehmer) -> HTML (1 Kat., 5122 Bytes)
  [17:58:25] #2: Herren 21 (5 Teilnehmer) -> HTML (1 Kat., 5890 Bytes)
  [17:58:40] #9: Damen 21 (1 Teilnehmer) -> HTML (2 Kat., 6230 Bytes)
```

**Diagnose-Endpunkt:** Während der Server läuft, zeigt `http://SERVER:PORT/api/status` den aktuellen Zustand als Klartext (ohne Authentifizierung).

### Client starten

```bash
python3 woda_client.py --winlaufen-pc IP_ADRESSE [--api-endpoint SERVER] [--api-token TOKEN]
```

| Parameter          | Standard   | Beschreibung                        |
|--------------------|------------|-------------------------------------|
| `--winlaufen-pc`   | 127.0.0.1  | IP-Adresse des Winlaufen-PC         |
| `--winlaufen-port` | 4444       | SprecherPC-Port                     |
| `--api-endpoint`   | (optional) | Server-Adresse (IP oder Domain)     |
| `--api-port`       | 8443       | Server-Port                         |
| `--api-token`      | (optional) | API-Token (oder `WODA_API_TOKEN`)   |
| `--api-test`       | –          | Nur Verbindung testen, dann beenden |
| `--debug`          | –          | Detaillierte Protokoll-Ausgabe      |

Der Client zeigt die Ergebnisse im Terminal an und sendet sie gleichzeitig an den Server.

### Verbindungstest

```bash
# Nur API-Verbindung testen
python3 woda_client.py --api-endpoint http://mein-web-server.tld --api-token TOKEN --api-test
```

### HTTPS (Reverse Proxy)

Wenn der Server hinter einem Reverse-Proxy mit SSL läuft:

```bash
python3 woda_client.py --api-endpoint https://mein-web-server.tld --api-token TOKEN
```

Bei `https://`-Prefix wird SSL für die API-Verbindung verwendet. Ohne Prefix wird `http://` angenommen (Standard für uvicorn ohne SSL).

## Übertragung (Queue-basiert)

Der Client verwendet eine Queue mit maximal einem Element. Bei schnellen Updates wird immer nur das neueste gesendet – ältere werden verworfen:

```
Winlaufen sendet:  U1  U2  U3  U4  U5
                    │   │   │   │   │
Queue (max 1):     [U1] │   │   │   │
                    │  [U3] │   │   │   ← U2 verworfen (U3 ist neuer)
                    │   │  [U5] │   │   ← U4 verworfen (U5 ist neuer)
                    │   │   │
Server empfängt:   U1  U3  U5
```

Fehlgeschlagene Sendungen werden bis zu 3× mit 0,5 Sekunden Pause wiederholt.

## Protokoll (SprecherPC-Schnittstelle)

WODA nutzt die SprecherPC-LAN-Schnittstelle von Winlaufen. Die Daten kommen als Java ObjectOutputStream über TCP Port 4444.

### Stream-Aufbau

Der Stream enthält drei Datentypen:

1. **Uhrzeit-Strings** – `"Uhr19:31:08"` (jede Sekunde)
2. **Update-Blöcke** – Ergebnisse mit Wettkampfname, Kategorie, Tabellenzeilen
3. **Nachrichten** – Java-Objekte (Vektoren) für Server-Nachrichten

### Update-Block Struktur

| #  | Typ      | Inhalt                                                                |
|----|----------|-----------------------------------------------------------------------|
| 0  | String   | Wettkampfart (z.B. "Standardwettkampf")                               |
| 1  | Integer  | Auswertungsmodus (0=klassenunabhängig, 1=Altersklassen, 2=Kategorien) |
| 2  | Integer  | Anzahl Klassen                                                        |
| 3  | String[] | Klassenbezeichnungen (z.B. ["Herren 21", "Damen 21"])                 |
| 4  | int[]    | Rundenzahl / Teamgröße                                                |
| 5  | Integer  | Position WinSpringen                                                  |
| 6  | Integer  | Sprecher-Nr. (Index in Klassenbezeichnungen = aktuelle Kategorie)     |
| 7  | Integer  | Runde / Durchgang                                                     |
| 8  | Integer  | Aktueller Zieleinlauf                                                 |
| 9+ | String[] | Ergebniszeilen (je 7 oder 8 Spalten)                                  |
| –  | String   | `"tabelle"` (Ende Tabellendaten)                                      |
| –  | String[] | Spaltenüberschriften                                                  |
| –  | String   | `"ende"` (Ende Wettkampfdaten)                                        |

### Spalten

**Langlauf** (7 Spalten): Rang, StNr, Name/Vorname, Verein, Vbd, Laufzeit, Rückstand

**Biathlon** (8 Spalten): Rang, StNr, Name/Vorname, Verein, Vbd, Schießen, Gesamtzeit, Rückstand

Die Spaltenanzahl wird automatisch aus den Tabellenüberschriften des Streams erkannt.

## Sicherheit

- **API-Token** – Jede API-Anfrage muss ein Bearer-Token im Authorization-Header senden
- **Timing-sichere Prüfung** – Token-Vergleich mit `hmac.compare_digest`
- **Eingabevalidierung** – Max. 500 Zeilen, 10 Spalten, 200 Zeichen pro Zelle, 2 MB Body
- **HTML-Escaping** – Alle Benutzerdaten werden escaped (`<>&"'`)
- **Atomare Dateischreibung** – `write` → `fsync` → `rename` (Besucher sehen nie halbe Dateien)
- **Pfad-Validierung** – Systemverzeichnisse blockiert, Schreibrechte geprüft
- **Keine API-Dokumentation** – OpenAPI/Swagger deaktiviert
- **Parser-Schutz** – Rekursionstiefe (50), Max. Array-Größe (10.000), Max. String (1 MB)

## Fehlerbehebung

| Symptom                                 | Ursache                                                    | Lösung                                     |
|-----------------------------------------|------------------------------------------------------------|--------------------------------------------|
| Client: "Verbindung fehlgeschlagen"     | Winlaufen nicht gestartet oder falscher Port               | Winlaufen starten, IP/Port prüfen          |
| Server: "Invalid HTTP request received" | Client sendet HTTP, Server erwartet HTTPS (oder umgekehrt) | Schema prüfen: `http://` vs `https://`     |
| Server: "Ungueltiges API-Token"         | Token stimmt nicht überein                                 | Token von Server-Ausgabe kopieren          |
| HTML ohne Ergebnisse                    | Alte Client-Version ohne Uhr-String-Behandlung             | Neuste Version verwenden                   |
| 401 Unauthorized                        | Falsches oder fehlendes Token                              | `--api-token` oder `WODA_API_TOKEN` setzen |

**Detaillierte Diagnose:** Server mit `--debug`-Level starten und `/api/status` im Browser aufrufen. Der Status-Endpunkt zeigt den aktuellen Speicherzustand und die HTML-Datei-Integrität.

## Umgebungsvariablen

| Variable         | Beschreibung                                         |
|------------------|------------------------------------------------------|
| `WODA_API_TOKEN` | API-Token (Alternative zu `--token` / `--api-token`) |

## Beispiel: Kompletter Aufbau

```bash
# 1. Server starten (auf dem Webserver)
python3 woda_server.py --path /var/www/ergebnisse --port 8443
#    → gibt API-Token aus, z.B. "abc123..."

# 2. Client starten (im Wettkampfnetzwerk)
python3 woda_client.py \
    --winlaufen-pc 127.0.0.1 \
    --api-endpoint http://mein-web-server.tld \
    --api-port 8443 \
    --api-token TOKEN

# 3. Webserver-Konfiguration (Caddy als Reverse Proxy für API + Datei-Server)
# Caddyfile:
# mein-web-server.tld {
#     handle /api/* {
#         reverse_proxy localhost:8443
#     }
#     handle {
#         root * /var/www/ergebnisse
#         file_server
#     }
# }
```

## Technische Details

- **Sprache:** Python 3.6+ (Client: nur Standardbibliothek; Server: FastAPI + uvicorn)
- **Parser:** Vollständiger Java ObjectOutputStream Parser (TC_OBJECT, TC_ARRAY, TC_STRING, TC_REFERENCE, TC_RESET, etc.)
- **Stream-Neustarts:** Der Parser erkennt 0xACED-Header mitten im Stream (Modus-Wechsel in Winlaufen)
- **Mehrkategorie:** Server akkumuliert alle Kategorien – abgeschlossene bleiben sichtbar
- **HTML:** Statisch, kein JavaScript (außer Meta-Refresh), funktioniert offline
- **Kodierung:** Umlaute (ä, ö, ü, ß) werden korrekt über UTF-8 übertragen und dargestellt

## Erstellt mit

Dieses Projekt wurde erstellt mit [Claude Opus 4.6](https://www.anthropic.com/claude) (`claude-opus-4-6`) von [Anthropic](https://www.anthropic.com/), Februar 2025.

## Lizenz

Dieses Programm ist ein unabhängiges Open-Source-Projekt und steht in keiner Verbindung zu Winlaufen oder dessen Entwicklern. Es wird ohne Support, Garantie oder Gewährleistung bereitgestellt. Nutzung auf eigene Verantwortung. Zur Verwendung von Winlaufen wird eine Lizenz von http://www.winlaufen.de/ benötigt.
