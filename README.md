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

WODA besteht aus zwei Komponenten die unabhaengig voneinander auf
verschiedenen Rechnern laufen:

| Komponente | Datei | Laeuft auf | Aufgabe |
|---|---|---|---|
| **WODA Client** | `woda_client.py` | Rechner mit Winlaufen | Empfaengt Ergebnisse, leitet sie weiter |
| **WODA Server** | `woda_server.py` | Server im Internet | Empfaengt Daten, generiert Webseite |

**Datenfluss:**

```
Winlaufen (Port 4444)
    |
    v  (Java ObjectStream, TCP)
WODA Client
    |               |
    v               v
WODA Server     Telegram Bot
    |
    v
Webserver (Caddy/nginx)
    |
    v
Browser (Smartphone/Tablet/PC)
```

Der WODA Client kann Ergebnisse gleichzeitig an den WODA Server und
an Telegram senden. Beide Ausgabeoptionen sind optional und koennen
einzeln oder kombiniert aktiviert werden.

---

## Voraussetzungen

- Python 3.6+ auf beiden Rechnern
- Auf dem Server: `pip install fastapi uvicorn`
- Auf dem Client: Keine zusaetzlichen Pakete (Python-Standardbibliothek)
- Netzwerkverbindung vom Client zum Winlaufen-PC (Port 4444, TCP)
- Netzwerkverbindung vom Client zum Server (Port 8443 oder konfiguriert)
- Fuer Telegram: Bot-Token von @BotFather

---

## Schnellstart

### 1. Server einrichten

Auf dem Server (z.B. ein VPS bei Hetzner, Netcup, etc.):

```bash
# 1. Verzeichnis fuer die Webseite anlegen
sudo mkdir -p /var/www/ergebnisse
sudo chown $USER:$USER /var/www/ergebnisse

# 2. Webserver installieren und einrichten (siehe Abschnitt unten)

# 3. FastAPI installieren
pip install fastapi uvicorn

# 4. WODA Server starten
python3 woda_server.py --path /var/www/ergebnisse
```

Der Server zeigt beim Start das API-Token an. Dieses Token wird fuer
den Client benoetigt.

### 2. Client starten

Auf dem Rechner mit Winlaufen:

```bash
# Nur Konsole (Test)
python3 woda_client.py --winlaufen-pc 192.168.0.100

# Mit WODA Server
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --api-endpoint mein-server.de \
    --api-token DAS_ANGEZEIGTE_TOKEN

# Mit Telegram
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --telegram-token "123456:ABC..." \
    --telegram-chat-id "-100..."

# Server + Telegram gleichzeitig
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --api-endpoint mein-server.de \
    --api-token DAS_ANGEZEIGTE_TOKEN \
    --telegram-token "123456:ABC..." \
    --telegram-chat-id "-100..."
```

### 3. Ergebnisse anschauen

Im Browser die Adresse des Webservers oeffnen, z.B.
`https://mein-server.de/ergebnisse/`. Die Seite aktualisiert sich
automatisch alle 30 Sekunden.

### 4. Nach dem Wettkampf

Auf dem Server Ctrl+C druecken. Den Client mit Enter beenden.
Die HTML-Datei bleibt im Webserver-Verzeichnis liegen und kann
als statisches Endergebnis weiter bereitgestellt oder geloescht
werden.

---

## Webserver einrichten

Der WODA Server generiert statische HTML-Dateien. Ein separater
Webserver liefert diese an die Besucher aus. Die folgenden
Anleitungen beschreiben eine schnelle Einrichtung fuer den
temporaeren Einsatz waehrend eines Wettkampfs.

### Variante A: Caddy (empfohlen)

Caddy besorgt automatisch ein TLS-Zertifikat (Let's Encrypt) und
braucht fast keine Konfiguration.

**Voraussetzungen:** Ein Server mit oeffentlicher IP und ein
DNS-Eintrag (A-Record) der auf diese IP zeigt, z.B.
`ergebnisse.mein-verein.de`.

```bash
# 1. Caddy installieren (Debian/Ubuntu)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# 2. Verzeichnis anlegen
sudo mkdir -p /var/www/ergebnisse
sudo chown $USER:$USER /var/www/ergebnisse

# 3. Caddy konfigurieren
# /etc/caddy/Caddyfile:
#
#   ergebnisse.mein-verein.de {
#       root * /var/www/ergebnisse
#       file_server
#   }

sudo systemctl reload caddy

# 4. Firewall oeffnen (falls aktiv)
sudo ufw allow 80
sudo ufw allow 443
sudo ufw allow 8443
```

Caddy holt sich automatisch ein Zertifikat. Die Seite ist sofort
unter `https://ergebnisse.mein-verein.de` erreichbar.

### Variante B: nginx

```bash
# 1. nginx installieren
sudo apt install nginx

# 2. Verzeichnis anlegen
sudo mkdir -p /var/www/ergebnisse
sudo chown $USER:$USER /var/www/ergebnisse

# 3. Konfiguration anlegen
# /etc/nginx/sites-available/ergebnisse:
#
#   server {
#       listen 80;
#       server_name ergebnisse.mein-verein.de;
#       root /var/www/ergebnisse;
#       index index.html;
#
#       location / {
#           try_files $uri $uri/ =404;
#       }
#   }

sudo ln -s /etc/nginx/sites-available/ergebnisse /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 4. TLS mit Certbot (optional aber empfohlen)
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d ergebnisse.mein-verein.de

# 5. Firewall
sudo ufw allow 'Nginx Full'
sudo ufw allow 8443
```

### Variante C: Einfacher Python-Server (nur zum Testen)

Fuer lokale Tests ohne oeffentlichen Zugang:

```bash
mkdir -p /tmp/ergebnisse
cd /tmp/ergebnisse
python3 -m http.server 8080
```

In einem zweiten Terminal:
```bash
python3 woda_server.py --path /tmp/ergebnisse
```

Browser: `http://localhost:8080`

### Firewall fuer die API

Der WODA Client sendet Daten an Port 8443 (oder den mit `--port`
konfigurierten Port) des Servers. Dieser Port muss in der Firewall
geoeffnet sein:

```bash
# UFW (Ubuntu/Debian)
sudo ufw allow 8443/tcp

# firewalld (CentOS/RHEL)
sudo firewall-cmd --add-port=8443/tcp --permanent
sudo firewall-cmd --reload

# iptables
sudo iptables -A INPUT -p tcp --dport 8443 -j ACCEPT
```

---

## WODA Client - Vollstaendige Referenz

### Alle Parameter

| Parameter | Beschreibung | Standard |
|---|---|---|
| `--winlaufen-pc` | Hostname / DNS / IP des Winlaufen-PC | 127.0.0.1 |
| `--port` | Port des Winlaufen Sprecher-PC | 4444 |
| `--api-endpoint` | Hostname / IP des WODA Servers | - |
| `--api-port` | Port der WODA Server API | 8443 |
| `--api-token` | API-Token (oder `WODA_API_TOKEN`) | - |
| `--api-test` | Verbindung zum WODA Server testen | - |
| `--telegram-token` | Bot-Token (oder `WODA_TELEGRAM_TOKEN`) | - |
| `--telegram-chat-id` | Chat-ID (oder `WODA_TELEGRAM_CHAT_ID`) | - |
| `--telegram-test` | Telegram-Verbindung testen | - |
| `--debug` / `-d` | Debug-Ausgabe aktivieren | aus |

### Umgebungsvariablen

Alle sensiblen Parameter koennen ueber Umgebungsvariablen gesetzt
werden. Das vermeidet, dass Tokens in der Prozessliste (`ps aux`)
sichtbar sind.

| Variable | Entspricht |
|---|---|
| `WODA_API_TOKEN` | `--api-token` |
| `WODA_TELEGRAM_TOKEN` | `--telegram-token` |
| `WODA_TELEGRAM_CHAT_ID` | `--telegram-chat-id` |

Kommandozeilenargumente haben Vorrang vor Umgebungsvariablen.

Beispiel:
```bash
export WODA_API_TOKEN="mein-geheimes-token"
export WODA_TELEGRAM_TOKEN="123456:ABC..."
export WODA_TELEGRAM_CHAT_ID="-100..."
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --api-endpoint mein-server.de
```

### URL-Schema fuer die API-Verbindung

Der Client verwendet standardmaessig HTTP fuer die Verbindung zum
WODA Server. Das ist fuer lokale Netze oder Tests geeignet, aber
fuer den Einsatz ueber das Internet sollte HTTPS verwendet werden.

```bash
# HTTP (Standard, fuer lokale Netze / Tests)
--api-endpoint 192.168.0.50

# HTTPS (empfohlen fuer Internet)
--api-endpoint https://mein-server.de
```

Wenn der Client HTTP verwendet, zeigt er eine Warnung an:

```
WARNUNG: API-Verbindung laeuft ueber HTTP (unverschluesselt).
  Das API-Token wird im Klartext uebertragen.
  Fuer Produktiveinsatz HTTPS verwenden:
    --api-endpoint https://mein-server.de
```

### Verwendungsbeispiele

```bash
# Nur Konsolenanzeige (lokal auf dem Winlaufen-PC)
python3 woda_client.py --winlaufen-pc 192.168.0.100

# WODA Server Verbindung testen
python3 woda_client.py \
    --api-endpoint mein-server.de \
    --api-token "abc123" \
    --api-test

# Telegram-Verbindung testen
python3 woda_client.py \
    --telegram-token "123456:ABC..." \
    --telegram-test

# Nur WODA Server
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --api-endpoint mein-server.de \
    --api-token "abc123"

# Nur Telegram
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --telegram-token "123456:ABC..." \
    --telegram-chat-id "-100..."

# Server + Telegram gleichzeitig
python3 woda_client.py --winlaufen-pc 192.168.0.100 \
    --api-endpoint mein-server.de \
    --api-token "abc123" \
    --telegram-token "123456:ABC..." \
    --telegram-chat-id "-100..."
```

### Telegram einrichten

1. In Telegram nach `@BotFather` suchen
2. `/newbot` senden, Name und Benutzername vergeben
3. Bot-Token notieren
4. Dem Bot eine Nachricht senden
5. Im Browser oeffnen: `https://api.telegram.org/bot<TOKEN>/getUpdates`
6. Die Chat-ID steht unter `"chat":{"id":...}`
7. Fuer Gruppen beginnt die ID mit `-100`

Alternativ:
```bash
python3 woda_client.py --telegram-token "TOKEN" --telegram-test
```

### Grosse Teilnehmerfelder

Bei Auswertungen mit vielen Teilnehmern (z.B. 117 Laeufer in
"Alle Klassen") teilt der Client die Telegram-Nachricht automatisch
in mehrere Teile auf. Jeder Teil enthaelt einen Abschnitt der
Tabelle mit dem Hinweis `[1-40 / 117]` etc. Die Konsolenanzeige
zeigt immer die vollstaendige Tabelle.

### Beenden

Enter druecken beendet den Client sauber. Eine Abschlussmeldung
wird an Telegram gesendet.

---

## WODA Server - Vollstaendige Referenz

### Alle Parameter

| Parameter | Beschreibung | Standard |
|---|---|---|
| `--path` | Pfad zum Webserver-Verzeichnis (erforderlich) | - |
| `--port` | Port fuer die API-Schnittstelle | 8443 |
| `--host` | Bind-Adresse | 0.0.0.0 |
| `--token` | API-Token festlegen (oder `WODA_API_TOKEN`, sonst auto) | zufaellig |
| `--debug` / `-d` | Debug-Ausgabe aktivieren | aus |

### Verwendungsbeispiele

```bash
# Standard (Token wird generiert)
python3 woda_server.py --path /var/www/ergebnisse

# Eigener Port
python3 woda_server.py --path /var/www/ergebnisse --port 9000

# Festes Token (oder ueber WODA_API_TOKEN)
python3 woda_server.py --path /var/www/ergebnisse --token "mein-geheimes-token"

# Token ueber Umgebungsvariable
export WODA_API_TOKEN="mein-geheimes-token"
python3 woda_server.py --path /var/www/ergebnisse
```

### API-Endpunkte

| Methode | Pfad | Beschreibung | Auth |
|---|---|---|---|
| POST | `/api/update` | Ergebnis-Update vom Client | Ja |
| GET | `/api/health` | Serverstatus pruefen | Ja |

Authentifizierung erfolgt ueber den `Authorization: Bearer TOKEN`
Header.

### Was die Webseite anzeigt

- Nur Daten die tatsaechlich vom Client empfangen wurden
- Kein Wettkampf-Titel (nur "Live-Ergebnisse")
- Alle Altersklassen untereinander, abgeschlossene bleiben sichtbar
- Automatische Aktualisierung alle 30 Sekunden
- Responsive: funktioniert auf Smartphones, Tablets und PCs
- Farbschema: Waldgruen

### Beenden

Ctrl+C beendet den Server. Die zuletzt generierte `index.html`
bleibt im Webserver-Verzeichnis und zeigt den letzten Stand.

---

## Sicherheit

### Authentifizierung

Die Kommunikation zwischen Client und Server ist durch ein
API-Token geschuetzt. Ohne gueltiges Token werden alle Anfragen
mit HTTP 401 abgelehnt.

Der Token-Vergleich auf dem Server ist Timing-Attack-sicher
(`hmac.compare_digest`). Das Token wird beim Serverstart
automatisch generiert (43 Zeichen, kryptographisch zufaellig)
oder kann fest vorgegeben werden.

### Transport-Verschluesselung

uvicorn laeuft ohne SSL. Die API-Verbindung ist standardmaessig
unverschluesselt (HTTP). Das API-Token wird dabei im Klartext
uebertragen. Fuer den Einsatz ueber das Internet gibt es zwei
Moeglichkeiten:

1. **Reverse Proxy mit TLS** (empfohlen): Caddy oder nginx vor
   den WODA Server schalten und die API ueber HTTPS erreichbar
   machen. Der Client wird dann mit
   `--api-endpoint https://mein-server.de` aufgerufen.

2. **VPN/SSH-Tunnel**: Client und Server ueber einen verschluesselten
   Tunnel verbinden. Der Client verwendet dann `--api-endpoint localhost`.

Der Client zeigt eine Warnung an wenn die Verbindung ueber HTTP laeuft.

### Token-Verwaltung

Tokens koennen ueber Umgebungsvariablen gesetzt werden statt ueber
die Kommandozeile. Das verhindert, dass sie in der Prozessliste
(`ps aux`) oder der Shell-History sichtbar sind.

```bash
# Schlecht: Token in der Kommandozeile sichtbar
python3 woda_client.py --api-token "GEHEIM" ...

# Besser: Token ueber Umgebungsvariable
export WODA_API_TOKEN="GEHEIM"
python3 woda_client.py --api-endpoint mein-server.de ...
```

### Eingabe-Validierung (Server)

Der Server validiert alle empfangenen Daten:

| Schutz | Limit |
|---|---|
| Maximale Request-Groesse | 2 MB |
| Maximale Zeilen pro Update | 500 |
| Maximale Spalten pro Zeile | 10 |
| Maximale Zeichenlaenge pro Zelle | 200 |
| Maximale Kategorien gespeichert | 50 |
| Rate-Limit | 60 Anfragen / 60 Sekunden pro IP |

Unbekannte oder zu grosse Daten werden mit HTTP 400 oder 413
abgelehnt. Bei Ueberschreitung des Rate-Limits antwortet der
Server mit HTTP 429.

### XSS-Schutz

Alle dynamischen Inhalte in der generierten HTML-Seite werden
vollstaendig escaped (alle 5 relevanten Zeichen: `<`, `>`, `&`,
`"`, `'`). Dadurch ist die Webseite gegen Cross-Site-Scripting
geschuetzt, auch wenn ein Angreifer schaedliche Daten an die API
sendet.

### Parser-Limits (Client)

Der Java-Serialisierungs-Parser im Client hat Groessenlimits
um Speichererschoepfung durch fehlerhafte oder manipulierte
Datenstreams zu verhindern:

| Schutz | Limit |
|---|---|
| Einzelne Leseoperation | 10 MB |
| Array-Elemente | 10.000 |
| String-Laenge | 1 MB |
| Felder pro Java-Klasse | 500 |
| Handle-Referenzen | 100.000 |
| Annotationen pro Block | 1.000 |

### Deaktivierte Server-Features

Die FastAPI-Dokumentation (`/docs`, `/redoc`) ist deaktiviert und
gibt keine Informationen ueber die API-Struktur preis.

---

## Fehlerbehebung

### Client verbindet sich nicht mit Winlaufen

- Ist Winlaufen gestartet und der Sprecher-PC Modus aktiv?
- Stimmt die IP-Adresse? (`--winlaufen-pc`)
- Ist Port 4444 (TCP) vom Client-Rechner erreichbar?
- Firewall auf dem Winlaufen-PC pruefen

### Client verbindet sich nicht mit WODA Server

- Ist der WODA Server gestartet?
- Stimmen Hostname, Port und Token?
- Ist Port 8443 in der Server-Firewall geoeffnet?
- `--api-test` verwenden um die Verbindung zu pruefen
- Bei HTTPS: Ist das Zertifikat gueltig?

### Server meldet "Invalid HTTP request received"

- Der Client versucht HTTPS zu sprechen, aber der Server laeuft
  als HTTP. Loesung: `--api-endpoint http://IP` verwenden, oder
  einen Reverse Proxy (Caddy/nginx) mit TLS davorschalten.

### Telegram sendet nicht

- `--telegram-test` verwenden
- Ist der Bot Mitglied der Gruppe?
- Hat der Bot Schreibrechte?
- Stimmt die Chat-ID?

### Grosse Tabellen werden in Telegram nicht angezeigt

Telegram hat ein Zeichenlimit von 4096 pro Nachricht. Der Client
teilt grosse Tabellen automatisch auf. Falls trotzdem Probleme
auftreten, liegt es moeglicherweise an der Telegram API Rate-Limit.
In diesem Fall einfach auf das naechste Update warten.

### Webseite zeigt keine Ergebnisse

- Schreibrechte auf den Webserver-Pfad pruefen
- Stimmt der `--path` Parameter?
- Existiert `index.html` im Verzeichnis?
- Ist der Webserver (Caddy/nginx) konfiguriert fuer diesen Pfad?
- Browser-Cache leeren (Ctrl+Shift+R)

### Server meldet "Zu viele Anfragen" (429)

Der eingebaute Rate-Limiter erlaubt maximal 60 Anfragen pro Minute
und IP-Adresse. Im Normalbetrieb wird dieses Limit nicht erreicht.
Falls doch, kann die Winlaufen-Update-Frequenz die Ursache sein.

---

## Technische Details

### Protokoll

Der Winlaufen Sprecher-PC sendet ueber TCP Port 4444 einen
Java-ObjectOutputStream-Datenstrom. Der Client empfaengt passiv
(reiner Push, kein Request-Response). Jeder Update-Block enthaelt
13 Java-Objekte:

1. Wettkampfbezeichnung (String)
2. Anzeigemodus (int)
3. Anzahl Kategorien (int)
4. Kategorienamen (String-Array)
5. Teilnehmerzahlen (int-Array)
6. Unbekannt
7. Ausgewaehlte Kategorie (int, Index)
8. Unbekannt
9. Unbekannt
10. Ergebniszeilen (je 7 Spalten als String-Array, abgeschlossen
    mit "tabelle")
11. Spaltenueberschriften (String-Array)
12. Ende-Marker ("ende")

### Datenfluss Client -> Server

Der Client sendet per HTTP(S) POST an `/api/update`:

```json
{
    "wettkampf": "Bezirksmeisterschaft",
    "display_mode": 2,
    "categories": ["U16 w", "U16 m", "Herren"],
    "selected_category": 1,
    "category": "U16 m",
    "headers": ["Rang", "StNr", ...],
    "rows": [["1", "25", "FISCHER Lukas", ...]],
    "timestamp": "2026-02-10T14:30:00"
}
```

Der Server validiert die Eingabedaten, akkumuliert Ergebnisse aller
Kategorien und generiert bei jedem Update die HTML-Datei atomar
neu (Write-to-Temp + Rename).

### Sicherheits-Architektur

```
Client (Empfaenger)              Server (Webseite)
====================             ==================
[Winlaufen TCP 4444]             [FastAPI :8443]
        |                                |
  Parser-Limits:                  Auth-Pruefung:
  - MAX_READ_SIZE 10MB            - hmac.compare_digest
  - MAX_ARRAY 10000               - Bearer Token
  - MAX_STRING 1MB                       |
  - MAX_FIELDS 500                Rate-Limiter:
  - MAX_HANDLES 100000            - 60 req/min/IP
  - MAX_ANNOTATIONS 1000                |
        |                         Validierung:
  Konsole / Telegram              - MAX_BODY 2MB
  / API Push                      - MAX_ROWS 500
                                  - MAX_CELL 200
                                  - Input Sanitization
                                         |
                                  HTML-Generierung:
                                  - XSS-Escaping (5 Zeichen)
                                  - Atomares Schreiben
                                  - Temp-File Cleanup
```
