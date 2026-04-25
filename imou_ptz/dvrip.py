"""Dahua DVRIP binary protocol client for PTZ control."""

import hashlib
import json
import socket
import struct
import threading
import time


class DahuaDVRIP:
    """Dahua DVRIP protocol client for PTZ control with auto-reconnect."""

    def __init__(self, host, port=37777, username="admin", password=""):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sock = None
        self.session_id = 0
        self.cmd_id = 1
        self._lock = threading.Lock()

    def connect(self):
        self.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((self.host, self.port))

        realm_req = bytes.fromhex("a0010000" + "00" * 20 + "050201010000a1aa")
        self.sock.sendall(realm_req)
        resp = self.sock.recv(4096)

        text = resp[32:].decode("ascii", errors="ignore")
        realm = random_val = ""
        for line in text.split("\r\n"):
            if line.startswith("Realm:"):
                realm = line[6:]
            elif line.startswith("Random:"):
                random_val = line[7:]
        if not realm or not random_val:
            raise ConnectionError("Failed to get realm/random from camera")

        hash_data = self._build_auth_hash(realm, random_val)
        hash_bytes = hash_data.encode("ascii")
        login_hdr = struct.pack("<I", 0x000005A0)
        login_hdr += struct.pack("<I", len(hash_bytes))
        login_hdr += b"\x00" * 16
        login_hdr += bytes.fromhex("050200080000a1aa")
        self.sock.sendall(login_hdr + hash_bytes)

        resp = self.sock.recv(4096)
        if len(resp) < 10 or resp[8:10] != b"\x00\x08":
            raise ConnectionError("DVRIP authentication failed")

        self.session_id = struct.unpack("<I", resp[16:20])[0]

    def _build_auth_hash(self, realm, random_val):
        user, pwd = self.username, self.password
        ha1 = hashlib.md5(f"{user}:{realm}:{pwd}".encode()).hexdigest().upper()
        gen2 = hashlib.md5(
            f"{user}:{random_val}:{ha1}".encode()
        ).hexdigest().upper()
        gen1 = hashlib.md5(
            f"{user}:{random_val}:{self._compressor_hash(pwd)}".encode()
        ).hexdigest().upper()
        return f"{user}&&{gen2}{gen1}"

    @staticmethod
    def _compressor_hash(password):
        digest = hashlib.md5(password.encode()).digest()
        charset = (
            "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
        )
        return "".join(
            charset[(digest[i] + digest[i + 1]) % 62] for i in range(0, 16, 2)
        )

    def _send_json(self, method, params):
        payload = json.dumps({
            "method": method, "params": params,
            "id": self.cmd_id, "session": self.session_id,
        }).encode("ascii")
        self.cmd_id += 1

        hdr = struct.pack("<I", 0xF6)
        hdr += struct.pack("<I", len(payload))
        hdr += struct.pack("<I", self.cmd_id)
        hdr += b"\x00" * 4
        hdr += struct.pack("<I", len(payload))
        hdr += b"\x00" * 4
        hdr += struct.pack("<I", self.session_id)
        hdr += b"\x00" * 4

        self.sock.sendall(hdr + payload)
        try:
            self.sock.settimeout(2)
            return self.sock.recv(4096)
        except TimeoutError:
            return b""
        finally:
            self.sock.settimeout(10)

    def _ensure_connected(self):
        if self.sock is None:
            self.connect()

    def ptz(self, action, code, speed=5, channel=0, arg1=0):
        """Send a PTZ command with auto-reconnect."""
        with self._lock:
            try:
                self._ensure_connected()
            except Exception:
                self.connect()
            params = {
                "channel": channel, "code": code,
                "arg1": arg1, "arg2": speed, "arg3": 0,
            }
            try:
                self._send_json(f"ptz.{action}", params)
            except OSError:
                self.connect()
                self._send_json(f"ptz.{action}", params)

    def ptz_start(self, code, speed=5):
        self.ptz("start", code, speed=speed)

    def ptz_stop(self, code="Right", speed=5):
        self.ptz("stop", code, speed=speed)

    def ptz_move(self, code, speed=5, duration=0.5):
        self.ptz_start(code, speed=speed)
        if duration > 0:
            time.sleep(duration)
            self.ptz_stop(code, speed=speed)

    def ptz_goto_preset(self, preset_num):
        self.ptz("start", "GotoPreset", arg1=preset_num)

    def ptz_set_preset(self, preset_num):
        self.ptz("start", "SetPreset", arg1=preset_num)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

