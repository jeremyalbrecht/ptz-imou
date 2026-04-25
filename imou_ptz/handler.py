"""HTTP request handler: ONVIF SOAP (POST) + JSON API (GET)."""

import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .constants import PTZ_CODES, PTZ_VEL_SPACE, ZOOM_VEL_SPACE
from .onvif import detect_operation, find_element, soap_envelope, velocity_to_ptz


class RequestHandler(BaseHTTPRequestHandler):
    """Handles both ONVIF SOAP (POST) and HTTP JSON API (GET) requests."""

    dvrip = None          # shared DahuaDVRIP instance
    camera_config = {}    # camera connection params

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _base_url(self):
        host = self.headers.get("Host", f"localhost:{self.server.server_port}")
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
                self._send_json({"error": f"Invalid code. Use: {PTZ_CODES}"}, 400)
                return
            try:
                self.dvrip.ptz_move(code, speed=speed, duration=duration)
                self._send_json({"status": "ok", "code": code, "speed": speed})
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

    _HANDLERS = {
        "GetSystemDateAndTime": "_onvif_date_time",
        "GetDeviceInformation": "_onvif_device_info",
        "GetCapabilities": "_onvif_capabilities",
        "GetServices": "_onvif_services",
        "GetScopes": "_onvif_scopes",
        "GetProfiles": "_onvif_profiles",
        "GetProfile": "_onvif_profiles",
        "GetStreamUri": "_onvif_stream_uri",
        "GetSnapshotUri": "_onvif_snapshot_uri",
        "GetVideoSources": "_onvif_video_sources",
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
                f'<s:Reason><s:Text xml:lang="en">Operation not supported: {op}'
                '</s:Text></s:Reason></s:Fault>',
                500,
            )

    # ---- Device service ----

    def _onvif_date_time(self, _elem):
        now = datetime.now(timezone.utc)
        self._send_xml(
            '<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>'
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
            '</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>'
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
            f'<tt:Device><tt:XAddr>{base}/onvif/device_service</tt:XAddr></tt:Device>'
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
            '<tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>'
            '</tds:Service>'
            '<tds:Service>'
            '<tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>'
            f'<tds:XAddr>{base}/onvif/media_service</tds:XAddr>'
            '<tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>'
            '</tds:Service>'
            '<tds:Service>'
            '<tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace>'
            f'<tds:XAddr>{base}/onvif/ptz_service</tds:XAddr>'
            '<tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>'
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

    # ---- Media service ----

    def _onvif_profiles(self, _elem):
        self._send_xml(
            '<trt:GetProfilesResponse>'
            '<trt:Profiles token="MainProfile" fixed="true">'
            '<tt:Name>MainProfile</tt:Name>'
            '<tt:VideoSourceConfiguration token="VSC_1">'
            '<tt:Name>VideoSource</tt:Name><tt:UseCount>1</tt:UseCount>'
            '<tt:SourceToken>VideoSource_1</tt:SourceToken>'
            '<tt:Bounds x="0" y="0" width="1920" height="1080"/>'
            '</tt:VideoSourceConfiguration>'
            '<tt:VideoEncoderConfiguration token="VEC_1">'
            '<tt:Name>VideoEncoder</tt:Name><tt:UseCount>1</tt:UseCount>'
            '<tt:Encoding>H264</tt:Encoding>'
            '<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>'
            '<tt:RateControl>'
            '<tt:FrameRateLimit>25</tt:FrameRateLimit>'
            '<tt:BitrateLimit>4096</tt:BitrateLimit>'
            '</tt:RateControl>'
            '</tt:VideoEncoderConfiguration>'
            '<tt:PTZConfiguration token="PTZConfig_1">'
            '<tt:Name>PTZ</tt:Name><tt:UseCount>1</tt:UseCount>'
            '<tt:NodeToken>PTZNode_1</tt:NodeToken>'
            f'<tt:DefaultContinuousPanTiltVelocitySpace>{PTZ_VEL_SPACE}</tt:DefaultContinuousPanTiltVelocitySpace>'
            f'<tt:DefaultContinuousZoomVelocitySpace>{ZOOM_VEL_SPACE}</tt:DefaultContinuousZoomVelocitySpace>'
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

    # ---- PTZ service ----

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
            f'<tt:URI>{PTZ_VEL_SPACE}</tt:URI>'
            '<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>'
            '<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>'
            '</tt:ContinuousPanTiltVelocitySpace>'
            '<tt:ContinuousZoomVelocitySpace>'
            f'<tt:URI>{ZOOM_VEL_SPACE}</tt:URI>'
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
            '<tt:Name>PTZ</tt:Name><tt:UseCount>1</tt:UseCount>'
            '<tt:NodeToken>PTZNode_1</tt:NodeToken>'
            f'<tt:DefaultContinuousPanTiltVelocitySpace>{PTZ_VEL_SPACE}</tt:DefaultContinuousPanTiltVelocitySpace>'
            f'<tt:DefaultContinuousZoomVelocitySpace>{ZOOM_VEL_SPACE}</tt:DefaultContinuousZoomVelocitySpace>'
            '</tptz:PTZConfiguration>'
            '</tptz:GetConfigurationsResponse>'
        )

    def _onvif_configuration_options(self, _elem):
        self._send_xml(
            '<tptz:GetConfigurationOptionsResponse>'
            '<tptz:PTZConfigurationOptions><tt:Spaces>'
            '<tt:ContinuousPanTiltVelocitySpace>'
            f'<tt:URI>{PTZ_VEL_SPACE}</tt:URI>'
            '<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>'
            '<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>'
            '</tt:ContinuousPanTiltVelocitySpace>'
            '<tt:ContinuousZoomVelocitySpace>'
            f'<tt:URI>{ZOOM_VEL_SPACE}</tt:URI>'
            '<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>'
            '</tt:ContinuousZoomVelocitySpace>'
            '</tt:Spaces></tptz:PTZConfigurationOptions>'
            '</tptz:GetConfigurationOptionsResponse>'
        )

    def _onvif_presets(self, _elem):
        presets = "".join(
            f'<tptz:Preset token="{i}"><tt:Name>Preset {i}</tt:Name></tptz:Preset>'
            for i in range(1, 9)
        )
        self._send_xml(f'<tptz:GetPresetsResponse>{presets}</tptz:GetPresetsResponse>')

    def _onvif_status(self, _elem):
        pos_space = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
        zoom_space = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"
        self._send_xml(
            '<tptz:GetStatusResponse><tptz:PTZStatus>'
            '<tt:Position>'
            f'<tt:PanTilt x="0" y="0" space="{pos_space}"/>'
            f'<tt:Zoom x="0" space="{zoom_space}"/>'
            '</tt:Position>'
            '<tt:MoveStatus>'
            '<tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>IDLE</tt:Zoom>'
            '</tt:MoveStatus>'
            '</tptz:PTZStatus></tptz:GetStatusResponse>'
        )

    # ---- PTZ movement → DVRIP ----

    def _onvif_continuous_move(self, elem):
        pan = tilt = zoom = 0.0
        pt = find_element(elem, "PanTilt")
        if pt is not None:
            pan, tilt = float(pt.get("x", "0")), float(pt.get("y", "0"))
        z = find_element(elem, "Zoom")
        if z is not None:
            zoom = float(z.get("x", "0"))
        code, speed = velocity_to_ptz(pan, tilt, zoom)
        step_dur = float(os.environ.get("PTZ_STEP_DURATION", "0.3"))
        if code:
            try:
                self.dvrip.ptz_move(code, speed=speed, duration=step_dur)
                self.log_message("PTZ ContinuousMove → %s speed=%d step=%.1fs", code, speed, step_dur)
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
            pan, tilt = float(pt.get("x", "0")), float(pt.get("y", "0"))
        z = find_element(elem, "Zoom")
        if z is not None:
            zoom = float(z.get("x", "0"))
        code, speed = velocity_to_ptz(pan, tilt, zoom)
        if code:
            dur = max(0.1, min(2.0, max(abs(pan), abs(tilt), abs(zoom))))
            try:
                self.dvrip.ptz_move(code, speed=speed, duration=dur)
                self.log_message("PTZ RelativeMove → %s speed=%d dur=%.1f", code, speed, dur)
            except Exception as e:
                self.log_message("PTZ error: %s", e)
        self._send_xml('<tptz:RelativeMoveResponse/>')

    def _onvif_absolute_move(self, _elem):
        self._send_xml('<tptz:AbsoluteMoveResponse/>')

    def _onvif_goto_preset(self, elem):
        token_elem = find_element(elem, "PresetToken")
        preset = int(token_elem.text) if token_elem is not None and token_elem.text else 1
        try:
            self.dvrip.ptz_goto_preset(preset)
            self.log_message("PTZ GotoPreset %d", preset)
        except Exception as e:
            self.log_message("PTZ error: %s", e)
        self._send_xml('<tptz:GotoPresetResponse/>')

    def _onvif_set_preset(self, elem):
        token_elem = find_element(elem, "PresetToken")
        preset = int(token_elem.text) if token_elem is not None and token_elem.text else 1
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

