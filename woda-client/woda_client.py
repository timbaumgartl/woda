#!/usr/bin/env python3
"""
Winlaufen Online Data Addon Client (WODA Client)
=================================================
Verbindet sich mit dem Winlaufen Zeitnahme-Server und leitet
Live-Ergebnisse weiter an:
  - WODA Server (REST-API fuer Webseiten-Anzeige)
  - Telegram-Chat (Bot-API)
  - Konsole (lokale Anzeige)

Erstellt von:
    Claude (Anthropic) im Auftrag des Nutzers.

Grundlage:
    Das Kommunikationsprotokoll des Winlaufen Sprecher-PC wurde durch
    Analyse von Netzwerkmitschnitten (pcapng) reverse-engineered.
    Der Server sendet ueber TCP (Port 4444) einen Java-ObjectOutputStream-
    Datenstrom. Jeder Update-Block enthaelt: Wettkampfart, Anzeigemodus,
    Kategorien mit Zaehlern, Ergebniszeilen (je 7 Spalten: Rang, StNr,
    Name, Verein, Verband, Laufzeit, Rueckstand), Spaltenueberschriften
    und einen Ende-Marker. Der Client empfaengt passiv (reiner Push).

HINWEIS: Dieses Programm ist ein unabhaengiges Open-Source-Projekt und
steht in keiner Verbindung zu Winlaufen oder dessen Entwicklern. Es
wird ohne Support, Garantie oder Gewaehrleistung bereitgestellt. Die
Nutzung erfolgt auf eigene Verantwortung. Winlaufen ist ein eingetragenes
Produkt seiner jeweiligen Rechteinhaber.

Usage:
    python3 woda_client.py --winlaufen-pc 192.168.0.100

    python3 woda_client.py --winlaufen-pc 192.168.0.100 \\
        --api-endpoint mein-server.de --api-token GEHEIM

    python3 woda_client.py --winlaufen-pc 192.168.0.100 \\
        --telegram-token "123456:ABC..." --telegram-chat-id "-100..."

    python3 woda_client.py --winlaufen-pc 192.168.0.100 \\
        --api-endpoint mein-server.de --api-token GEHEIM \\
        --telegram-token "123456:ABC..." --telegram-chat-id "-100..."
"""

import socket
import struct
import sys
import argparse
import os
import json
import threading
import ssl
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DISCLAIMER = (
    "Dieses Programm ist ein unabhaengiges Projekt und steht in keiner\n"
    "Verbindung zu Winlaufen oder dessen Entwicklern. Keine Garantie,\n"
    "kein Support. Nutzung auf eigene Verantwortung."
)

# Telegram Nachrichten-Limit
TG_MAX_LEN = 4000  # 4096 offiziell, 4000 mit Puffer


# ==============================================================================
# Java Serialization Constants
# ==============================================================================
STREAM_MAGIC        = 0xACED
STREAM_VERSION      = 5
TC_NULL             = 0x70
TC_REFERENCE        = 0x71
TC_CLASSDESC        = 0x72
TC_OBJECT           = 0x73
TC_STRING           = 0x74
TC_ARRAY            = 0x75
TC_CLASS            = 0x76
TC_BLOCKDATA        = 0x77
TC_ENDBLOCKDATA     = 0x78
TC_RESET            = 0x79
TC_BLOCKDATALONG    = 0x7A
TC_EXCEPTION        = 0x7B
TC_LONGSTRING       = 0x7C
TC_PROXYCLASSDESC   = 0x7D
TC_ENUM             = 0x7E
SC_SERIALIZABLE     = 0x02
SC_WRITE_METHOD     = 0x01
BASE_WIRE_HANDLE    = 0x7E0000


# ==============================================================================
# Buffered Socket Reader
# ==============================================================================
# Schutz gegen Speichererschoepfung durch fehlerhafte/manipulierte Daten
MAX_READ_SIZE = 10 * 1024 * 1024    # 10 MB pro Einzelleseoperation
MAX_ARRAY_ELEMENTS = 10000          # Max Array-Elemente (Winlaufen: ~200)
MAX_STRING_LENGTH = 1 * 1024 * 1024 # 1 MB pro String
MAX_FIELDS = 500                    # Max Felder pro Java-Klasse
MAX_HANDLES = 100000                # Max Handle-Eintraege (Referenz-Tabelle)
MAX_ANNOTATIONS = 1000              # Max Annotationen pro Block


class SocketReader:
    def __init__(self, sock, bufsize=8192):
        self.sock = sock
        self.buf = b""
        self.bufsize = bufsize

    def read(self, n):
        if n > MAX_READ_SIZE:
            raise ValueError(f"Leseanforderung zu gross: {n} Bytes (max {MAX_READ_SIZE})")
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(self.bufsize)
            except OSError as e:
                raise ConnectionError(f"Verbindung unterbrochen: {e}")
            if not chunk:
                raise ConnectionError("Verbindung vom Server getrennt")
            self.buf += chunk
        result, self.buf = self.buf[:n], self.buf[n:]
        return result

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


# ==============================================================================
# Java Serialization Data Structures
# ==============================================================================
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


# ==============================================================================
# Java ObjectInputStream Parser
# ==============================================================================
class JavaObjectStreamParser:
    MAX_DEPTH = 50  # Schutz gegen Stack-Overflow bei verschachtelten Objekten

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
        if len(self.handles) >= MAX_HANDLES:
            raise ValueError(f"Handle-Tabelle voll ({MAX_HANDLES} Eintraege)")
        h = self.next_handle
        self.next_handle += 1
        self.handles[h] = obj
        return h

    def read_stream_header(self):
        magic = self.reader.read_ushort()
        version = self.reader.read_ushort()
        if magic != STREAM_MAGIC:
            raise ValueError(f"Ungueltiger Stream-Magic: 0x{magic:04x}")
        if version != STREAM_VERSION:
            raise ValueError(f"Ungueltige Stream-Version: {version}")

    def read_object(self):
        self._depth += 1
        if self._depth > self.MAX_DEPTH:
            raise ValueError(f"Maximale Verschachtelungstiefe ({self.MAX_DEPTH}) ueberschritten")
        try:
            return self._read_object_dispatch()
        finally:
            self._depth -= 1

    def _read_object_dispatch(self):
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
            raise ValueError(f"String zu gross: {length} Bytes (max {MAX_STRING_LENGTH})")
        s = self.reader.read(length).decode("utf-8", errors="replace")
        self._assign_handle(s)
        return s

    def _read_class_desc(self):
        name = self.reader.read_utf()
        serial_uid = self.reader.read_long()
        handle = self._assign_handle(None)
        flags = self.reader.read_byte()
        field_count = self.reader.read_ushort()
        if field_count > MAX_FIELDS:
            raise ValueError(f"Zu viele Felder: {field_count} (max {MAX_FIELDS})")
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
        if iface_count < 0 or iface_count > MAX_FIELDS:
            raise ValueError(f"Zu viele Interfaces: {iface_count} (max {MAX_FIELDS})")
        interfaces = [self.reader.read_utf() for _ in range(iface_count)]
        self._read_annotations()
        super_desc = self.read_object()
        desc = JavaClassDesc(f"Proxy({','.join(interfaces)})", 0, 0, [], super_desc)
        self.handles[handle] = desc
        return desc

    def _read_annotations(self):
        for _ in range(MAX_ANNOTATIONS):
            tc = self.reader.read_byte()
            if tc == TC_ENDBLOCKDATA: return
            elif tc == TC_BLOCKDATA:     self.reader.read(self.reader.read_byte())
            elif tc == TC_BLOCKDATALONG: self.reader.read(self.reader.read_int())
            elif tc == TC_STRING:        self._read_string()
            elif tc == TC_REFERENCE:     self._read_reference()
            elif tc == TC_OBJECT:        self._read_object()
            elif tc == TC_NULL:          pass
            else: raise ValueError(f"Unerwarteter TC in Annotation: 0x{tc:02x}")
        raise ValueError(f"Annotationsblock zu lang (max {MAX_ANNOTATIONS} Eintraege)")

    def _read_class(self):
        desc = self.read_object()
        self._assign_handle(desc)
        return desc

    def _read_object(self):
        desc = self.read_object()
        handle = self._assign_handle(None)
        obj = JavaObject(desc)
        self._read_object_fields(obj, desc)
        self.handles[handle] = obj
        return obj

    def _read_object_fields(self, obj, desc):
        if not isinstance(desc, JavaClassDesc): return
        if desc.super_desc and isinstance(desc.super_desc, JavaClassDesc):
            self._read_object_fields(obj, desc.super_desc)
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
            raise ValueError(f"Array zu gross: {count} Elemente (max {MAX_ARRAY_ELEMENTS})")
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


# ==============================================================================
# Value Resolution
# ==============================================================================
def resolve(val):
    if val is None:            return None
    if isinstance(val, str):   return val
    if isinstance(val, (int, float)): return val
    if isinstance(val, JavaObject):
        v = val.values.get("value")
        return v if v is not None else str(val)
    if isinstance(val, JavaArray):
        return [resolve(e) for e in val.elements]
    return str(val)


# ==============================================================================
# WODA Server API Pusher
# ==============================================================================
class ApiPusher:
    """
    Sendet Ergebnisse an den WODA Server per HTTP POST.
    """

    def __init__(self, endpoint, port=8443, token="", debug=False):
        self.endpoint = endpoint.rstrip("/")
        self.port = port
        self.token = token
        self.debug = debug
        # URL: Nutzer kann http:// oder https:// explizit angeben.
        # Ohne Schema wird http:// verwendet (uvicorn laeuft ohne SSL).
        if self.endpoint.startswith("http://") or self.endpoint.startswith("https://"):
            self.base_url = f"{self.endpoint}:{self.port}"
        else:
            self.base_url = f"http://{self.endpoint}:{self.port}"

    def _log(self, msg):
        if self.debug:
            print(f"  [API: {msg}]", file=sys.stderr)

    def _post(self, path, payload):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        })
        # SSL-Kontext nur bei https verwenden
        ctx = ssl.create_default_context() if url.startswith("https://") else None
        try:
            with urlopen(req, timeout=15, context=ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self._log(f"HTTP {e.code}: {body[:200]}")
            return None
        except (URLError, OSError) as e:
            self._log(f"Verbindungsfehler: {e}")
            return None

    def test_connection(self):
        """Testet die Verbindung zum WODA Server."""
        print(f"Teste Verbindung zu {self.base_url} ...")
        result = self._post("/api/health", {})
        if result and result.get("status") == "ok":
            print(f"Verbindung zum WODA Server erfolgreich.")
            print(f"  Server-Version: {result.get('version', '?')}")
            return True
        # Fallback: GET request
        try:
            url = f"{self.base_url}/api/health"
            req = Request(url, headers={"Authorization": f"Bearer {self.token}"})
            ctx = ssl.create_default_context() if url.startswith("https://") else None
            with urlopen(req, timeout=10, context=ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("status") == "ok":
                    print(f"Verbindung zum WODA Server erfolgreich.")
                    return True
        except Exception:
            pass
        print(f"FEHLER: Verbindung zum WODA Server fehlgeschlagen.")
        print(f"  Ist der Server erreichbar unter {self.base_url}?")
        print(f"  Stimmt das API-Token?")
        return False

    def push_update(self, state):
        """Sendet ein Update an den WODA Server (asynchron)."""
        payload = {
            "wettkampf": state.get("wettkampf", ""),
            "display_mode": state.get("display_mode", 0),
            "categories": state.get("categories", []),
            "selected_category": state.get("selected_category", -1),
            "category": state.get("category", ""),
            "headers": state.get("headers", []),
            "rows": state.get("rows", []),
            "timestamp": datetime.now().isoformat(),
        }

        def _do():
            result = self._post("/api/update", payload)
            if result:
                self._log(f"Update gesendet ({len(state.get('rows', []))} Zeilen)")
            else:
                self._log("Update fehlgeschlagen")

        threading.Thread(target=_do, daemon=True).start()


# ==============================================================================
# Telegram Bot Integration
# ==============================================================================
class TelegramNotifier:
    """
    Sendet Ergebnistabellen an einen Telegram-Chat.
    Unterstuetzt grosse Tabellen durch automatisches Aufteilen
    in mehrere Nachrichten (Telegram-Limit: 4096 Zeichen).
    """

    API_BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token, chat_id, debug=False):
        self.token = token
        self.chat_id = str(chat_id)
        self.debug = debug
        self.last_message_ids = {}  # {category: [msg_id, ...]}
        self.last_category = None

    def _log(self, msg):
        if self.debug:
            print(f"  [TG: {msg}]", file=sys.stderr)

    def _api_call(self, method, payload):
        url = self.API_BASE.format(token=self.token, method=method)
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("ok"):
                    return result.get("result")
                else:
                    self._log(f"API-Fehler: {result.get('description', '?')}")
                    return None
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self._log(f"HTTP {e.code}: {body[:200]}")
            return None
        except (URLError, OSError) as e:
            self._log(f"Netzwerkfehler: {e}")
            return None

    def send_message(self, text, parse_mode="HTML", disable_notification=False):
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }
        result = self._api_call("sendMessage", payload)
        if result:
            return result.get("message_id")
        return None

    def edit_message(self, message_id, text, parse_mode="HTML"):
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        result = self._api_call("editMessageText", payload)
        return result is not None

    def delete_message(self, message_id):
        payload = {"chat_id": self.chat_id, "message_id": message_id}
        return self._api_call("deleteMessage", payload) is not None

    def test_connection(self):
        me = self._api_call("getMe", {})
        if not me:
            print(f"FEHLER: Telegram Bot-Token ungueltig!")
            return False
        bot_name = me.get("first_name", "?")
        bot_user = me.get("username", "?")
        print(f"Bot verbunden: {bot_name} (@{bot_user})")

        if self.chat_id and self.chat_id != "0":
            msg_id = self.send_message(
                "<b>WODA Client</b>\n\n"
                "Telegram-Verbindung erfolgreich.\n"
                "Der Bot ist bereit, Ergebnisse zu senden.\n\n"
                f"<i>{DISCLAIMER}</i>",
            )
            if msg_id:
                print(f"Testnachricht an Chat {self.chat_id} gesendet.")
                return True
            else:
                print(f"FEHLER: Konnte nicht an Chat {self.chat_id} senden!")
                return False
        else:
            print(f"\nKeine --telegram-chat-id angegeben.")
            print(f"So findest du deine Chat-ID:")
            print(f"  1. Sende eine Nachricht an @{bot_user}")
            token_masked = self.token[:8] + "..." if len(self.token) > 8 else "***"
            print(f"  2. Oeffne: https://api.telegram.org/bot{token_masked}/getUpdates")
            print(f"     (Ersetze den maskierten Teil durch dein vollstaendiges Token)")
            print(f"  3. Suche nach 'chat':{{\"id\":XXXXXX}}")
            print(f"  4. Nutze diese ID als --telegram-chat-id")
            return True

    # --------------------------------------------------------------------------
    # Tabellen-Formatierung mit automatischem Splitting
    # --------------------------------------------------------------------------
    def _format_table_parts(self, state):
        """
        Erzeugt eine oder mehrere Telegram-Nachrichten fuer die Tabelle.
        Teilt automatisch auf wenn das Zeichenlimit ueberschritten wird.

        Returns: list[str] - Liste der Nachrichtentexte
        """
        category = state.get("category", "")
        rows = state.get("rows", [])
        ts = datetime.now().strftime("%H:%M:%S")

        if not rows:
            return [f"<b>{category}</b>\n\nWarte auf Ergebnisse..."]

        # Spaltenbreiten berechnen
        headers = ["Rg", "St", "Name", "Verein", "Vbd", "Zeit", "Rckst"]
        col_max = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row[:7]):
                col_max[i] = max(col_max[i], len(str(val)))

        def format_row(row):
            rang   = row[0] if len(row) > 0 else ""
            stnr   = row[1] if len(row) > 1 else ""
            name   = row[2] if len(row) > 2 else ""
            verein = row[3] if len(row) > 3 else ""
            vbd    = row[4] if len(row) > 4 else ""
            zeit   = row[5] if len(row) > 5 else ""
            rueck  = row[6] if len(row) > 6 else ""
            return (
                f"{rang:>{col_max[0]}s} "
                f"{stnr:>{col_max[1]}s} "
                f"{name:<{col_max[2]}s} "
                f"{verein:<{col_max[3]}s} "
                f"{vbd:<{col_max[4]}s} "
                f"{zeit:>{col_max[5]}s} "
                f"{rueck:>{col_max[6]}s}"
            )

        hdr_line = (
            f"{headers[0]:>{col_max[0]}s} "
            f"{headers[1]:>{col_max[1]}s} "
            f"{headers[2]:<{col_max[2]}s} "
            f"{headers[3]:<{col_max[3]}s} "
            f"{headers[4]:<{col_max[4]}s} "
            f"{headers[5]:>{col_max[5]}s} "
            f"{headers[6]:>{col_max[6]}s}"
        )
        sep_line = (
            f"{'-'*col_max[0]} "
            f"{'-'*col_max[1]} "
            f"{'-'*col_max[2]} "
            f"{'-'*col_max[3]} "
            f"{'-'*col_max[4]} "
            f"{'-'*col_max[5]} "
            f"{'-'*col_max[6]}"
        )

        # Versuche alles in eine Nachricht
        header_text = f"<b>{category}</b>  ({ts})\n\n<pre>\n{hdr_line}\n{sep_line}\n"
        footer_text = f"</pre>\n{len(rows)} Laeufer im Ziel"
        body_lines = [format_row(r) for r in rows]
        full_msg = header_text + "\n".join(body_lines) + "\n" + footer_text

        if len(full_msg) <= TG_MAX_LEN:
            return [full_msg]

        # Aufteilen in mehrere Nachrichten
        self._log(f"Tabelle zu gross ({len(full_msg)} Zeichen), teile auf")
        parts = []
        part_num = 1
        chunk_rows = []
        chunk_start = 0

        for i, row_line in enumerate(body_lines):
            chunk_rows.append(row_line)
            # Teste ob aktuelle Chunk-Groesse noch passt
            part_header = (
                f"<b>{category}</b>  ({ts}) "
                f"[{chunk_start + 1}-{chunk_start + len(chunk_rows)}"
                f" / {len(rows)}]\n\n<pre>\n{hdr_line}\n{sep_line}\n"
            )
            part_footer = f"</pre>"
            test_msg = part_header + "\n".join(chunk_rows) + "\n" + part_footer

            if len(test_msg) > TG_MAX_LEN and len(chunk_rows) > 1:
                # Letzte Zeile gehoert zum naechsten Teil
                chunk_rows.pop()
                part_header = (
                    f"<b>{category}</b>  ({ts}) "
                    f"[{chunk_start + 1}-{chunk_start + len(chunk_rows)}"
                    f" / {len(rows)}]\n\n<pre>\n{hdr_line}\n{sep_line}\n"
                )
                part_msg = part_header + "\n".join(chunk_rows) + "\n</pre>"
                parts.append(part_msg)
                chunk_start += len(chunk_rows)
                chunk_rows = [row_line]
                part_num += 1

        # Letzter Teil
        if chunk_rows:
            part_header = (
                f"<b>{category}</b>  ({ts}) "
                f"[{chunk_start + 1}-{chunk_start + len(chunk_rows)}"
                f" / {len(rows)}]\n\n<pre>\n{hdr_line}\n{sep_line}\n"
            )
            part_footer = f"</pre>\n{len(rows)} Laeufer im Ziel"
            parts.append(part_header + "\n".join(chunk_rows) + "\n" + part_footer)

        self._log(f"Aufgeteilt in {len(parts)} Nachrichten")
        return parts

    # --------------------------------------------------------------------------
    # Update Handler
    # --------------------------------------------------------------------------
    def on_update(self, state):
        """Bei jedem Update die komplette Tabelle senden/aktualisieren."""
        rows = state.get("rows", [])
        category = state.get("category", "")

        if not rows:
            return

        if category != self.last_category:
            self._log(f"Kategorie: {category}")
            self.last_category = category

        parts = self._format_table_parts(state)

        def _do():
            old_ids = self.last_message_ids.get(category, [])

            # Alte Nachrichten loeschen wenn Anzahl sich aendert
            if len(old_ids) != len(parts) and old_ids:
                for mid in old_ids:
                    self.delete_message(mid)
                old_ids = []

            new_ids = []
            for i, text in enumerate(parts):
                if i < len(old_ids):
                    # Bestehende Nachricht editieren
                    success = self.edit_message(old_ids[i], text)
                    if success:
                        new_ids.append(old_ids[i])
                    else:
                        mid = self.send_message(text, disable_notification=True)
                        if mid:
                            new_ids.append(mid)
                else:
                    # Neue Nachricht senden
                    mid = self.send_message(text, disable_notification=(i > 0))
                    if mid:
                        new_ids.append(mid)

            self.last_message_ids[category] = new_ids

        threading.Thread(target=_do, daemon=True).start()


# ==============================================================================
# Console Display
# ==============================================================================
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
    clear_screen()
    ts = datetime.now().strftime("%H:%M:%S")
    parts = []
    if state.get("telegram_active"):
        parts.append(f"{GREEN}[TG]{RESET}")
    if state.get("api_active"):
        parts.append(f"{GREEN}[API]{RESET}")
    extras = "  " + " ".join(parts) if parts else ""

    print(f"\n  {BOLD}WODA Client{RESET}  {GRAY}[{ts}]{RESET}{extras}\n")

    mode_names = {0: "Startliste", 1: "Zwischenstand", 2: "Zwischenstand", 3: "Endergebnis"}
    mode = state.get("display_mode", 0)
    mode_str = mode_names.get(mode, f"Modus {mode}")
    category = state.get("category", "")
    if category:
        print(f"  {BOLD}{CYAN}Aktueller {mode_str}:  {category}{RESET}")

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

    rows = state.get("rows", [])
    headers = state.get("headers",
                        ["Rang", "StNr", "Name, Vorname", "Verein", "Vbd", "Laufzeit", "Rueckstand"])

    if not rows:
        print(f"  {DIM}Warte auf Ergebnisse...{RESET}")
        print(f"\n  {GRAY}[Enter] zum Beenden{RESET}")
        return

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

    host = state.get("host", "?")
    port = state.get("port", "?")
    print(f"\n  {GRAY}Empfange Daten von {host}:{port} ... [Enter] zum Beenden{RESET}")


# ==============================================================================
# Protocol Handler
# ==============================================================================
class WinlaufenClient:
    def __init__(self, host="127.0.0.1", port=4444, debug=False,
                 telegram=None, api_pusher=None):
        self.host = host
        self.port = port
        self.debug = debug
        self.telegram = telegram
        self.api_pusher = api_pusher
        self.state = {"host": host, "port": port}
        if telegram:
            self.state["telegram_active"] = True
        if api_pusher:
            self.state["api_active"] = True
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
            print(f"\n  Ist Winlaufen gestartet auf {self.host}:{self.port}?")
            self._sock.close()
            return

        self._sock.settimeout(None)
        self._running = True

        worker = threading.Thread(target=self._receive_loop, daemon=True)
        worker.start()

        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass

        self._shutdown()

    def _shutdown(self):
        if not self._running:
            return
        self._running = False
        print(f"\n{YELLOW}Beende...{RESET}")
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self.telegram:
            self.telegram.send_message(
                "<b>WODA Client</b>\n\nUebertragung beendet.",
                disable_notification=True,
            )
        print(f"{YELLOW}Beendet.{RESET}")

    def _receive_loop(self):
        reader = SocketReader(self._sock)
        parser = JavaObjectStreamParser(reader, debug=self.debug)

        try:
            parser.read_stream_header()
            self._log("Java ObjectStream Header OK")

            if self.telegram:
                self.telegram.send_message(
                    f"<b>WODA Client</b>\n\n"
                    f"Verbunden mit Winlaufen-PC {self.host}:{self.port}\n"
                    f"Ergebnisse werden gesendet.",
                    disable_notification=True,
                )

            while self._running:
                self._read_update_block(parser)

        except ConnectionError as e:
            if self._running:
                print(f"\n{YELLOW}Verbindung getrennt: {e}{RESET}")
                if self.telegram:
                    self.telegram.send_message(
                        "<b>WODA Client</b>\n\nVerbindung getrennt.",
                        disable_notification=True,
                    )
                print(f"{GRAY}[Enter] zum Beenden{RESET}")
        except Exception as e:
            if self._running:
                print(f"\n{RED}Fehler: {e}{RESET}")
                if self.debug:
                    import traceback
                    traceback.print_exc()
                print(f"{GRAY}[Enter] zum Beenden{RESET}")

    def _log(self, msg):
        if self.debug:
            print(f"{GRAY}[{msg}]{RESET}", file=sys.stderr)

    def _read_update_block(self, parser):
        obj = parser.read_object()
        wettkampf = resolve(obj)

        if wettkampf == "RESET":
            return
        if not isinstance(wettkampf, str):
            return
        if wettkampf in ("ende", "tabelle"):
            return

        self.state["wettkampf"] = wettkampf
        display_mode = resolve(parser.read_object())
        self.state["display_mode"] = display_mode
        resolve(parser.read_object())

        cats_obj = parser.read_object()
        categories = resolve(cats_obj) if isinstance(cats_obj, JavaArray) else []
        self.state["categories"] = categories

        counts_obj = parser.read_object()
        self.state["cat_counts"] = resolve(counts_obj) if isinstance(counts_obj, JavaArray) else []

        resolve(parser.read_object())

        sel_cat = resolve(parser.read_object())
        self.state["selected_category"] = sel_cat
        category = ""
        if isinstance(sel_cat, int) and 0 <= sel_cat < len(categories):
            category = categories[sel_cat]
        self.state["category"] = category

        resolve(parser.read_object())
        resolve(parser.read_object())

        rows = []
        MAX_ROWS_PER_BLOCK = 2000  # Schutz gegen Endlosschleife
        while True:
            obj = parser.read_object()
            val = resolve(obj)
            if isinstance(val, str) and val == "tabelle":
                break
            if isinstance(val, list) and len(val) == 7:
                rows.append([str(v) if v is not None else "" for v in val])
            if len(rows) >= MAX_ROWS_PER_BLOCK:
                self._log(f"Warnung: Max Zeilen ({MAX_ROWS_PER_BLOCK}) erreicht, breche ab")
                break

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

        # Konsole
        display_update(self.state)

        # Telegram
        if self.telegram:
            self.telegram.on_update(self.state)

        # WODA Server
        if self.api_pusher:
            self.api_pusher.push_update(self.state)


# ==============================================================================
# Main
# ==============================================================================
def main():
    p = argparse.ArgumentParser(
        description="WODA Client - Winlaufen Online Data Addon Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Beispiele:
  %(prog)s --winlaufen-pc 192.168.0.100

  %(prog)s --winlaufen-pc 192.168.0.100 \\
      --api-endpoint mein-server.de --api-token GEHEIM

  %(prog)s --winlaufen-pc 192.168.0.100 \\
      --telegram-token "TOKEN" --telegram-chat-id "CHAT_ID"

  %(prog)s --winlaufen-pc 192.168.0.100 \\
      --api-endpoint mein-server.de --api-token GEHEIM \\
      --telegram-token "TOKEN" --telegram-chat-id "CHAT_ID"

{DISCLAIMER}
""",
    )

    # Winlaufen
    wl = p.add_argument_group("Winlaufen Verbindung")
    wl.add_argument("--winlaufen-pc", default="127.0.0.1",
                    help="Hostname / IP des Winlaufen-PC (default: 127.0.0.1)")
    wl.add_argument("--port", type=int, default=4444,
                    help="Port des Winlaufen Sprecher-PC (default: 4444)")

    # WODA Server API
    api = p.add_argument_group("WODA Server Verbindung")
    api.add_argument("--api-endpoint",
                     help="Hostname / IP des WODA Servers")
    api.add_argument("--api-port", type=int, default=8443,
                     help="Port des WODA Servers (default: 8443)")
    api.add_argument("--api-token", default=os.environ.get("WODA_API_TOKEN", ""),
                     help="API-Token (oder Umgebungsvariable WODA_API_TOKEN)")
    api.add_argument("--api-test", action="store_true",
                     help="Verbindung zum WODA Server testen")

    # Telegram
    tg = p.add_argument_group("Telegram (optional)")
    tg.add_argument("--telegram-token", default=os.environ.get("WODA_TELEGRAM_TOKEN", ""),
                    help="Bot-Token (oder Umgebungsvariable WODA_TELEGRAM_TOKEN)")
    tg.add_argument("--telegram-chat-id", default=os.environ.get("WODA_TELEGRAM_CHAT_ID", ""),
                    help="Ziel-Chat-ID (oder Umgebungsvariable WODA_TELEGRAM_CHAT_ID)")
    tg.add_argument("--telegram-test", action="store_true",
                    help="Telegram-Verbindung testen")

    p.add_argument("--debug", "-d", action="store_true",
                    help="Debug-Ausgabe")

    args = p.parse_args()

    print(f"{DIM}{DISCLAIMER}{RESET}\n")

    # --- Tests ---
    if args.telegram_test:
        if not args.telegram_token:
            print(f"{RED}--telegram-token ist erforderlich{RESET}")
            sys.exit(1)
        n = TelegramNotifier(args.telegram_token, args.telegram_chat_id or "0",
                             debug=args.debug)
        sys.exit(0 if n.test_connection() else 1)

    if args.api_test:
        if not args.api_endpoint or not args.api_token:
            print(f"{RED}--api-endpoint und --api-token sind erforderlich{RESET}")
            sys.exit(1)
        ap = ApiPusher(args.api_endpoint, args.api_port, args.api_token,
                       debug=args.debug)
        sys.exit(0 if ap.test_connection() else 1)

    # --- Telegram ---
    telegram = None
    if args.telegram_token:
        if not args.telegram_chat_id:
            print(f"{RED}--telegram-chat-id erforderlich{RESET}")
            sys.exit(1)
        telegram = TelegramNotifier(args.telegram_token, args.telegram_chat_id,
                                    debug=args.debug)
        print(f"Telegram aktiviert.")

    # --- API Pusher ---
    api_pusher = None
    if args.api_endpoint:
        if not args.api_token:
            print(f"{RED}--api-token erforderlich wenn --api-endpoint gesetzt{RESET}")
            sys.exit(1)
        api_pusher = ApiPusher(args.api_endpoint, args.api_port, args.api_token,
                               debug=args.debug)
        print(f"WODA Server: {api_pusher.base_url}")

    if not telegram and not api_pusher:
        print(f"{GRAY}Kein Telegram und kein WODA Server konfiguriert.")
        print(f"Nur Konsolenanzeige aktiv.{RESET}\n")
    elif api_pusher and not api_pusher.base_url.startswith("https://"):
        print(f"{YELLOW}WARNUNG: API-Verbindung laeuft ueber HTTP (unverschluesselt).{RESET}")
        print(f"{YELLOW}  Das API-Token wird im Klartext uebertragen.{RESET}")
        print(f"{YELLOW}  Fuer Produktiveinsatz HTTPS verwenden:{RESET}")
        print(f"{YELLOW}    --api-endpoint https://mein-server.de{RESET}\n")

    # --- Client ---
    client = WinlaufenClient(
        host=args.winlaufen_pc,
        port=args.port,
        debug=args.debug,
        telegram=telegram,
        api_pusher=api_pusher,
    )
    client.run()


if __name__ == "__main__":
    main()
