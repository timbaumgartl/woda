#!/usr/bin/env python3
"""
Winlaufen Online Data Addon Client (WODA Client)
=================================================

Was macht dieses Programm?
--------------------------
Der WODA Client verbindet sich mit dem Winlaufen Zeitnahme-Programm
und liest die Live-Ergebnisse mit. Diese werden:
  1. In der Konsole angezeigt (immer)
  2. An den WODA Server gesendet (wenn konfiguriert)

Der WODA Server stellt die Ergebnisse dann als Webseite bereit.

Wie funktioniert die Verbindung zu Winlaufen?
----------------------------------------------
Winlaufen hat einen "Sprecher-PC"-Modus. Dabei sendet das Programm
ueber TCP (Port 4444) einen Datenstrom im Java-Format. Dieser Client
liest diesen Datenstrom und extrahiert die Ergebnistabellen daraus.

Das Protokoll wurde durch Analyse von Netzwerkmitschnitten (pcapng)
reverse-engineered. Jeder Update-Block enthaelt:
  - Wettkampfname, Kategorien, Teilnehmerzahlen
  - Ergebniszeilen mit 7 Spalten (Rang, StNr, Name, Verein, Vbd, Zeit, Rueckstand)
  - Spaltenueberschriften und Steuermarker

HINWEIS: Dieses Programm ist ein unabhaengiges Open-Source-Projekt und
steht in keiner Verbindung zu Winlaufen oder dessen Entwicklern. Es
wird ohne Support, Garantie oder Gewaehrleistung bereitgestellt. Die
Nutzung erfolgt auf eigene Verantwortung.

Erstellt von Claude (Anthropic) im Auftrag des Nutzers.

Beispiele:
    # Nur Konsolenanzeige
    python3 woda_client.py --winlaufen-pc 192.168.0.100

    # Mit WODA Server
    python3 woda_client.py --winlaufen-pc 192.168.0.100 \\
        --api-endpoint mein-server.de --api-token MEIN_TOKEN

    # Verbindung zum Server testen
    python3 woda_client.py --api-endpoint mein-server.de \\
        --api-token MEIN_TOKEN --api-test
"""

# =============================================================================
# Imports - nur Python-Standardbibliothek, keine zusaetzlichen Pakete noetig
# =============================================================================
import socket          # TCP-Verbindung zu Winlaufen
import struct          # Binaerdaten lesen (Java-Format)
import sys             # Programmsteuerung (exit, stderr)
import argparse        # Kommandozeilenargumente parsen
import os              # Betriebssystem-Funktionen (clear screen)
import json            # JSON fuer API-Kommunikation
import threading       # Hintergrund-Threads fuer Netzwerk
import ssl             # HTTPS-Verschluesselung
import queue           # Thread-sichere Warteschlange fuer API-Nachrichten
import time            # Zeitstempel und Wartezeiten
from datetime import datetime        # Formatierte Zeitausgabe
from urllib.request import Request, urlopen   # HTTP-Anfragen senden
from urllib.error import URLError, HTTPError  # HTTP-Fehler abfangen

# =============================================================================
# Konstanten
# =============================================================================

# Hinweistext der in der Konsole und auf der Webseite angezeigt wird
DISCLAIMER = (
    "Dieses Programm ist ein unabhaengiges Projekt und steht in keiner\n"
    "Verbindung zu Winlaufen oder dessen Entwicklern. Keine Garantie,\n"
    "kein Support. Nutzung auf eigene Verantwortung."
)

# Schutz gegen Speichererschoepfung durch fehlerhafte/manipulierte Daten
# aus dem Winlaufen-Stream. Im Normalbetrieb werden diese Limits nie erreicht.
MAX_READ_SIZE = 10 * 1024 * 1024    # 10 MB pro Einzelleseoperation
MAX_ARRAY_ELEMENTS = 10000          # Max Elemente in einem Java-Array
MAX_STRING_LENGTH = 1 * 1024 * 1024 # 1 MB pro einzelnem String


# =============================================================================
# Java Serialization - Konstanten
# =============================================================================
# Winlaufen sendet Daten im Java ObjectOutputStream Format.
# Diese Konstanten entsprechen den Markern ("Type Codes") im Protokoll.
# Jedes Datenobjekt beginnt mit einem dieser Bytes, das den Typ angibt.

STREAM_MAGIC        = 0xACED    # Magische Bytes am Anfang jedes Java-Streams
STREAM_VERSION      = 5         # Protokollversion

# Type Codes - bestimmen welcher Datentyp als naechstes kommt
TC_NULL             = 0x70      # Null-Wert (kein Objekt)
TC_REFERENCE        = 0x71      # Verweis auf ein bereits gelesenes Objekt
TC_CLASSDESC        = 0x72      # Klassenbeschreibung (Metadaten)
TC_OBJECT           = 0x73      # Ein Java-Objekt
TC_STRING           = 0x74      # Ein String (kurz, max 65535 Bytes)
TC_ARRAY            = 0x75      # Ein Array (Liste von Werten)
TC_CLASS            = 0x76      # Klassen-Referenz
TC_BLOCKDATA        = 0x77      # Rohe Bytes (kurz, max 255 Bytes)
TC_ENDBLOCKDATA     = 0x78      # Ende eines Datenblocks
TC_RESET            = 0x79      # Stream-Reset (alle Referenzen zuruecksetzen)
TC_BLOCKDATALONG    = 0x7A      # Rohe Bytes (lang, bis 2 GB)
TC_EXCEPTION        = 0x7B      # Ausnahme/Fehler
TC_LONGSTRING       = 0x7C      # Langer String (ueber 65535 Bytes)
TC_PROXYCLASSDESC   = 0x7D      # Proxy-Klassenbeschreibung
TC_ENUM             = 0x7E      # Aufzaehlungswert (Enum)

# Flags fuer Klassenbeschreibungen
SC_SERIALIZABLE     = 0x02      # Klasse ist serialisierbar
SC_WRITE_METHOD     = 0x01      # Klasse hat eigene writeObject-Methode

# Basis-Handle fuer Objektreferenzen (Java-Standard)
BASE_WIRE_HANDLE    = 0x7E0000


# =============================================================================
# SocketReader - Liest Binaerdaten aus der TCP-Verbindung
# =============================================================================
class SocketReader:
    """
    Gepufferter Leser fuer TCP-Sockets.

    Warum gepuffert? TCP liefert Daten in Stuecken die nicht mit den
    Objektgrenzen uebereinstimmen muessen. Der Puffer sammelt die
    Stuecke und gibt sie in der angeforderten Groesse zurueck.
    """

    def __init__(self, sock, bufsize=8192):
        self.sock = sock          # Die TCP-Verbindung
        self.buf = b""            # Interner Puffer fuer empfangene Bytes
        self.bufsize = bufsize    # Wie viele Bytes pro recv() angefordert werden

    def read(self, n):
        """Liest genau n Bytes aus dem Socket. Wartet bis alle da sind."""
        if n > MAX_READ_SIZE:
            raise ValueError(f"Leseanforderung zu gross: {n} Bytes")
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(self.bufsize)
            except OSError as e:
                raise ConnectionError(f"Verbindung unterbrochen: {e}")
            if not chunk:
                raise ConnectionError("Verbindung vom Server getrennt")
            self.buf += chunk
        result = self.buf[:n]
        self.buf = self.buf[n:]
        return result

    # Hilfsmethoden: Java-Datentypen lesen
    # ">H" = Big-Endian unsigned short (2 Bytes), ">i" = signed int etc.
    def read_byte(self):   return self.read(1)[0]
    def read_ushort(self): return struct.unpack(">H", self.read(2))[0]
    def read_int(self):    return struct.unpack(">i", self.read(4))[0]
    def read_uint(self):   return struct.unpack(">I", self.read(4))[0]
    def read_long(self):   return struct.unpack(">q", self.read(8))[0]
    def read_float(self):  return struct.unpack(">f", self.read(4))[0]
    def read_double(self): return struct.unpack(">d", self.read(8))[0]
    def read_bool(self):   return self.read_byte() != 0

    def read_utf(self):
        """Liest einen UTF-8 String (max 65535 Bytes, Java-Standard)."""
        length = self.read_ushort()
        return self.read(length).decode("utf-8", errors="replace")


# =============================================================================
# Java-Datenstrukturen
# =============================================================================

class JavaClassDesc:
    """Beschreibung einer Java-Klasse (Name, Felder, Elternklasse)."""
    def __init__(self, name="", serial_uid=0, flags=0, fields=None, super_desc=None):
        self.name = name
        self.serial_uid = serial_uid
        self.flags = flags
        self.fields = fields or []     # [(typ, name, klasse), ...]
        self.super_desc = super_desc


class JavaObject:
    """Ein Java-Objekt mit Klassenbeschreibung und Feldwerten."""
    def __init__(self, class_desc, values=None):
        self.class_desc = class_desc
        self.values = values or {}     # {feldname: wert, ...}


class JavaArray:
    """Ein Java-Array (z.B. String[] oder int[])."""
    def __init__(self, class_desc, elements):
        self.class_desc = class_desc
        self.elements = elements       # [wert, wert, ...]


# =============================================================================
# Java ObjectStream Parser - Liest den Datenstrom von Winlaufen
# =============================================================================

class JavaObjectStreamParser:
    """
    Liest einen Java ObjectOutputStream und wandelt ihn in Python-Objekte um.

    Vereinfacht:
      1. Lese ein Type-Code-Byte (z.B. TC_STRING = 0x74)
      2. Je nach Typ: Lese die zugehoerigen Daten
      3. Speichere das Ergebnis (fuer spaetere Referenzen)
      4. Gib das Python-Objekt zurueck
    """

    MAX_DEPTH = 50  # Schutz gegen Stack-Overflow

    def __init__(self, reader, debug=False):
        self.reader = reader
        self.handles = {}                       # Referenz-Tabelle
        self.next_handle = BASE_WIRE_HANDLE
        self.debug = debug
        self._depth = 0

    def _log(self, msg):
        if self.debug:
            print(f"  {msg}", file=sys.stderr)

    def _assign_handle(self, obj):
        """Weist dem Objekt eine Handle-Nummer zu (fuer spaetere Referenzen)."""
        h = self.next_handle
        self.next_handle += 1
        self.handles[h] = obj
        return h

    def read_stream_header(self):
        """Liest die ersten 4 Bytes: Magic (0xACED) + Version (5)."""
        magic = self.reader.read_ushort()
        version = self.reader.read_ushort()
        if magic != STREAM_MAGIC:
            raise ValueError(f"Ungueltiger Stream-Magic: 0x{magic:04x}")
        if version != STREAM_VERSION:
            raise ValueError(f"Ungueltige Stream-Version: {version}")

    def read_object(self):
        """Liest das naechste Objekt (mit Tiefenpruefung)."""
        self._depth += 1
        if self._depth > self.MAX_DEPTH:
            raise ValueError(f"Verschachtelungstiefe ueberschritten ({self.MAX_DEPTH})")
        try:
            return self._dispatch()
        finally:
            self._depth -= 1

    def _dispatch(self):
        """Liest Type-Code und ruft passende Methode auf."""
        tc = self.reader.read_byte()
        if   tc == TC_NULL:           return None
        elif tc == TC_REFERENCE:      return self._read_reference()
        elif tc == TC_STRING:         return self._read_string()
        elif tc == TC_LONGSTRING:     return self._read_long_string()
        elif tc == TC_OBJECT:         return self._read_object()
        elif tc == TC_ARRAY:          return self._read_array()
        elif tc == TC_CLASSDESC:      return self._read_class_desc()
        elif tc == TC_PROXYCLASSDESC: return self._read_proxy_class_desc()
        elif tc == TC_CLASS:          return self._read_class()
        elif tc == TC_BLOCKDATA:      return self._read_block_data()
        elif tc == TC_BLOCKDATALONG:  return self._read_block_data_long()
        elif tc == TC_ENDBLOCKDATA:   return "ENDBLOCKDATA"
        elif tc == TC_RESET:
            self.handles.clear()
            self.next_handle = BASE_WIRE_HANDLE
            return "RESET"
        elif tc == TC_ENUM:           return self._read_enum()
        else: raise ValueError(f"Unbekannter Type-Code: 0x{tc:02x}")

    def _read_reference(self):
        handle = self.reader.read_uint()
        return self.handles.get(handle, f"<ref:0x{handle:06x}>")

    def _read_string(self):
        s = self.reader.read_utf()
        self._assign_handle(s)
        return s

    def _read_long_string(self):
        length = self.reader.read_long()
        if length < 0 or length > MAX_STRING_LENGTH:
            raise ValueError(f"String zu gross: {length} Bytes")
        s = self.reader.read(length).decode("utf-8", errors="replace")
        self._assign_handle(s)
        return s

    def _read_class_desc(self):
        """Liest eine Klassenbeschreibung: Name, Felder, Elternklasse."""
        name = self.reader.read_utf()
        serial_uid = self.reader.read_long()
        handle = self._assign_handle(None)
        flags = self.reader.read_byte()
        field_count = self.reader.read_ushort()
        fields = []
        for _ in range(field_count):
            ftype = chr(self.reader.read_byte())  # I=int, L=Objekt, [=Array ...
            fname = self.reader.read_utf()
            fclass = self.read_object() if ftype in ("L", "[") else None
            fields.append((ftype, fname, fclass))
        self._read_annotations()
        super_desc = self.read_object()
        desc = JavaClassDesc(name, serial_uid, flags, fields, super_desc)
        self.handles[handle] = desc
        return desc

    def _read_proxy_class_desc(self):
        handle = self._assign_handle(None)
        iface_count = self.reader.read_int()
        interfaces = [self.reader.read_utf() for _ in range(iface_count)]
        self._read_annotations()
        super_desc = self.read_object()
        desc = JavaClassDesc(f"Proxy({','.join(interfaces)})", 0, 0, [], super_desc)
        self.handles[handle] = desc
        return desc

    def _read_annotations(self):
        """Liest Annotations-Daten bis zum Ende-Marker."""
        while True:
            tc = self.reader.read_byte()
            if tc == TC_ENDBLOCKDATA: break
            elif tc == TC_BLOCKDATA:     self.reader.read(self.reader.read_byte())
            elif tc == TC_BLOCKDATALONG:
                length = self.reader.read_int()
                if length < 0:
                    raise ValueError(f"Negative BlockData-Laenge: {length}")
                self.reader.read(length)
            elif tc == TC_STRING:    self._read_string()
            elif tc == TC_REFERENCE: self._read_reference()
            elif tc == TC_OBJECT:    self._read_object()
            elif tc == TC_NULL:      pass
            else: raise ValueError(f"Unerwarteter TC in Annotation: 0x{tc:02x}")

    def _read_class(self):
        desc = self.read_object()
        self._assign_handle(desc)
        return desc

    def _read_object(self):
        """Liest ein Java-Objekt: Klassenbeschreibung + Feldwerte."""
        desc = self.read_object()
        handle = self._assign_handle(None)
        obj = JavaObject(desc)
        self._read_fields(obj, desc)
        self.handles[handle] = obj
        return obj

    def _read_fields(self, obj, desc):
        """Liest alle Felder eines Objekts (inkl. Elternklasse)."""
        if not isinstance(desc, JavaClassDesc): return
        if desc.super_desc and isinstance(desc.super_desc, JavaClassDesc):
            self._read_fields(obj, desc.super_desc)
        for ftype, fname, _ in desc.fields:
            obj.values[fname] = self._read_field_value(ftype)
        if desc.flags & SC_WRITE_METHOD:
            self._read_annotations()

    def _read_field_value(self, ftype):
        """Liest einen Feldwert passend zum Typzeichen."""
        if   ftype == "I": return self.reader.read_int()
        elif ftype == "J": return self.reader.read_long()
        elif ftype == "F": return self.reader.read_float()
        elif ftype == "D": return self.reader.read_double()
        elif ftype == "B": return self.reader.read_byte()
        elif ftype == "S": return struct.unpack(">h", self.reader.read(2))[0]
        elif ftype == "Z": return self.reader.read_bool()
        elif ftype == "C": return chr(self.reader.read_ushort())
        elif ftype in ("L", "["): return self.read_object()
        else: raise ValueError(f"Unbekannter Feldtyp: {ftype}")

    def _read_array(self):
        """Liest ein Java-Array (Typ + Laenge + Elemente)."""
        desc = self.read_object()
        count = self.reader.read_int()
        if count < 0 or count > MAX_ARRAY_ELEMENTS:
            raise ValueError(f"Array zu gross: {count}")
        handle = self._assign_handle(None)
        atype = desc.name if isinstance(desc, JavaClassDesc) else ""
        if   atype == "[I": elements = [self.reader.read_int() for _ in range(count)]
        elif atype == "[J": elements = [self.reader.read_long() for _ in range(count)]
        elif atype == "[F": elements = [self.reader.read_float() for _ in range(count)]
        elif atype == "[D": elements = [self.reader.read_double() for _ in range(count)]
        elif atype == "[B": elements = list(self.reader.read(count))
        elif atype == "[Z": elements = [self.reader.read_bool() for _ in range(count)]
        elif atype == "[S": elements = [struct.unpack(">h", self.reader.read(2))[0] for _ in range(count)]
        elif atype == "[C": elements = [chr(self.reader.read_ushort()) for _ in range(count)]
        else: elements = [self.read_object() for _ in range(count)]
        arr = JavaArray(desc, elements)
        self.handles[handle] = arr
        return arr

    def _read_enum(self):
        desc = self.read_object()
        handle = self._assign_handle(None)
        name = self.read_object()
        self.handles[handle] = name
        return name

    def _read_block_data(self):
        return self.reader.read(self.reader.read_byte())

    def _read_block_data_long(self):
        length = self.reader.read_int()
        if length < 0:
            raise ValueError(f"Negative BlockData-Laenge: {length}")
        return self.reader.read(length)


# =============================================================================
# resolve() - Wandelt Java-Objekte in einfache Python-Werte um
# =============================================================================

def resolve(val):
    """
    Packt Java-Wrapper aus:
      String -> String, Zahl -> Zahl,
      JavaObject -> dessen "value"-Feld,
      JavaArray -> Python-Liste
    """
    if val is None:            return None
    if isinstance(val, str):   return val
    if isinstance(val, (int, float)): return val
    if isinstance(val, JavaObject):
        v = val.values.get("value")
        return v if v is not None else str(val)
    if isinstance(val, JavaArray):
        return [resolve(e) for e in val.elements]
    return str(val)


# =============================================================================
# WODA Server API - Zuverlaessige Uebertragung per Warteschlange
# =============================================================================

class ApiPusher:
    """
    Sendet Ergebnisse an den WODA Server.

    Zuverlaessigkeit durch:
    - Warteschlange (Queue) statt direkter Threads pro Update
    - Ein einziger Sender-Thread mit Retry-Logik
    - Nur das neueste Update in der Queue (Server braucht aktuellen Stand)
    """

    def __init__(self, endpoint, port=8443, token="", debug=False):
        self.endpoint = endpoint.rstrip("/")
        self.port = port
        self.token = token
        self.debug = debug

        # URL: Ohne Schema wird http:// verwendet (uvicorn ohne SSL).
        if self.endpoint.startswith("http://") or self.endpoint.startswith("https://"):
            self.base_url = f"{self.endpoint}:{self.port}"
        else:
            self.base_url = f"http://{self.endpoint}:{self.port}"

        # Warteschlange mit max 1 Element (nur neustes Update)
        self._queue = queue.Queue(maxsize=1)

        # Status-Tracking
        self.last_send_ok = None   # True/False/None = noch kein Versuch
        self.send_count = 0        # Erfolgreich gesendet
        self.error_count = 0       # Fehlgeschlagen
        self.last_error = ""       # Letzter Fehlertext

        # Sender-Thread starten
        self._worker = threading.Thread(target=self._send_loop, daemon=True)
        self._worker.start()

    def _log(self, msg):
        if self.debug:
            print(f"  [API: {msg}]", file=sys.stderr)

    def _http(self, path, payload=None, method="POST"):
        """Fuehrt einen HTTP-Request aus. Gibt JSON-Antwort oder None zurueck."""
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        req = Request(url, data=data, headers=headers)
        if method == "GET":
            req = Request(url, headers=headers)
        ctx = ssl.create_default_context() if url.startswith("https://") else None
        try:
            with urlopen(req, timeout=15, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self._log(f"HTTP {e.code}: {body[:200]}")
            self.last_error = f"HTTP {e.code}"
            return None
        except (URLError, OSError) as e:
            self._log(f"Verbindungsfehler: {e}")
            self.last_error = str(e)[:80]
            return None

    def _send_loop(self):
        """Sender-Thread: Wartet auf Updates und sendet sie mit Retry."""
        while True:
            try:
                payload = self._queue.get(timeout=5)
            except queue.Empty:
                continue
            if payload is None:
                break  # Beenden-Signal

            # Bis zu 3 Versuche
            for attempt in range(1, 4):
                result = self._http("/api/update", payload)
                if result and result.get("status") == "ok":
                    self.last_send_ok = True
                    self.send_count += 1
                    self._log(f"Update #{self.send_count} gesendet")
                    break
                else:
                    self.last_send_ok = False
                    self.error_count += 1
                    self._log(f"Versuch {attempt}/3 fehlgeschlagen")
                    if attempt < 3:
                        time.sleep(0.5)

    def push_update(self, state):
        """
        Stellt ein Update in die Warteschlange.
        Ersetzt ein altes noch nicht gesendetes Update (nur neustes zaehlt).
        """
        payload = {
            "category": state.get("category", ""),
            "rows": state.get("rows", []),
            "headers": state.get("headers", []),
            "timestamp": datetime.now().isoformat(),
        }
        # Altes Update aus der Queue entfernen
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            pass

    def test_connection(self):
        """Testet die Verbindung zum WODA Server (--api-test)."""
        print(f"Teste Verbindung zu {self.base_url} ...")
        result = self._http("/api/health", method="GET")
        if result and result.get("status") == "ok":
            print(f"Verbindung zum WODA Server erfolgreich.")
            print(f"  Server-Version: {result.get('version', '?')}")
            return True
        print(f"FEHLER: Verbindung fehlgeschlagen.")
        if self.last_error:
            print(f"  Fehler: {self.last_error}")
        return False

    def stop(self):
        """Sender-Thread sauber beenden."""
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass


# =============================================================================
# Konsolenanzeige
# =============================================================================

# ANSI-Farbcodes fuer Terminal-Ausgabe
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
CYAN    = "\033[36m"
WHITE   = "\033[97m"
BLUE_BG = "\033[44m"
GRAY    = "\033[90m"
YELLOW  = "\033[33m"
GREEN   = "\033[32m"
RED     = "\033[31m"


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def display_update(state):
    """Zeigt den aktuellen Ergebnisstand im Terminal an."""
    clear_screen()
    ts = datetime.now().strftime("%H:%M:%S")

    # API-Status anzeigen
    parts = []
    api = state.get("api_pusher")
    if api:
        if api.last_send_ok is True:
            parts.append(f"{GREEN}[API ok: {api.send_count}]{RESET}")
        elif api.last_send_ok is False:
            parts.append(f"{RED}[API Fehler: {api.last_error}]{RESET}")
        else:
            parts.append(f"{YELLOW}[API ...]{RESET}")
    extras = "  " + " ".join(parts) if parts else ""

    print(f"\n  {BOLD}WODA Client{RESET}  {GRAY}[{ts}]{RESET}{extras}\n")

    # Modus und Kategorie
    mode_names = {0: "Startliste", 1: "Zwischenstand", 2: "Zwischenstand", 3: "Endergebnis"}
    mode = state.get("display_mode", 0)
    category = state.get("category", "")
    if category:
        print(f"  {BOLD}{CYAN}Aktueller {mode_names.get(mode, '?')}:  {category}{RESET}")

    # Kategorie-Tabs
    categories = state.get("categories", [])
    sel_idx = state.get("selected_category", -1)
    if categories:
        cat_parts = []
        for i, c in enumerate(categories):
            if i == sel_idx:
                cat_parts.append(f"{BOLD}{WHITE}[{c}]{RESET}")
            else:
                cat_parts.append(f"{GRAY}{c}{RESET}")
        print(f"  {' | '.join(cat_parts)}")
    print()

    # Tabelle
    rows = state.get("rows", [])
    headers = state.get("headers",
                        ["Rang", "StNr", "Name, Vorname", "Verein",
                         "Vbd", "Laufzeit", "Rueckstand"])

    if not rows:
        print(f"  {DIM}Warte auf Ergebnisse...{RESET}")
        print(f"\n  {GRAY}[Enter] zum Beenden{RESET}")
        return

    # Spaltenbreiten berechnen
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row[:len(widths)]):
            widths[i] = max(widths[i], len(str(val)))
    widths = [max(w, 4) for w in widths]
    widths[2] = max(widths[2], 22)
    widths[3] = max(widths[3], 22)
    right_align = {0, 1, 5, 6}

    def fmt(val, i):
        s = str(val)
        return f"{s:>{widths[i]}}" if i in right_align else f"{s:<{widths[i]}}"

    sep = "-" * (sum(widths) + 3 * len(widths) + 1)

    hdr = " | ".join(f"{BOLD}{fmt(h, i)}{RESET}" for i, h in enumerate(headers))
    print(f"  | {hdr} |")
    print(f"  {sep}")

    for idx, row in enumerate(rows):
        cols = " | ".join(fmt(row[i] if i < len(row) else "", i) for i in range(len(headers)))
        if idx == 0:
            print(f"  | {BLUE_BG}{WHITE}{cols}{RESET} |")
        else:
            print(f"  | {cols} |")

    print(f"  {sep}")
    print(f"  {DIM}{len(rows)} Teilnehmer{RESET}")
    print(f"\n  {GRAY}Empfange von {state.get('host','?')}:{state.get('port','?')} ..."
          f" [Enter] zum Beenden{RESET}")


# =============================================================================
# Winlaufen Client - Verbindung + Protokoll
# =============================================================================

class WinlaufenClient:
    """
    Verbindet sich mit Winlaufen, liest Ergebnisse, zeigt sie an
    und sendet sie optional an den WODA Server.
    """

    def __init__(self, host="127.0.0.1", port=4444, debug=False,
                 api_pusher=None):
        self.host = host
        self.port = port
        self.debug = debug
        self.api_pusher = api_pusher
        self.state = {"host": host, "port": port, "api_pusher": api_pusher}
        self.update_count = 0
        self._sock = None
        self._running = False

    def run(self):
        """Startet den Client: Verbinden, Empfangs-Thread, auf Enter warten."""
        print(f"{BOLD}Verbinde mit Winlaufen-PC {self.host}:{self.port} ...{RESET}")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10)
        try:
            self._sock.connect((self.host, self.port))
            print(f"{GREEN}Verbunden.{RESET}")
        except (ConnectionRefusedError, OSError) as e:
            print(f"{RED}Verbindung fehlgeschlagen: {e}{RESET}")
            print(f"\n  Ist Winlaufen gestartet auf {self.host}:{self.port}?")
            self._sock.close()
            return
        self._sock.settimeout(None)
        self._running = True
        threading.Thread(target=self._receive_loop, daemon=True).start()
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        self._shutdown()

    def _shutdown(self):
        if not self._running: return
        self._running = False
        print(f"\n{YELLOW}Beende...{RESET}")
        if self._sock:
            try: self._sock.close()
            except OSError: pass
        if self.api_pusher:
            self.api_pusher.stop()
        print(f"{YELLOW}Beendet.{RESET}")

    def _receive_loop(self):
        """Empfangs-Thread: Liest kontinuierlich Update-Bloecke."""
        reader = SocketReader(self._sock)
        parser = JavaObjectStreamParser(reader, debug=self.debug)
        try:
            parser.read_stream_header()
            self._log("Java ObjectStream Header OK")
            while self._running:
                self._read_update_block(parser)
        except ConnectionError as e:
            if self._running:
                print(f"\n{YELLOW}Verbindung getrennt: {e}{RESET}")
                print(f"{GRAY}[Enter] zum Beenden{RESET}")
        except Exception as e:
            if self._running:
                print(f"\n{RED}Fehler: {e}{RESET}")
                if self.debug:
                    import traceback; traceback.print_exc()
                print(f"{GRAY}[Enter] zum Beenden{RESET}")

    def _log(self, msg):
        if self.debug:
            print(f"{GRAY}[{msg}]{RESET}", file=sys.stderr)

    def _read_update_block(self, parser):
        """
        Liest einen Update-Block (13 Objekte in fester Reihenfolge):
          1. Wettkampfname        7. Index aktuelle Kategorie
          2. Anzeigemodus         8-9. Unbekannt
          3. Unbekannt           10. Ergebniszeilen (bis "tabelle")
          4. Kategorienamen      11. Spaltenueberschriften
          5. Teilnehmerzahlen    12. Ende-Marker ("ende")
          6. Unbekannt
        """
        obj = parser.read_object()
        wettkampf = resolve(obj)
        if wettkampf == "RESET": return
        if not isinstance(wettkampf, str): return
        if wettkampf in ("ende", "tabelle"): return

        self.state["wettkampf"] = wettkampf
        self.state["display_mode"] = resolve(parser.read_object())
        resolve(parser.read_object())  # Unbekannt

        cats_obj = parser.read_object()
        categories = resolve(cats_obj) if isinstance(cats_obj, JavaArray) else []
        self.state["categories"] = categories

        counts_obj = parser.read_object()
        self.state["cat_counts"] = resolve(counts_obj) if isinstance(counts_obj, JavaArray) else []
        resolve(parser.read_object())  # Unbekannt

        sel_cat = resolve(parser.read_object())
        self.state["selected_category"] = sel_cat
        category = ""
        if isinstance(sel_cat, int) and 0 <= sel_cat < len(categories):
            category = categories[sel_cat]
        self.state["category"] = category

        resolve(parser.read_object())  # Unbekannt
        resolve(parser.read_object())  # Unbekannt

        # Ergebniszeilen lesen bis "tabelle" kommt
        rows = []
        MAX_ROWS = 2000
        while True:
            val = resolve(parser.read_object())
            if isinstance(val, str) and val == "tabelle": break
            if isinstance(val, list) and len(val) == 7:
                rows.append([str(v) if v is not None else "" for v in val])
            if len(rows) >= MAX_ROWS: break
        self.state["rows"] = rows

        headers_obj = parser.read_object()
        headers = resolve(headers_obj) if isinstance(headers_obj, JavaArray) else None
        if headers:
            self.state["headers"] = headers

        ende = resolve(parser.read_object())
        if ende != "ende":
            self._log(f"Warnung: Erwartete 'ende', bekam '{ende}'")

        self.update_count += 1
        self._log(f"Update #{self.update_count}: {category} - {len(rows)} Zeilen")

        display_update(self.state)
        if self.api_pusher:
            self.api_pusher.push_update(self.state)


# =============================================================================
# Programmstart
# =============================================================================

def main():
    p = argparse.ArgumentParser(
        description="WODA Client - Winlaufen Online Data Addon Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Beispiele:
  %(prog)s --winlaufen-pc 192.168.0.100
  %(prog)s --winlaufen-pc 192.168.0.100 \\
      --api-endpoint mein-server.de --api-token GEHEIM

{DISCLAIMER}
""",
    )

    wl = p.add_argument_group("Winlaufen Verbindung")
    wl.add_argument("--winlaufen-pc", default="127.0.0.1",
                    help="Hostname / IP des Winlaufen-PC (default: 127.0.0.1)")
    wl.add_argument("--port", type=int, default=4444,
                    help="Port des Winlaufen Sprecher-PC (default: 4444)")

    api = p.add_argument_group("WODA Server Verbindung")
    api.add_argument("--api-endpoint",
                     help="Hostname / IP des WODA Servers")
    api.add_argument("--api-port", type=int, default=8443,
                     help="Port des WODA Servers (default: 8443)")
    api.add_argument("--api-token", default=os.environ.get("WODA_API_TOKEN", ""),
                     help="API-Token (oder Umgebungsvariable WODA_API_TOKEN)")
    api.add_argument("--api-test", action="store_true",
                     help="Verbindung zum WODA Server testen und beenden")

    p.add_argument("--debug", "-d", action="store_true",
                   help="Debug-Ausgabe aktivieren")

    args = p.parse_args()
    print(f"{DIM}{DISCLAIMER}{RESET}\n")

    # Verbindungstest
    if args.api_test:
        if not args.api_endpoint or not args.api_token:
            print(f"{RED}--api-endpoint und --api-token erforderlich{RESET}")
            sys.exit(1)
        ap = ApiPusher(args.api_endpoint, args.api_port, args.api_token,
                       debug=args.debug)
        sys.exit(0 if ap.test_connection() else 1)

    # API-Pusher
    api_pusher = None
    if args.api_endpoint:
        if not args.api_token:
            print(f"{RED}--api-token erforderlich wenn --api-endpoint gesetzt{RESET}")
            sys.exit(1)
        api_pusher = ApiPusher(args.api_endpoint, args.api_port, args.api_token,
                               debug=args.debug)
        print(f"WODA Server: {api_pusher.base_url}")
        if not api_pusher.base_url.startswith("https://"):
            print(f"{YELLOW}HINWEIS: HTTP-Verbindung (unverschluesselt).{RESET}\n")

    if not api_pusher:
        print(f"{GRAY}Kein WODA Server konfiguriert. Nur Konsolenanzeige.{RESET}\n")

    client = WinlaufenClient(
        host=args.winlaufen_pc, port=args.port,
        debug=args.debug, api_pusher=api_pusher,
    )
    client.run()


if __name__ == "__main__":
    main()
