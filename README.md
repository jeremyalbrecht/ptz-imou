# IMOU / Dahua ONVIF PTZ Proxy

ONVIF-compatible PTZ proxy for IMOU Ranger 2C (IPC-TA22C) and compatible Dahua cameras. Translates standard ONVIF PTZ commands to the Dahua DVRIP binary protocol (port 37777), enabling PTZ control from **Frigate NVR** and other ONVIF clients.

Built for cameras where the native ONVIF PTZ is non-functional (stub that accepts commands but never moves).

## How it works

```
Frigate Web UI → ONVIF SOAP → [This Proxy] → DVRIP binary protocol → Camera moves
```

## Quick Start

```bash
# Run directly
python3 imou_ptz.py serve --host 192.168.1.7 --password YOUR_PASSWORD

# Or with Docker
docker build -t imou-ptz .
docker run -e CAMERA_HOST=192.168.1.7 -e CAMERA_PASSWORD=YOUR_PASSWORD -p 8000:8000 imou-ptz
```

## Frigate Configuration

Point Frigate's ONVIF config at this proxy instead of the camera:

```yaml
cameras:
  imou_ranger:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://admin:YOUR_PASSWORD@192.168.1.7:554/cam/realmonitor?channel=1&subtype=0
          roles:
            - detect
            - record
    onvif:
      host: imou-ptz-proxy    # This proxy's hostname/IP
      port: 8000
      user: admin              # Ignored by proxy but required by Frigate
      password: YOUR_PASSWORD   # Ignored by proxy but required by Frigate
```

PTZ controls will appear in the Frigate web UI once the ONVIF connection succeeds.

## CLI Usage

```bash
# Move camera
python3 imou_ptz.py move right --speed 5 --duration 0.5
python3 imou_ptz.py move leftup --speed 3 --duration 1.0

# Stop movement
python3 imou_ptz.py stop

# Start ONVIF proxy server (default)
python3 imou_ptz.py serve --api-port 8000
python3 imou_ptz.py  # same as above
```

### Directions

`left`, `right`, `up`, `down`, `leftup`, `leftdown`, `rightup`, `rightdown`, `zoomtele`, `zoomwide`, `focusnear`, `focusfar`

## HTTP JSON API

Also exposes a simple HTTP API on the same port:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/ptz/move?code=Right&speed=5&duration=0.5` | GET | Move camera |
| `/ptz/stop` | GET | Stop movement |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CAMERA_HOST` | `192.168.1.7` | Camera IP address |
| `CAMERA_DVRIP_PORT` | `37777` | DVRIP protocol port |
| `CAMERA_USER` | `admin` | Camera username |
| `CAMERA_PASSWORD` | _(empty)_ | Camera password |
| `API_PORT` | `8000` | Server listen port |

## Docker

```bash
docker build -t imou-ptz .
docker run -d \
  -e CAMERA_HOST=192.168.1.7 \
  -e CAMERA_PASSWORD=YOUR_PASSWORD \
  -p 8000:8000 \
  imou-ptz
```

## Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: imou-ptz-proxy
spec:
  replicas: 1
  selector:
    matchLabels:
      app: imou-ptz-proxy
  template:
    metadata:
      labels:
        app: imou-ptz-proxy
    spec:
      containers:
        - name: imou-ptz-proxy
          image: imou-ptz:latest
          ports:
            - containerPort: 8000
          env:
            - name: CAMERA_HOST
              value: "192.168.1.7"
            - name: CAMERA_PASSWORD
              value: "YOUR_PASSWORD"
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 30
---
apiVersion: v1
kind: Service
metadata:
  name: imou-ptz-proxy
spec:
  selector:
    app: imou-ptz-proxy
  ports:
    - port: 8000
      targetPort: 8000
```

## Technical Details

- **Protocol**: Dahua DVRIP binary protocol on port 37777
- **Auth**: Dual-hash (gen1 compressor + gen2 MD5 chain)
- **Session**: Persistent DVRIP connection with auto-reconnect
- **ONVIF**: Standalone server (Device + Media + PTZ services)
- **Zero dependencies**: Python 3.12+ stdlib only
