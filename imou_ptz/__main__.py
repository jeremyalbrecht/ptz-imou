"""CLI entry point. Run with: python -m imou_ptz"""

import argparse
import os

from .constants import PTZ_CODES
from .dvrip import DahuaDVRIP
from .server import run_server


def _camera_args(parser):
    parser.add_argument("--host", default=os.environ.get("CAMERA_HOST", "192.168.1.7"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CAMERA_DVRIP_PORT", "37777")),
    )
    parser.add_argument("--user", default=os.environ.get("CAMERA_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("CAMERA_PASSWORD", ""))
    return parser


def main():
    parent = _camera_args(argparse.ArgumentParser(add_help=False))
    parser = argparse.ArgumentParser(
        description="IMOU/Dahua ONVIF PTZ Proxy for Frigate"
    )
    sub = parser.add_subparsers(dest="command")

    serve_p = _camera_args(
        sub.add_parser("serve", parents=[parent], help="Run ONVIF PTZ proxy server")
    )
    serve_p.add_argument("--bind", default="0.0.0.0")
    serve_p.add_argument(
        "--api-port",
        type=int,
        default=int(os.environ.get("API_PORT", "8000")),
    )

    move_p = _camera_args(
        sub.add_parser("move", parents=[parent], help="Execute a PTZ movement")
    )
    move_p.add_argument("code", choices=[c.lower() for c in PTZ_CODES])
    move_p.add_argument("--speed", type=int, default=5)
    move_p.add_argument("--duration", type=float, default=0.5)

    _camera_args(sub.add_parser("stop", parents=[parent], help="Stop PTZ movement"))

    args = parser.parse_args()

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
        print(
            f"Moving {code_map[args.code]} speed={args.speed} duration={args.duration}s"
        )
        cam.ptz_move(code_map[args.code], speed=args.speed, duration=args.duration)
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

