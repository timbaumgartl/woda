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
