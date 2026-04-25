FROM python:3.12-slim

WORKDIR /app

# Copy package
COPY imou_ptz/ ./imou_ptz/

# Camera connection (override via env vars or args)
ENV CAMERA_HOST=192.168.1.7
ENV CAMERA_DVRIP_PORT=37777
ENV CAMERA_USER=admin
ENV CAMERA_PASSWORD=
ENV API_PORT=8000

EXPOSE 8000

ENTRYPOINT ["python3", "-m", "imou_ptz"]
CMD ["serve"]
