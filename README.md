# RoomTone

RoomTone is a small browser-based voice room built with FastAPI, WebSockets, and WebRTC.

## Quick install on Ubuntu

Use a fresh Ubuntu 22.04 or 24.04 VPS with a public IP address and a domain/subdomain whose DNS points to that IP. Allow TCP ports 80 and 443 in the VPS firewall. The installer configures Caddy to obtain and renew HTTPS certificates automatically.

After the repository is published, the one-line installer will be:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/RZAsadi/roomtone/main/install.sh)
```

The installer asks for the domain, installs Python and Caddy, creates a dedicated service account, configures a systemd service, and enables HTTPS.

## Manual install

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Put a reverse proxy with HTTPS in front of Uvicorn for microphone access and WebSocket signaling. `Caddyfile.example` and `roomtone.service.example` are examples for production deployment.

## Voice connectivity

WebRTC attempts direct media connectivity using STUN. Some networks require a TURN relay. For those networks, configure an authenticated TURN service and add its ICE server URL and credentials to the browser's `rtcConfig` in `app.py`. Do not operate an unauthenticated public TURN relay.

## Security and limitations

- The current app uses in-memory room state and should run with one Uvicorn worker.
- Room names are not access control. Do not expose sensitive conversations until authentication and room authorization are implemented.
- Use a maintained Ubuntu release, keep the VPS updated, and restrict SSH access.
