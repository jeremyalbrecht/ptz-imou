"""Server startup."""

from http.server import HTTPServer

from .dvrip import DahuaDVRIP
from .handler import RequestHandler


def run_server(bind, port, camera_config):
    dvrip = DahuaDVRIP(
        host=camera_config["host"],
        port=camera_config.get("dvrip_port", 37777),
        username=camera_config["username"],
        password=camera_config["password"],
    )
    host = camera_config['host']
    dvrip_port = camera_config.get('dvrip_port', 37777)
    print(f"Connecting to camera {host}:{dvrip_port}...")
    dvrip.connect()
    print("DVRIP authenticated successfully.")

    RequestHandler.dvrip = dvrip
    RequestHandler.camera_config = camera_config

    server = HTTPServer((bind, port), RequestHandler)
    print(f"\nONVIF PTZ Proxy listening on {bind}:{port}")
    print("  ONVIF:  POST /onvif/device_service")
    print("          POST /onvif/media_service")
    print("          POST /onvif/ptz_service")
    print("  HTTP:   GET  /health")
    print("          GET  /ptz/move?code=Right&speed=5&duration=0.5")
    print("          GET  /ptz/stop")
    server.serve_forever()

