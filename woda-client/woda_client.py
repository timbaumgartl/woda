#!/usr/bin/env python3
"""
Winlaufen Online Data Addon Client (WODA Client)
=================================================

Liest Live-Ergebnisse vom Winlaufen Zeitnahme-Programm und sendet
sie an den WODA Server, der daraus eine Webseite erzeugt.
Unterstuetzt Langlauf (7 Spalten) und Biathlon (8 Spalten).

Voraussetzungen: Nur Python 3.6+ (Standardbibliothek)

HINWEIS: Unabhaengiges Open-Source-Projekt, keine Verbindung zu
Winlaufen. Keine Garantie, kein Support, Nutzung auf eigene Verantwortung.

Protokoll-Referenz (SprecherPC LAN-Schnittstelle):
  Obj 0: Wettkampfart (String)
  Obj 1: Auswertungsmodus (Integer)
  Obj 2: Anzahl Klassen (Integer)
  Obj 3: Klassenbezeichnungen (String[])
  Obj 4: Rundenzahl/Teamgroesse (int[])
  Obj 5: Position WinSpringen (Integer)
  Obj 6: Sprecher-Nr/Klassenreihenfolge (Integer) = Kategorie-Index
  Obj 7: Runde/Durchgang (Integer)
  Obj 8: Aktueller Einlauf (Integer)
  Obj 9ff: Tabellendaten (Object[] mit 7-8 Spalten)
  danach: "tabelle" (String)
  danach: Tabellenueberschriften (String[])
  danach: "ende" (String)
  Zwischen Bloecken: "Uhr"+HH:MM:SS Strings (Uhrzeit)

Erstellt mit Claude Opus 4.6 (claude-opus-4-6) von Anthropic, Februar 2025.
Projekt: https://github.com/timbaumgartl/woda

Beispiele:
    python3 woda_client.py --winlaufen-pc 127.0.0.1
    python3 woda_client.py --winlaufen-pc 127.0.0.1 --api-endpoint http://mein-web-server.tld --api-token MEIN_TOKEN
"""

import socket
import struct
import sys
import argparse
import os
import json
import threading
import ssl
import queue
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DISCLAIMER = (
    "Unabhaengiges Open-Source-Projekt. Keine Verbindung zu Winlaufen.\n"
    "Keine Garantie, kein Support. Nutzung auf eigene Verantwortung."
)

# Schutzgrenzen gegen fehlerhafte/manipulierte Daten
MAX_READ_SIZE = 10 * 1024 * 1024
MAX_ARRAY_ELEMENTS = 10000
MAX_STRING_LENGTH = 1 * 1024 * 1024

# ============================================================================
# Java Serialization Konstanten
# ============================================================================
STREAM_MAGIC    = 0xACED
STREAM_VERSION  = 5
TC_NULL         = 0x70
TC_REFERENCE    = 0x71
TC_CLASSDESC    = 0x72
TC_OBJECT       = 0x73
TC_STRING       = 0x74
TC_ARRAY        = 0x75
TC_CLASS        = 0x76
TC_BLOCKDATA    = 0x77
TC_ENDBLOCKDATA = 0x78
TC_RESET        = 0x79
TC_BLOCKDATALONG = 0x7A
TC_EXCEPTION    = 0x7B
TC_LONGSTRING   = 0x7C
TC_PROXYCLASSDESC = 0x7D
TC_ENUM         = 0x7E
SC_SERIALIZABLE = 0x02
SC_WRITE_METHOD = 0x01
BASE_WIRE_HANDLE = 0x7E0000

# ============================================================================
# SocketReader
# ============================================================================
class SocketReader:
    """Gepufferter Binaer-Leser fuer TCP-Sockets."""
    def __init__(self, sock, bufsize=8192):
        self.sock = sock
        self.buf = b""
        self.bufsize = bufsize

    def read(self, n):
        if n > MAX_READ_SIZE:
            raise ValueError(f"Leseanforderung zu gross: {n}")
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(self.bufsize)
            except OSError as e:
                raise ConnectionError(f"Verbindung unterbrochen: {e}")
            if not chunk:
                raise ConnectionError("Verbindung getrennt")
            self.buf += chunk
        result = self.buf[:n]
        self.buf = self.buf[n:]
        return result

    def peek(self, n=2):
        """Liest n Bytes ohne den Puffer voranzubewegen."""
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(self.bufsize)
            except OSError as e:
                raise ConnectionError(f"Verbindung unterbrochen: {e}")
            if not chunk:
                raise ConnectionError("Verbindung getrennt")
            self.buf += chunk
        return self.buf[:n]

    def read_byte(self):   return self.read(1)[0]
    def read_ushort(self): return struct.unpack(">H", self.read(2))[0]
    def read_int(self):    return struct.unpack(">i", self.read(4))[0]
    def read_uint(self):   return struct.unpack(">I", self.read(4))[0]
    def read_long(self):   return struct.unpack(">q", self.read(8))[0]
    def read_float(self):  return struct.unpack(">f", self.read(4))[0]
    def read_double(self): return struct.unpack(">d", self.read(8))[0]
    def read_bool(self):   return self.read_byte() != 0
    def read_utf(self):
        length = self.read_ushort()
        return self.read(length).decode("utf-8", errors="replace")

# ============================================================================
# Java-Datenstrukturen
# ============================================================================
class JavaClassDesc:
    def __init__(self, name="", serial_uid=0, flags=0, fields=None, super_desc=None):
        self.name = name
        self.serial_uid = serial_uid
        self.flags = flags
        self.fields = fields or []
        self.super_desc = super_desc

class JavaObject:
    def __init__(self, class_desc, values=None):
        self.class_desc = class_desc
        self.values = values or {}

class JavaArray:
    def __init__(self, class_desc, elements):
        self.class_desc = class_desc
        self.elements = elements

# ============================================================================
# Java ObjectStream Parser
# ============================================================================
class JavaObjectStreamParser:
    MAX_DEPTH = 50

    def __init__(self, reader, debug=False):
        self.reader = reader
        self.handles = {}
        self.next_handle = BASE_WIRE_HANDLE
        self.debug = debug
        self._depth = 0

    def _log(self, msg):
        if self.debug:
            print(f"  {msg}", file=sys.stderr)

    def _assign_handle(self, obj):
        h = self.next_handle
        self.next_handle += 1
        self.handles[h] = obj
        return h

    def read_stream_header(self):
        magic = self.reader.read_ushort()
        version = self.reader.read_ushort()
        if magic != STREAM_MAGIC:
            raise ValueError(f"Ungueltiger Stream-Magic: 0x{magic:04x}")

    def reset_handles(self):
        """Setzt die Referenztabelle zurueck (fuer Stream-Neustart)."""
        self.handles.clear()
        self.next_handle = BASE_WIRE_HANDLE

    def read_object(self):
        self._depth += 1
        if self._depth > self.MAX_DEPTH:
            raise ValueError(f"Verschachtelungstiefe > {self.MAX_DEPTH}")
        try:
            return self._dispatch()
        finally:
            self._depth -= 1

    def _dispatch(self):
        tc = self.reader.read_byte()

        # Stream-Neustart erkennen: 0xAC gefolgt von 0xED
        # Kann mitten in einem Update-Block passieren wenn Winlaufen
        # die Verbindung intern zuruecksetzt (z.B. bei Modus-Wechsel)
        if tc == 0xAC:
            next_byte = self.reader.read_byte()
            if next_byte == 0xED:
                # Restlichen Header lesen (Version: 2 Bytes)
                self.reader.read_ushort()
                self.handles.clear()
                self.next_handle = BASE_WIRE_HANDLE
                self._log("Stream-Neustart erkannt (0xACED mitten im Datenstrom)")
                return "RESET"
            else:
                raise ValueError(f"Unbekannter Type-Code: 0xac (gefolgt von 0x{next_byte:02x})")

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
        else:
            raise ValueError(f"Unbekannter Type-Code: 0x{tc:02x}")

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
            raise ValueError(f"String zu gross: {length}")
        s = self.reader.read(length).decode("utf-8", errors="replace")
        self._assign_handle(s)
        return s

    def _read_class_desc(self):
        name = self.reader.read_utf()
        serial_uid = self.reader.read_long()
        handle = self._assign_handle(None)
        flags = self.reader.read_byte()
        field_count = self.reader.read_ushort()
        fields = []
        for _ in range(field_count):
            ftype = chr(self.reader.read_byte())
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
        while True:
            tc = self.reader.read_byte()
            if tc == TC_ENDBLOCKDATA: break
            elif tc == TC_BLOCKDATA:     self.reader.read(self.reader.read_byte())
            elif tc == TC_BLOCKDATALONG:
                length = self.reader.read_int()
                if length < 0: raise ValueError(f"Negative BlockData-Laenge")
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
        desc = self.read_object()
        handle = self._assign_handle(None)
        obj = JavaObject(desc)
        self._read_fields(obj, desc)
        self.handles[handle] = obj
        return obj

    def _read_fields(self, obj, desc):
        if not isinstance(desc, JavaClassDesc): return
        if desc.super_desc and isinstance(desc.super_desc, JavaClassDesc):
            self._read_fields(obj, desc.super_desc)
        for ftype, fname, _ in desc.fields:
            obj.values[fname] = self._read_field_value(ftype)
        if desc.flags & SC_WRITE_METHOD:
            self._read_annotations()

    def _read_field_value(self, ftype):
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
        if length < 0: raise ValueError(f"Negative BlockData-Laenge")
        return self.reader.read(length)


def resolve(val):
    """Java-Wrapper auspacken -> einfachen Python-Wert."""
    if val is None:            return None
    if isinstance(val, str):   return val
    if isinstance(val, (int, float)): return val
    if isinstance(val, JavaObject):
        v = val.values.get("value")
        return v if v is not None else val  # JavaObject zurueckgeben wenn kein value
    if isinstance(val, JavaArray):
        return [resolve(e) for e in val.elements]
    return val


# ============================================================================
# API-Pusher (Queue-basiert, mit Retry)
# ============================================================================
class ApiPusher:
    """Sendet Updates zuverlaessig an den WODA Server."""

    def __init__(self, endpoint, port=8443, token="", debug=False):
        self.token = token
        self.debug = debug
        ep = endpoint.rstrip("/")
        if ep.startswith("http://") or ep.startswith("https://"):
            self.base_url = f"{ep}:{port}"
        else:
            self.base_url = f"http://{ep}:{port}"
        self._queue = queue.Queue(maxsize=1)
        self.last_send_ok = None
        self.send_count = 0
        self.error_count = 0
        self.last_error = ""
        threading.Thread(target=self._send_loop, daemon=True).start()

    def _log(self, msg):
        if self.debug: print(f"  [API: {msg}]", file=sys.stderr)

    def _http(self, path, payload=None, method="POST"):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.token}"}
        req = Request(url, data=data, headers=headers) if method == "POST" \
            else Request(url, headers=headers)
        ctx = ssl.create_default_context() if url.startswith("https://") else None
        try:
            with urlopen(req, timeout=15, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            self.last_error = f"HTTP {e.code}"
            try: e.read()
            except: pass
            return None
        except (URLError, OSError) as e:
            self.last_error = str(e)[:80]
            return None

    def _send_loop(self):
        while True:
            try:
                payload = self._queue.get(timeout=5)
            except queue.Empty:
                continue
            if payload is None: break
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
                    if attempt < 3: time.sleep(0.5)

    def push_update(self, payload):
        """Neustes Update in Queue (verdraengt altes)."""
        try:
            while not self._queue.empty(): self._queue.get_nowait()
        except queue.Empty: pass
        try: self._queue.put_nowait(payload)
        except queue.Full: pass

    def test_connection(self):
        print(f"Teste Verbindung zu {self.base_url} ...")
        result = self._http("/api/health", method="GET")
        if result and result.get("status") == "ok":
            print(f"Verbindung OK. Version: {result.get('version', '?')}")
            return True
        print(f"FEHLER: Verbindung fehlgeschlagen.")
        if self.last_error: print(f"  {self.last_error}")
        return False

    def stop(self):
        try: self._queue.put_nowait(None)
        except queue.Full: pass


# ============================================================================
# Terminal-Anzeige
# ============================================================================
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
    """Zeigt den aktuellen Ergebnisstand im Terminal."""
    clear_screen()
    ts = datetime.now().strftime("%H:%M:%S")
    clock = state.get("clock", "")

    # API-Status
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

    clock_str = f"  WL:{clock}" if clock else ""
    print(f"\n  {BOLD}WODA Client{RESET}  {GRAY}[{ts}{clock_str}]{RESET}{extras}\n")

    # Modus + Kategorie
    mode_names = {0: "Startliste", 1: "Zwischenstand", 2: "Zwischenstand", 3: "Endergebnis"}
    mode = state.get("display_mode", 0)
    category = state.get("category", "")
    if category:
        print(f"  {BOLD}{CYAN}{mode_names.get(mode, '?')}:  {category}{RESET}")

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
    headers = state.get("headers", [])
    if not headers:
        headers = ["Rang", "StNr", "Name, Vorname", "Verein",
                   "Vbd", "Laufzeit", "Rueckstand"]

    if not rows:
        print(f"  {DIM}Warte auf Ergebnisse...{RESET}")
        print(f"\n  {GRAY}[Enter] zum Beenden{RESET}")
        return

    ncols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(min(len(row), ncols)):
            widths[i] = max(widths[i], len(str(row[i])))
    widths = [max(w, 4) for w in widths]
    # Name und Verein breiter
    if ncols > 2: widths[2] = max(widths[2], 20)
    if ncols > 3: widths[3] = max(widths[3], 18)
    right_align = {0, 1} | {i for i in range(5, ncols)}

    def fmt(val, i):
        s = str(val)[:widths[i]]
        return f"{s:>{widths[i]}}" if i in right_align else f"{s:<{widths[i]}}"

    sep = "-" * (sum(widths) + 3 * ncols + 1)
    hdr = " | ".join(f"{BOLD}{fmt(h, i)}{RESET}" for i, h in enumerate(headers))
    print(f"  | {hdr} |")
    print(f"  {sep}")

    for idx, row in enumerate(rows):
        cols = " | ".join(fmt(row[i] if i < len(row) else "", i) for i in range(ncols))
        if idx == 0:
            print(f"  | {BLUE_BG}{WHITE}{cols}{RESET} |")
        else:
            print(f"  | {cols} |")

    print(f"  {sep}")
    print(f"  {DIM}{len(rows)} Teilnehmer{RESET}")
    print(f"\n  {GRAY}Empfange von {state.get('host','?')}:{state.get('port','?')} ..."
          f" [Enter] zum Beenden{RESET}")


# ============================================================================
# Winlaufen Client
# ============================================================================
class WinlaufenClient:
    def __init__(self, host="127.0.0.1", port=4444, debug=False, api_pusher=None):
        self.host = host
        self.port = port
        self.debug = debug
        self.api_pusher = api_pusher
        self.state = {"host": host, "port": port, "api_pusher": api_pusher}
        self.update_count = 0
        self._sock = None
        self._running = False

    def run(self):
        print(f"{BOLD}Verbinde mit Winlaufen-PC {self.host}:{self.port} ...{RESET}")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10)
        try:
            self._sock.connect((self.host, self.port))
            print(f"{GREEN}Verbunden.{RESET}")
        except (ConnectionRefusedError, OSError) as e:
            print(f"{RED}Verbindung fehlgeschlagen: {e}{RESET}")
            self._sock.close()
            return
        self._sock.settimeout(None)
        self._running = True
        threading.Thread(target=self._receive_loop, daemon=True).start()
        try: input()
        except (EOFError, KeyboardInterrupt): pass
        self._shutdown()

    def _shutdown(self):
        if not self._running: return
        self._running = False
        print(f"\n{YELLOW}Beende...{RESET}")
        if self._sock:
            try: self._sock.close()
            except OSError: pass
        if self.api_pusher: self.api_pusher.stop()

    def _receive_loop(self):
        """
        Hauptschleife: Liest Objekte aus dem Stream und verteilt sie.

        Der Stream sendet drei Arten von Daten:
        1. Uhrzeit-Strings: "Uhr" + HH:MM:SS (jede Sekunde)
        2. Nachrichten: JavaObject (Vector) gefolgt von "nachricht"
        3. Update-Bloecke: Wettkampfname (String) gefolgt von 12 weiteren Objekten

        Zwischen Update-Bloecken und nach Pausen kann Winlaufen den
        Stream neu starten (neuer 0xACED Header). Das erkennen wir
        durch Peek auf die naechsten Bytes.
        """
        reader = SocketReader(self._sock)
        parser = JavaObjectStreamParser(reader, debug=self.debug)

        try:
            parser.read_stream_header()
            self._log("Stream-Header OK")

            while self._running:
                # Naechstes Objekt lesen
                # Stream-Neustarts (0xACED) werden direkt in _dispatch()
                # erkannt und als "RESET" zurueckgegeben
                obj = parser.read_object()
                val = resolve(obj)

                # --- Uhrzeit-String ("UhrHH:MM:SS") -> ueberspringen ---
                if isinstance(val, str) and val.startswith("Uhr"):
                    self.state["clock"] = val[3:]  # "19:31:08"
                    continue

                # --- Steuermarker -> ueberspringen ---
                if val == "RESET":
                    continue
                if isinstance(val, str) and val in ("ende", "tabelle", "ENDBLOCKDATA"):
                    continue

                # --- Nachricht (String gefolgt von "nachricht") ---
                if isinstance(val, str) and val == "nachricht":
                    self._log("Nachricht-Marker empfangen")
                    continue

                # --- JavaObject am Top-Level = Nachricht/unbekannt ---
                # Nachrichten von Winlaufen kommen als JavaObject (Vector)
                # gefolgt vom String "nachricht". Wir ueberspringen das Object
                # und der "nachricht"-String wird im naechsten Durchlauf erkannt.
                if isinstance(val, JavaObject):
                    self._log("JavaObject uebersprungen (vermutlich Nachricht)")
                    continue

                # --- Update-Block: Wettkampfname (String) ---
                if isinstance(val, str):
                    self._read_update_block(parser, val)
                    continue

                # Alles andere ueberspringen
                self._log(f"Uebersprungen: {type(val).__name__}")

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

    def _read_update_block(self, parser, wettkampf):
        """
        Liest einen Update-Block (Wettkampfname wurde bereits gelesen).
        Struktur laut SprecherPC-Dokumentation:
          1. Auswertungsmodus (Integer)
          2. Anzahl Klassen (Integer)
          3. Klassenbezeichnungen (String[])
          4. Rundenzahl/Teamgroesse (int[])
          5. Position WinSpringen (Integer)
          6. Sprecher-Nr = Kategorie-Index (Integer)
          7. Runde/Durchgang (Integer)
          8. Aktueller Einlauf (Integer)
          9ff. Tabellenzeilen (Object[]) bis "tabelle"
          danach: Spaltenueberschriften (String[])
          danach: "ende"
        """
        self.state["wettkampf"] = wettkampf

        # 1. Auswertungsmodus
        self.state["display_mode"] = resolve(parser.read_object())

        # 2. Anzahl Klassen
        resolve(parser.read_object())

        # 3. Klassenbezeichnungen
        cats_obj = parser.read_object()
        categories = resolve(cats_obj) if isinstance(cats_obj, JavaArray) else []
        if not isinstance(categories, list):
            categories = []
        self.state["categories"] = categories

        # 4. Rundenzahl/Teamgroesse
        resolve(parser.read_object())

        # 5. Position WinSpringen
        resolve(parser.read_object())

        # 6. Sprecher-Nr (Kategorie-Index)
        sel_cat = resolve(parser.read_object())
        self.state["selected_category"] = sel_cat

        # Kategoriename bestimmen
        category = ""
        if isinstance(sel_cat, int) and 0 <= sel_cat < len(categories):
            cat_val = categories[sel_cat]
            if isinstance(cat_val, str):
                category = cat_val
        # Fallback wenn kein gueltiger Index
        if not category:
            category = wettkampf if wettkampf != "Standardwettkampf" else ""
        if not category and categories:
            first = categories[0]
            if isinstance(first, str):
                category = first
        if not category:
            category = "Ergebnisse"
        self.state["category"] = category

        # 7. Runde/Durchgang
        resolve(parser.read_object())

        # 8. Aktueller Einlauf
        resolve(parser.read_object())

        # 9ff. Tabellenzeilen bis "tabelle"
        # Ein Stream-Neustart (RESET) kann auch mitten im Block kommen
        rows = []
        MAX_ROWS = 2000
        reset_during_rows = False
        while True:
            val = resolve(parser.read_object())
            if isinstance(val, str) and val == "tabelle":
                break
            if val == "RESET":
                reset_during_rows = True
                self._log("Stream-Neustart waehrend Tabellenzeilen")
                break
            if isinstance(val, list):
                rows.append([str(v) if v is not None else "" for v in val])
            if len(rows) >= MAX_ROWS:
                break

        # Bei Stream-Neustart: Block abbrechen, Daten bis hierher verwenden
        if reset_during_rows:
            self.state["rows"] = rows
            if rows:
                self.update_count += 1
                self._log(f"Teilblock #{self.update_count}: {category} - {len(rows)} Zeilen (abgebrochen)")
                display_update(self.state)
                if self.api_pusher:
                    self.api_pusher.push_update({
                        "category": category,
                        "rows": rows,
                        "headers": self.state.get("headers", []),
                        "timestamp": datetime.now().isoformat(),
                    })
            return

        self.state["rows"] = rows

        # Spaltenueberschriften
        headers_obj = parser.read_object()
        headers = resolve(headers_obj) if isinstance(headers_obj, JavaArray) else None
        if isinstance(headers, list) and len(headers) > 0:
            self.state["headers"] = [str(h) for h in headers]

        # Ende-Marker
        ende = resolve(parser.read_object())
        if isinstance(ende, str) and ende != "ende":
            self._log(f"Warnung: Erwartete 'ende', bekam '{ende}'")

        self.update_count += 1
        self._log(f"Update #{self.update_count}: {category} - {len(rows)} Zeilen, "
                  f"{len(self.state.get('headers', []))} Spalten")

        # Terminal aktualisieren
        display_update(self.state)

        # An WODA Server senden
        if self.api_pusher and rows:
            self.api_pusher.push_update({
                "category": category,
                "rows": rows,
                "headers": self.state.get("headers", []),
                "timestamp": datetime.now().isoformat(),
            })


# ============================================================================
# Programmstart
# ============================================================================
def main():
    p = argparse.ArgumentParser(
        description="WODA Client - Winlaufen Online Data Addon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Beispiele:
  %(prog)s --winlaufen-pc 192.168.0.100
  %(prog)s --winlaufen-pc 192.168.0.100 \\
      --api-endpoint mein-server.de --api-token GEHEIM

{DISCLAIMER}
""")

    wl = p.add_argument_group("Winlaufen")
    wl.add_argument("--winlaufen-pc", default="127.0.0.1",
                    help="IP des Winlaufen-PC (default: 127.0.0.1)")
    wl.add_argument("--port", type=int, default=4444,
                    help="Port (default: 4444)")

    api = p.add_argument_group("WODA Server")
    api.add_argument("--api-endpoint", help="IP/Hostname des WODA Servers")
    api.add_argument("--api-port", type=int, default=8443, help="Port (default: 8443)")
    api.add_argument("--api-token", default=os.environ.get("WODA_API_TOKEN", ""),
                     help="API-Token (oder WODA_API_TOKEN)")
    api.add_argument("--api-test", action="store_true",
                     help="Verbindung testen und beenden")

    p.add_argument("--debug", "-d", action="store_true", help="Debug-Ausgabe")
    args = p.parse_args()
    print(f"{DIM}{DISCLAIMER}{RESET}\n")

    if args.api_test:
        if not args.api_endpoint or not args.api_token:
            print(f"{RED}--api-endpoint und --api-token erforderlich{RESET}")
            sys.exit(1)
        ap = ApiPusher(args.api_endpoint, args.api_port, args.api_token, args.debug)
        sys.exit(0 if ap.test_connection() else 1)

    api_pusher = None
    if args.api_endpoint:
        if not args.api_token:
            print(f"{RED}--api-token erforderlich{RESET}")
            sys.exit(1)
        api_pusher = ApiPusher(args.api_endpoint, args.api_port, args.api_token, args.debug)
        print(f"WODA Server: {api_pusher.base_url}")
        if not api_pusher.base_url.startswith("https://"):
            print(f"{YELLOW}HINWEIS: HTTP (unverschluesselt){RESET}\n")

    if not api_pusher:
        print(f"{GRAY}Kein WODA Server konfiguriert. Nur Konsolenanzeige.{RESET}\n")

    WinlaufenClient(host=args.winlaufen_pc, port=args.port,
                    debug=args.debug, api_pusher=api_pusher).run()

if __name__ == "__main__":
    main()
