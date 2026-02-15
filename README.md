# Winlaufen Online Data Addon (WODA)

Dieses Programm ist ein unabhaengiges Open-Source-Projekt und steht in
keiner Verbindung zu Winlaufen oder dessen Entwicklern. Es wird ohne
Support, Garantie oder Gewaehrleistung bereitgestellt. Die Nutzung
erfolgt auf eigene Verantwortung. Winlaufen ist ein eingetragenes
Produkt seiner jeweiligen Rechteinhaber.

Erstellt von Claude (Anthropic) im Auftrag des Nutzers. Das
Kommunikationsprotokoll wurde durch Analyse von Netzwerkmitschnitten
(pcapng) reverse-engineered.

---

## Uebersicht

WODA besteht aus zwei Programmen die auf verschiedenen Rechnern laufen:

| Komponente | Datei | Laeuft auf | Aufgabe |
|---|---|---|---|
| **WODA Client** | `woda_client.py` | Rechner mit Winlaufen | Liest Ergebnisse, sendet sie weiter |
| **WODA Server** | `woda_server.py` | Server im Internet | Empfaengt Daten, erzeugt Webseite |

**Datenfluss:**

```
Winlaufen (Port 4444)
    |
    v  TCP (Java ObjectStream)
WODA Client
    |
    v  HTTP/HTTPS (JSON)
WODA Server
    |
    v  Statische HTML-Datei
Webserver (Caddy/nginx)
    |
    v
Browser (Smartphone/Tablet/PC)
```

---

## Voraussetzungen

- Python 3.6+ auf beiden Rechnern
- Auf dem Server: `pip install fastapi uvicorn`
- Auf dem Client: Keine zusaetzlichen Pakete (nur Python-Standardbibliothek)
- Netzwerkverbindung vom Client zum Winlaufen-PC (Port 4444, TCP)
- Netzwerkverbindung vom Client zum Server (Port 8443 oder konfiguriert)

---

## Schnellstart

### 1. Server starten

```bash
# Verzeichnis anlegen
sudo mkdir -p /var/www/ergebnisse
sudo chown $USER:$USER /var/www/ergebnisse

# FastAPI installieren
pip install fastapi uvicorn

# Server starten
python3 woda_server.py --path /var/www/ergebnisse
```

Der Server zeigt beim Start das API-Token an. Dieses Token wird im
Client als `--api-token` benoetigt.

Der Server zeigt ein **Live-Log** in der Konsole an. Jedes empfangene
Update wird dort mit Zeitstempel, Kategorie und Teilnehmerzahl
protokolliert.

### 2. Client starten

```bash
# Nur Konsolenanzeige (zum Testen)
python3 woda_client.py --winlaufen-pc 192.168.0.100

# Mit WODA Server
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --api-endpoint mein-server.de \
    --api-token DAS_ANGEZEIGTE_TOKEN
```

### 3. Ergebnisse anschauen

Im Browser die Adresse des Webservers oeffnen. Die Seite aktualisiert
sich automatisch alle 30 Sekunden.

### 4. Beenden

- Client: Enter druecken
- Server: Ctrl+C

Die HTML-Datei bleibt im Webserver-Verzeichnis als statisches
Endergebnis.

---

## Webserver einrichten

Der WODA Server erzeugt statische HTML-Dateien. Ein separater
Webserver liefert diese an die Besucher aus.

### Caddy (empfohlen)

Caddy besorgt automatisch ein TLS-Zertifikat und braucht fast keine
Konfiguration. Voraussetzung: DNS-Eintrag auf die Server-IP.

```bash
# Installieren (Debian/Ubuntu)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

Caddyfile (`/etc/caddy/Caddyfile`):
```
ergebnisse.mein-verein.de {
    root * /var/www/ergebnisse
    file_server
}
```

```bash
sudo systemctl reload caddy
sudo ufw allow 80
sudo ufw allow 443
sudo ufw allow 8443
```

### nginx

```bash
sudo apt install nginx
```

nginx-Konfiguration (`/etc/nginx/sites-available/ergebnisse`):
```
server {
    listen 80;
    server_name ergebnisse.mein-verein.de;
    root /var/www/ergebnisse;
    index index.html;
    location / { try_files $uri $uri/ =404; }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/ergebnisse /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Optional: TLS mit Certbot
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d ergebnisse.mein-verein.de
```

### Python (nur zum Testen)

```bash
mkdir -p /tmp/ergebnisse
python3 woda_server.py --path /tmp/ergebnisse &
cd /tmp/ergebnisse && python3 -m http.server 8080
# Browser: http://localhost:8080
```

### Firewall

Der API-Port (default 8443) muss vom Client erreichbar sein:

```bash
sudo ufw allow 8443/tcp
```

---

## WODA Client - Referenz

### Parameter

| Parameter | Beschreibung | Standard |
|---|---|---|
| `--winlaufen-pc` | IP / Hostname des Winlaufen-PC | 127.0.0.1 |
| `--port` | Port des Winlaufen Sprecher-PC | 4444 |
| `--api-endpoint` | IP / Hostname des WODA Servers | - |
| `--api-port` | Port der WODA Server API | 8443 |
| `--api-token` | API-Token (oder `WODA_API_TOKEN`) | - |
| `--api-test` | Verbindung testen und beenden | - |
| `--debug` / `-d` | Debug-Ausgabe | aus |

### Umgebungsvariablen

Tokens koennen ueber Umgebungsvariablen gesetzt werden (nicht in
der Prozessliste sichtbar):

```bash
export WODA_API_TOKEN="mein-token"
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --api-endpoint mein-server.de
```

### HTTP vs. HTTPS

```bash
# HTTP (Standard, z.B. im lokalen Netz)
--api-endpoint 192.168.0.200

# HTTPS (empfohlen fuer oeffentliche Server)
--api-endpoint https://mein-server.de
```

Der Client zeigt einen Hinweis wenn HTTP verwendet wird.

### Zuverlaessige Uebertragung

Der Client verwendet eine Warteschlange (Queue) mit einem einzigen
Sender-Thread. Das loest folgende Probleme:

- **Kein Datenstau:** Winlaufen sendet schnelle Updates, der Client
  sendet immer nur den aktuellsten Stand
- **Automatischer Retry:** Bei Netzwerkfehlern wird bis zu 3x
  wiederholt
- **Nicht blockierend:** Die Konsolenanzeige bleibt fluessig auch
  wenn der Server kurz nicht erreichbar ist

---

## WODA Server - Referenz

### Parameter

| Parameter | Beschreibung | Standard |
|---|---|---|
| `--path` | Pfad zum Webserver-Verzeichnis (erforderlich) | - |
| `--port` | Port fuer die API | 8443 |
| `--host` | Bind-Adresse | 0.0.0.0 |
| `--token` | API-Token (oder `WODA_API_TOKEN`, sonst auto) | zufaellig |
| `--debug` / `-d` | Ausfuehrliches HTTP-Log | aus |

### API-Endpunkte

| Methode | Pfad | Beschreibung |
|---|---|---|
| POST | `/api/update` | Ergebnis-Update vom Client |
| GET | `/api/health` | Serverstatus pruefen |

Beide Endpunkte erfordern `Authorization: Bearer TOKEN`.

### Live-Log

Der Server zeigt in der Konsole jedes empfangene Update an:

```
  [14:32:15] Update #1: U16 w (12 Teilnehmer)
  [14:32:18] Update #2: U16 w (14 Teilnehmer)
  [14:33:01] Update #3: U16 m (8 Teilnehmer)
  [14:33:45] Update #4: Herren (23 Teilnehmer)
```

Warnungen (z.B. ungueltige Tokens) werden gelb, Fehler rot angezeigt.

### Webseite

- Nur empfangene Daten werden angezeigt
- Alle Kategorien untereinander, abgeschlossene bleiben sichtbar
- Auto-Refresh alle 30 Sekunden
- Responsive (Smartphone, Tablet, PC)
- Farbschema: Waldgruen

---

## Sicherheit

### Was geschuetzt ist

- **Token-Auth:** Kryptographisch sicheres Token (32 Bytes),
  timing-sichere Pruefung (`hmac.compare_digest`)
- **Eingabevalidierung:** Max 500 Zeilen, 10 Spalten, 200 Zeichen
  pro Wert, 2 MB Request-Groesse
- **XSS-Schutz:** Alle Daten werden HTML-escaped (5 Zeichen)
- **Atomare Datei-Schreibung:** Temp-Datei + Rename
- **API-Docs deaktiviert:** Kein Swagger/OpenAPI exponiert
- **Pfad-Pruefung:** Symlinks aufgeloest, Systemverzeichnisse blockiert

### Was bewusst entfernt wurde

- **Rate-Limiting:** Wurde entfernt weil es im Praxiseinsatz
  legitime Updates blockiert hat. Die API ist per Token geschuetzt
  und wird nur von einem Client befuellt.

### Empfehlungen

- HTTPS fuer oeffentliche Server verwenden (Caddy macht das automatisch)
- API-Port (8443) nur fuer den Client freigeben
- Token ueber Umgebungsvariable setzen statt in der Kommandozeile
- Nach dem Wettkampf den Server stoppen

---

## Fehlerbehebung

### Client verbindet sich nicht mit Winlaufen

- Winlaufen gestartet? Sprecher-PC Modus aktiv?
- IP-Adresse korrekt? (`--winlaufen-pc`)
- Port 4444 erreichbar? (Firewall pruefen)

### Client verbindet sich nicht mit WODA Server

- Server gestartet? Token korrekt?
- Port 8443 in der Server-Firewall offen?
- `--api-test` verwenden um die Verbindung zu pruefen
- "Invalid HTTP request received": Schema pruefen (`http://` vs `https://`)

### Webseite zeigt keine Ergebnisse

- Schreibrechte auf `--path` Verzeichnis?
- `index.html` im Verzeichnis vorhanden?
- Webserver (Caddy/nginx) konfiguriert?
- Browser-Cache leeren (Ctrl+Shift+R)

### Server Live-Log zeigt keine Updates

- Kein Eintrag im Log: Client sendet nicht (Verbindungsproblem)
- Updates im Log aber Webseite leer: Schreibrechte pruefen
- `--debug` am Server aktivieren fuer ausfuehrliches HTTP-Log

---

## Technisches

### Winlaufen-Protokoll

TCP Port 4444, Java ObjectOutputStream. Jeder Block enthaelt 13 Objekte:
Wettkampfname, Anzeigemodus, Kategorienamen, Teilnehmerzahlen,
Kategorie-Index, Ergebniszeilen (7 Spalten), Spaltenueberschriften,
Ende-Marker.

### Client -> Server Payload

```json
{
    "category": "U16 w",
    "rows": [["1", "25", "FISCHER Lukas", "SC Ort", "BSV", "27:30", "00:00"]],
    "headers": ["Rang", "StNr", "Name, Vorname", "Verein", "Vbd", "Laufzeit", "Rueckstand"],
    "timestamp": "2026-02-15T14:30:00"
}
```

### Warteschlangen-Prinzip

```
Winlaufen sendet Updates:  [U1] [U2] [U3] [U4] [U5]
                              |    |    |    |    |
Queue (max 1):             [U1]------>  [U3]  [U5]-->
                              |           |       |
Server empfaengt:          [U1]        [U3]    [U5]
```

U2 und U4 werden uebersprungen weil U3 bzw. U5 den aktuelleren
Stand enthalten. Der Server braucht nur den jeweils neuesten Stand.
