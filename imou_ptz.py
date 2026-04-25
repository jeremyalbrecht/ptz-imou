#!/usr/bin/env python3
"""
Compatibility shim — the implementation now lives in the imou_ptz/ package.
Run with:  python -m imou_ptz  (or keep using this file directly)

IMOU / Dahua ONVIF PTZ Proxy for Frigate

Acts as an ONVIF-compatible PTZ server that translates ONVIF SOAP commands
to the Dahua DVRIP binary protocol (port 37777). Designed for cameras where
the built-in ONVIF PTZ is non-functional (e.g. IMOU Ranger 2C / IPC-TA22C).

Run as a sidecar or standalone service alongside Frigate NVR. Point Frigate's
ONVIF config to this proxy and PTZ controls will work in the Frigate web UI.

Also exposes a simple HTTP JSON API for direct PTZ control.
"""

import argparse
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PTZ_CODES = [
    "Left", "Right", "Up", "Down",
    "LeftUp", "LeftDown", "RightUp", "RightDown",
    "ZoomTele", "ZoomWide",
    "FocusNear", "FocusFar",
]


# --------------- Dahua DVRIP Protocol ---------------

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

        # Session ID at offset 16 (not 4 — offset 4 is data length)
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
        except socket.timeout:
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
            except (socket.error, OSError):
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


# --------------- ONVIF XML Helpers ---------------

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"


def find_element(root, local_name):
    """Find element by local name recursively, ignoring namespaces."""
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == local_name:
            return elem
    return None


def detect_operation(body_bytes):
    """Detect ONVIF operation name from SOAP body."""
    try:
        root = ET.fromstring(body_bytes)
    except ET.ParseError:
        return None, None
    for ns in (SOAP12, SOAP11):
        body = root.find(f"{{{ns}}}Body")
        if body is not None:
            for child in body:
                local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                return local, child
    return None, None


def soap_envelope(body_xml):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'<s:Body>{body_xml}</s:Body>'
        '</s:Envelope>'
    )


def velocity_to_ptz(pan, tilt, zoom):
    """Map ONVIF velocity (-1..1) to DVRIP direction code and speed (1-8).

    Speed is capped at PTZ_MAX_SPEED (default 3) for small, controlled steps.
    """
    max_speed = int(os.environ.get("PTZ_MAX_SPEED", "3"))
    if abs(zoom) > 0.01:
        return (
            "ZoomTele" if zoom > 0 else "ZoomWide",
            max(1, min(max_speed, round(abs(zoom) * max_speed))),
        )
    has_pan, has_tilt = abs(pan) > 0.01, abs(tilt) > 0.01
    if has_pan and has_tilt:
        if pan > 0:
            code = "RightUp" if tilt > 0 else "RightDown"
        else:
            code = "LeftUp" if tilt > 0 else "LeftDown"
    elif has_pan:
        code = "Right" if pan > 0 else "Left"
    elif has_tilt:
        code = "Up" if tilt > 0 else "Down"
    else:
        return None, 0
    return code, max(1, min(max_speed, round(max(abs(pan), abs(tilt)) * max_speed)))


# --------------- ONVIF + HTTP Request Handler ---------------

class RequestHandler(BaseHTTPRequestHandler):
    """Handles both ONVIF SOAP (POST) and HTTP JSON API (GET) requests."""

    dvrip = None          # shared DahuaDVRIP instance
    camera_config = {}    # camera connection params

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _base_url(self):
        host = self.headers.get(
            "Host", f"localhost:{self.server.server_port}"
        )
        return f"http://{host}"

    def _send_xml(self, body_xml, status=200):
        data = soap_envelope(body_xml).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- HTTP JSON API (GET) ----

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        params = parse_qs(urlparse(self.path).query)

        if path == "/health":
            self._send_json({"status": "ok"})
        elif path == "/ptz/move":
            code = params.get("code", [None])[0]
            speed = int(params.get("speed", ["5"])[0])
            duration = float(params.get("duration", ["0.5"])[0])
            if not code or code not in PTZ_CODES:
                self._send_json(
                    {"error": f"Invalid code. Use: {PTZ_CODES}"}, 400
                )
                return
            try:
                self.dvrip.ptz_move(code, speed=speed, duration=duration)
                self._send_json(
                    {"status": "ok", "code": code, "speed": speed}
                )
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/ptz/stop":
            try:
                self.dvrip.ptz_stop()
                self._send_json({"status": "ok", "action": "stop"})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "Not found"}, 404)

    # ---- ONVIF SOAP (POST) ----

    # Operation name → handler method
    _HANDLERS = {
        # Device service
        "GetSystemDateAndTime": "_onvif_date_time",
        "GetDeviceInformation": "_onvif_device_info",
        "GetCapabilities": "_onvif_capabilities",
        "GetServices": "_onvif_services",
        "GetScopes": "_onvif_scopes",
        # Media service
        "GetProfiles": "_onvif_profiles",
        "GetProfile": "_onvif_profiles",
        "GetStreamUri": "_onvif_stream_uri",
        "GetSnapshotUri": "_onvif_snapshot_uri",
        "GetVideoSources": "_onvif_video_sources",
        # PTZ service
        "GetServiceCapabilities": "_onvif_ptz_capabilities",
        "GetNodes": "_onvif_nodes",
        "GetNode": "_onvif_nodes",
        "GetConfigurations": "_onvif_configurations",
        "GetConfiguration": "_onvif_configurations",
        "GetConfigurationOptions": "_onvif_configuration_options",
        "GetCompatibleConfigurations": "_onvif_configurations",
        "GetPresets": "_onvif_presets",
        "GetStatus": "_onvif_status",
        "ContinuousMove": "_onvif_continuous_move",
        "Stop": "_onvif_stop",
        "RelativeMove": "_onvif_relative_move",
        "AbsoluteMove": "_onvif_absolute_move",
        "GotoPreset": "_onvif_goto_preset",
        "SetPreset": "_onvif_set_preset",
        "RemovePreset": "_onvif_remove_preset",
        "GotoHomePosition": "_onvif_noop",
        "SetHomePosition": "_onvif_noop",
    }

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        op, elem = detect_operation(body)

        if op is None:
            self._send_json({"error": "Invalid SOAP request"}, 400)
            return

        handler_name = self._HANDLERS.get(op)
        if handler_name:
            self.log_message("ONVIF %s", op)
            getattr(self, handler_name)(elem)
        else:
            self.log_message("ONVIF unknown operation: %s", op)
            self._send_xml(
                '<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code>'
                '<s:Reason><s:Text xml:lang="en">'
                f'Operation not supported: {op}'
                '</s:Text></s:Reason></s:Fault>',
                500,
            )

    # ---- Device service responses ----

    def _onvif_date_time(self, _elem):
        now = datetime.now(timezone.utc)
        self._send_xml(
            '<tds:GetSystemDateAndTimeResponse>'
            '<tds:SystemDateAndTime>'
            '<tt:DateTimeType>NTP</tt:DateTimeType>'
            '<tt:DaylightSavings>false</tt:DaylightSavings>'
            '<tt:UTCDateTime>'
            f'<tt:Time><tt:Hour>{now.hour}</tt:Hour>'
            f'<tt:Minute>{now.minute}</tt:Minute>'
            f'<tt:Second>{now.second}</tt:Second></tt:Time>'
            f'<tt:Date><tt:Year>{now.year}</tt:Year>'
            f'<tt:Month>{now.month}</tt:Month>'
            f'<tt:Day>{now.day}</tt:Day></tt:Date>'
            '</tt:UTCDateTime>'
            '</tds:SystemDateAndTime>'
            '</tds:GetSystemDateAndTimeResponse>'
        )

    def _onvif_device_info(self, _elem):
        self._send_xml(
            '<tds:GetDeviceInformationResponse>'
            '<tds:Manufacturer>IMOU</tds:Manufacturer>'
            '<tds:Model>IPC-TA22C</tds:Model>'
            '<tds:FirmwareVersion>2.680.0000000.30.R</tds:FirmwareVersion>'
            '<tds:SerialNumber>DVRIP-PTZ-PROXY</tds:SerialNumber>'
            '<tds:HardwareId>1.0</tds:HardwareId>'
            '</tds:GetDeviceInformationResponse>'
        )

    def _onvif_capabilities(self, _elem):
        base = self._base_url()
        self._send_xml(
            '<tds:GetCapabilitiesResponse><tds:Capabilities>'
            f'<tt:Device><tt:XAddr>{base}/onvif/device_service</tt:XAddr>'
            '</tt:Device>'
            f'<tt:Media><tt:XAddr>{base}/onvif/media_service</tt:XAddr>'
            '<tt:StreamingCapabilities>'
            '<tt:RTPMulticast>false</tt:RTPMulticast>'
            '<tt:RTP_TCP>true</tt:RTP_TCP>'
            '<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>'
            '</tt:StreamingCapabilities></tt:Media>'
            f'<tt:PTZ><tt:XAddr>{base}/onvif/ptz_service</tt:XAddr></tt:PTZ>'
            '</tds:Capabilities></tds:GetCapabilitiesResponse>'
        )

    def _onvif_services(self, _elem):
        base = self._base_url()
        self._send_xml(
            '<tds:GetServicesResponse>'
            '<tds:Service>'
            '<tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace>'
            f'<tds:XAddr>{base}/onvif/device_service</tds:XAddr>'
            '<tds:Version><tt:Major>2</tt:Major>'
            '<tt:Minor>0</tt:Minor></tds:Version>'
            '</tds:Service>'
            '<tds:Service>'
            '<tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>'
            f'<tds:XAddr>{base}/onvif/media_service</tds:XAddr>'
            '<tds:Version><tt:Major>2</tt:Major>'
            '<tt:Minor>0</tt:Minor></tds:Version>'
            '</tds:Service>'
            '<tds:Service>'
            '<tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace>'
            f'<tds:XAddr>{base}/onvif/ptz_service</tds:XAddr>'
            '<tds:Version><tt:Major>2</tt:Major>'
            '<tt:Minor>0</tt:Minor></tds:Version>'
            '</tds:Service>'
            '</tds:GetServicesResponse>'
        )

    def _onvif_scopes(self, _elem):
        self._send_xml(
            '<tds:GetScopesResponse>'
            '<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef>'
            '<tt:ScopeItem>onvif://www.onvif.org/type/ptz</tt:ScopeItem>'
            '</tds:Scopes>'
            '<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef>'
            '<tt:ScopeItem>onvif://www.onvif.org/type/video_encoder</tt:ScopeItem>'
            '</tds:Scopes>'
            '</tds:GetScopesResponse>'
        )

    # ---- Media service responses ----

    _PTZ_VEL_SPACE = (
        "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
    )
    _ZOOM_VEL_SPACE = (
        "http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"
    )

    def _onvif_profiles(self, _elem):
        self._send_xml(
            '<trt:GetProfilesResponse>'
            '<trt:Profiles token="MainProfile" fixed="true">'
            '<tt:Name>MainProfile</tt:Name>'
            '<tt:VideoSourceConfiguration token="VSC_1">'
            '<tt:Name>VideoSource</tt:Name>'
            '<tt:UseCount>1</tt:UseCount>'
            '<tt:SourceToken>VideoSource_1</tt:SourceToken>'
            '<tt:Bounds x="0" y="0" width="1920" height="1080"/>'
            '</tt:VideoSourceConfiguration>'
            '<tt:VideoEncoderConfiguration token="VEC_1">'
            '<tt:Name>VideoEncoder</tt:Name>'
            '<tt:UseCount>1</tt:UseCount>'
            '<tt:Encoding>H264</tt:Encoding>'
            '<tt:Resolution>'
            '<tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>'
            '</tt:Resolution>'
            '<tt:RateControl>'
            '<tt:FrameRateLimit>25</tt:FrameRateLimit>'
            '<tt:BitrateLimit>4096</tt:BitrateLimit>'
            '</tt:RateControl>'
            '</tt:VideoEncoderConfiguration>'
            '<tt:PTZConfiguration token="PTZConfig_1">'
            '<tt:Name>PTZ</tt:Name>'
            '<tt:UseCount>1</tt:UseCount>'
            '<tt:NodeToken>PTZNode_1</tt:NodeToken>'
            '<tt:DefaultContinuousPanTiltVelocitySpace>'
            f'{self._PTZ_VEL_SPACE}'
            '</tt:DefaultContinuousPanTiltVelocitySpace>'
            '<tt:DefaultContinuousZoomVelocitySpace>'
            f'{self._ZOOM_VEL_SPACE}'
            '</tt:DefaultContinuousZoomVelocitySpace>'
            '</tt:PTZConfiguration>'
            '</trt:Profiles>'
            '</trt:GetProfilesResponse>'
        )

    def _onvif_stream_uri(self, _elem):
        c = self.camera_config
        uri = (
            f"rtsp://{c['username']}:{c['password']}"
            f"@{c['host']}:554/cam/realmonitor?channel=1&subtype=0"
        )
        self._send_xml(
            '<trt:GetStreamUriResponse>'
            f'<trt:MediaUri><tt:Uri>{uri}</tt:Uri>'
            '<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>'
            '<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>'
            '<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri>'
            '</trt:GetStreamUriResponse>'
        )

    def _onvif_snapshot_uri(self, _elem):
        self._send_xml(
            '<trt:GetSnapshotUriResponse>'
            '<trt:MediaUri><tt:Uri></tt:Uri>'
            '<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>'
            '<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>'
            '<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri>'
            '</trt:GetSnapshotUriResponse>'
        )

    def _onvif_video_sources(self, _elem):
        self._send_xml(
            '<trt:GetVideoSourcesResponse>'
            '<trt:VideoSources token="VideoSource_1">'
            '<tt:Resolution>'
            '<tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>'
            '</tt:Resolution>'
            '</trt:VideoSources>'
            '</trt:GetVideoSourcesResponse>'
        )

    # ---- PTZ service responses ----

    def _onvif_ptz_capabilities(self, _elem):
        self._send_xml(
            '<tptz:GetServiceCapabilitiesResponse>'
            '<tptz:Capabilities EFlip="false" Reverse="false"/>'
            '</tptz:GetServiceCapabilitiesResponse>'
        )

    def _onvif_nodes(self, _elem):
        self._send_xml(
            '<tptz:GetNodesResponse>'
            '<tptz:PTZNode token="PTZNode_1" FixedHomePosition="false">'
            '<tt:Name>PTZ</tt:Name>'
            '<tt:SupportedPTZSpaces>'
            '<tt:ContinuousPanTiltVelocitySpace>'
            f'<tt:URI>{self._PTZ_VEL_SPACE}</tt:URI>'
            '<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>'
            '<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>'
            '</tt:ContinuousPanTiltVelocitySpace>'
            '<tt:ContinuousZoomVelocitySpace>'
            f'<tt:URI>{self._ZOOM_VEL_SPACE}</tt:URI>'
            '<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>'
            '</tt:ContinuousZoomVelocitySpace>'
            '</tt:SupportedPTZSpaces>'
            '<tt:MaximumNumberOfPresets>8</tt:MaximumNumberOfPresets>'
            '<tt:HomeSupported>false</tt:HomeSupported>'
            '</tptz:PTZNode>'
            '</tptz:GetNodesResponse>'
        )

    def _onvif_configurations(self, _elem):
        self._send_xml(
            '<tptz:GetConfigurationsResponse>'
            '<tptz:PTZConfiguration token="PTZConfig_1">'
            '<tt:Name>PTZ</tt:Name>'
            '<tt:UseCount>1</tt:UseCount>'
            '<tt:NodeToken>PTZNode_1</tt:NodeToken>'
            '<tt:DefaultContinuousPanTiltVelocitySpace>'
            f'{self._PTZ_VEL_SPACE}'
            '</tt:DefaultContinuousPanTiltVelocitySpace>'
            '<tt:DefaultContinuousZoomVelocitySpace>'
            f'{self._ZOOM_VEL_SPACE}'
            '</tt:DefaultContinuousZoomVelocitySpace>'
            '</tptz:PTZConfiguration>'
            '</tptz:GetConfigurationsResponse>'
        )

    def _onvif_configuration_options(self, _elem):
        self._send_xml(
            '<tptz:GetConfigurationOptionsResponse>'
            '<tptz:PTZConfigurationOptions>'
            '<tt:Spaces>'
            '<tt:ContinuousPanTiltVelocitySpace>'
            f'<tt:URI>{self._PTZ_VEL_SPACE}</tt:URI>'
            '<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>'
            '<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>'
            '</tt:ContinuousPanTiltVelocitySpace>'
            '<tt:ContinuousZoomVelocitySpace>'
            f'<tt:URI>{self._ZOOM_VEL_SPACE}</tt:URI>'
            '<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>'
            '</tt:ContinuousZoomVelocitySpace>'
            '</tt:Spaces>'
            '</tptz:PTZConfigurationOptions>'
            '</tptz:GetConfigurationOptionsResponse>'
        )

    def _onvif_presets(self, _elem):
        presets = "".join(
            f'<tptz:Preset token="{i}">'
            f'<tt:Name>Preset {i}</tt:Name></tptz:Preset>'
            for i in range(1, 9)
        )
        self._send_xml(
            f'<tptz:GetPresetsResponse>{presets}'
            f'</tptz:GetPresetsResponse>'
        )

    def _onvif_status(self, _elem):
        pos_space = (
            "http://www.onvif.org/ver10/tptz/PanTiltSpaces/"
            "PositionGenericSpace"
        )
        zoom_space = (
            "http://www.onvif.org/ver10/tptz/ZoomSpaces/"
            "PositionGenericSpace"
        )
        self._send_xml(
            '<tptz:GetStatusResponse><tptz:PTZStatus>'
            '<tt:Position>'
            f'<tt:PanTilt x="0" y="0" space="{pos_space}"/>'
            f'<tt:Zoom x="0" space="{zoom_space}"/>'
            '</tt:Position>'
            '<tt:MoveStatus>'
            '<tt:PanTilt>IDLE</tt:PanTilt>'
            '<tt:Zoom>IDLE</tt:Zoom>'
            '</tt:MoveStatus>'
            '</tptz:PTZStatus></tptz:GetStatusResponse>'
        )

    # ---- PTZ movement handlers (→ DVRIP) ----

    def _onvif_continuous_move(self, elem):
        pan = tilt = zoom = 0.0
        pt = find_element(elem, "PanTilt")
        if pt is not None:
            pan = float(pt.get("x", "0"))
            tilt = float(pt.get("y", "0"))
        z = find_element(elem, "Zoom")
        if z is not None:
            zoom = float(z.get("x", "0"))

        code, speed = velocity_to_ptz(pan, tilt, zoom)
        # Use short timed steps instead of truly continuous movement
        step_dur = float(os.environ.get("PTZ_STEP_DURATION", "0.3"))
        if code:
            try:
                self.dvrip.ptz_move(code, speed=speed, duration=step_dur)
                self.log_message(
                    "PTZ ContinuousMove → %s speed=%d step=%.1fs", code, speed, step_dur
                )
            except Exception as e:
                self.log_message("PTZ error: %s", e)
        self._send_xml('<tptz:ContinuousMoveResponse/>')

    def _onvif_stop(self, _elem):
        try:
            self.dvrip.ptz_stop()
            self.log_message("PTZ Stop")
        except Exception as e:
            self.log_message("PTZ error: %s", e)
        self._send_xml('<tptz:StopResponse/>')

    def _onvif_relative_move(self, elem):
        pan = tilt = zoom = 0.0
        pt = find_element(elem, "PanTilt")
        if pt is not None:
            pan = float(pt.get("x", "0"))
            tilt = float(pt.get("y", "0"))
        z = find_element(elem, "Zoom")
        if z is not None:
            zoom = float(z.get("x", "0"))

        code, speed = velocity_to_ptz(pan, tilt, zoom)
        if code:
            dur = max(0.1, min(2.0, max(abs(pan), abs(tilt), abs(zoom))))
            try:
                self.dvrip.ptz_move(code, speed=speed, duration=dur)
                self.log_message(
                    "PTZ RelativeMove → %s speed=%d dur=%.1f",
                    code, speed, dur,
                )
            except Exception as e:
                self.log_message("PTZ error: %s", e)
        self._send_xml('<tptz:RelativeMoveResponse/>')

    def _onvif_absolute_move(self, _elem):
        self._send_xml('<tptz:AbsoluteMoveResponse/>')

    def _onvif_goto_preset(self, elem):
        token_elem = find_element(elem, "PresetToken")
        preset = 1
        if token_elem is not None and token_elem.text:
            try:
                preset = int(token_elem.text)
            except ValueError:
                pass
        try:
            self.dvrip.ptz_goto_preset(preset)
            self.log_message("PTZ GotoPreset %d", preset)
        except Exception as e:
            self.log_message("PTZ error: %s", e)
        self._send_xml('<tptz:GotoPresetResponse/>')

    def _onvif_set_preset(self, elem):
        token_elem = find_element(elem, "PresetToken")
        preset = 1
        if token_elem is not None and token_elem.text:
            try:
                preset = int(token_elem.text)
            except ValueError:
                pass
        try:
            self.dvrip.ptz_set_preset(preset)
            self.log_message("PTZ SetPreset %d", preset)
        except Exception as e:
            self.log_message("PTZ error: %s", e)
        self._send_xml(
            f'<tptz:SetPresetResponse>'
            f'<tptz:PresetToken>{preset}</tptz:PresetToken>'
            f'</tptz:SetPresetResponse>'
        )

    def _onvif_remove_preset(self, _elem):
        self._send_xml('<tptz:RemovePresetResponse/>')

    def _onvif_noop(self, _elem):
        self._send_xml('<Response/>')


# --------------- Server ---------------

def run_server(bind, port, camera_config):
    dvrip = DahuaDVRIP(
        host=camera_config["host"],
        port=camera_config.get("dvrip_port", 37777),
        username=camera_config["username"],
        password=camera_config["password"],
    )
    print(
        f"Connecting to camera "
        f"{camera_config['host']}:{camera_config.get('dvrip_port', 37777)}..."
    )
    dvrip.connect()
    print("DVRIP authenticated successfully.")

    RequestHandler.dvrip = dvrip
    RequestHandler.camera_config = camera_config

    server = HTTPServer((bind, port), RequestHandler)
    print(f"\nONVIF PTZ Proxy listening on {bind}:{port}")
    print(f"  ONVIF:  POST /onvif/device_service")
    print(f"          POST /onvif/media_service")
    print(f"          POST /onvif/ptz_service")
    print(f"  HTTP:   GET  /health")
    print(f"          GET  /ptz/move?code=Right&speed=5&duration=0.5")
    print(f"          GET  /ptz/stop")
    server.serve_forever()


# --------------- CLI ---------------

def main():
    # Shared camera args across all subcommands
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--host",
        default=os.environ.get("CAMERA_HOST", "192.168.1.7"),
        help="Camera IP (env: CAMERA_HOST)",
    )
    parent.add_argument(
        "--port", type=int,
        default=int(os.environ.get("CAMERA_DVRIP_PORT", "37777")),
        help="DVRIP port (env: CAMERA_DVRIP_PORT)",
    )
    parent.add_argument(
        "--user",
        default=os.environ.get("CAMERA_USER", "admin"),
        help="Username (env: CAMERA_USER)",
    )
    parent.add_argument(
        "--password",
        default=os.environ.get("CAMERA_PASSWORD", ""),
        help="Password (env: CAMERA_PASSWORD)",
    )

    parser = argparse.ArgumentParser(
        description="IMOU/Dahua ONVIF PTZ Proxy for Frigate"
    )
    sub = parser.add_subparsers(dest="command")

    # serve (default)
    serve_p = sub.add_parser(
        "serve", parents=[parent],
        help="Run ONVIF PTZ proxy server",
    )
    serve_p.add_argument("--bind", default="0.0.0.0", help="Bind address")
    serve_p.add_argument(
        "--api-port", type=int,
        default=int(os.environ.get("API_PORT", "8000")),
        help="Server port (env: API_PORT)",
    )

    # move
    move_p = sub.add_parser(
        "move", parents=[parent], help="Execute a PTZ movement",
    )
    move_p.add_argument(
        "code", choices=[c.lower() for c in PTZ_CODES],
        help="Direction to move",
    )
    move_p.add_argument("--speed", type=int, default=5, help="Speed 1-8")
    move_p.add_argument(
        "--duration", type=float, default=0.5,
        help="Duration in seconds (0 = continuous)",
    )

    # stop
    sub.add_parser("stop", parents=[parent], help="Stop PTZ movement")

    args = parser.parse_args()

    # Default to serve when no subcommand given
    if args.command is None:
        args.command = "serve"
        args.host = os.environ.get("CAMERA_HOST", "192.168.1.7")
        args.port = int(os.environ.get("CAMERA_DVRIP_PORT", "37777"))
        args.user = os.environ.get("CAMERA_USER", "admin")
        args.password = os.environ.get("CAMERA_PASSWORD", "")
        args.bind = "0.0.0.0"
        args.api_port = int(os.environ.get("API_PORT", "8000"))

    camera_config = {
        "host": args.host,
        "dvrip_port": args.port,
        "username": args.user,
        "password": args.password,
    }

    if args.command == "serve":
        run_server(args.bind, args.api_port, camera_config)

    elif args.command == "move":
        code_map = {c.lower(): c for c in PTZ_CODES}
        cam = DahuaDVRIP(args.host, args.port, args.user, args.password)
        cam.connect()
        print(f"Moving {code_map[args.code]} speed={args.speed} "
              f"duration={args.duration}s")
        cam.ptz_move(code_map[args.code],
                     speed=args.speed, duration=args.duration)
        cam.close()
        print("Done.")

    elif args.command == "stop":
        cam = DahuaDVRIP(args.host, args.port, args.user, args.password)
        cam.connect()
        cam.ptz_stop()
        cam.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
