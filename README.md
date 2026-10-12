# RoomTone

RoomTone is a small browser-based voice room built with FastAPI, WebSockets, and WebRTC.

## Quick install on Ubuntu

Use a fresh Ubuntu 22.04 or 24.04 VPS with a public IP address and a domain/subdomain whose DNS points to that IP. Allow TCP ports 80 and 443 in the VPS firewall. The installer configures Caddy to obtain and renew HTTPS certificates automatically.

After the repository is published, the one-line installer will be:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/RZAsadi/roomtone/main/install.sh)
```

The installer asks for the domain, installs Python, Caddy, and coturn, creates a dedicated service account, configures a systemd service, and enables HTTPS. It generates a private TURN shared secret and the app issues time-limited TURN credentials from `/ice-config`.

In the VPS provider firewall, allow TCP 80 and 443, TCP/UDP 3478, and UDP 49160–49200. The installer adds the same rules to UFW when UFW is active. The TURN hostname uses the domain entered during installation, so that domain must resolve to the VPS public IP.

## Manual install

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Put a reverse proxy with HTTPS in front of Uvicorn for microphone access and WebSocket signaling. `Caddyfile.example` and `roomtone.service.example` are examples for production deployment.

## Voice connectivity

WebRTC tries direct media first and uses the locally installed coturn relay when necessary. The app generates time-limited TURN REST credentials; do not expose the shared secret. UDP relay ports must be reachable from the internet for media to flow.

## Security and limitations

- The current app uses in-memory room state and should run with one Uvicorn worker.
- Room names are not access control. Do not expose sensitive conversations until authentication and room authorization are implemented.
- Use a maintained Ubuntu release, keep the VPS updated, and restrict SSH access.
