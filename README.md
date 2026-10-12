# RoomTone

RoomTone is a browser-based voice-room application built with FastAPI, WebSockets, WebRTC, SQLite, and coturn.

## Quick install on Ubuntu

Use a fresh Ubuntu 22.04 or 24.04 VPS with a public IP address and a domain/subdomain whose DNS points to that IP. Allow TCP ports 80 and 443 in the VPS firewall. The installer configures Caddy to obtain and renew HTTPS certificates automatically.

After the repository is published, the one-line installer will be:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/RZAsadi/roomtone/main/install.sh)
```

The installer asks for the domain, installs Python, Caddy, and coturn, creates a dedicated service account, configures a systemd service, and enables HTTPS. It generates a private TURN shared secret and places the matching domain/secret into the RoomTone service environment so `/ice-config` can issue time-limited TURN credentials. Without both `ROOMTONE_TURN_DOMAIN` and `ROOMTONE_TURN_SECRET` in the running service environment, `/ice-config` returns STUN-only configuration and TURN relay is not available. Existing `/opt/roomtone/roomtone.db` data is not overwritten by an application update.

The installer is intended for a fresh or deliberately managed Ubuntu VPS: it replaces `/etc/caddy/Caddyfile`, `/etc/turnserver.conf`, and the RoomTone systemd unit. Review those files and back up the current configuration before rerunning it on an existing deployment. It does not migrate the existing application directory as a versioned release.

In the VPS provider firewall, allow TCP 80 and 443, TCP/UDP 3478, and UDP 49160–49200. The installer adds the same rules to UFW when UFW is active. The TURN hostname uses the domain entered during installation, so that domain must resolve to the VPS public IP.

## Manual install

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Put a reverse proxy with HTTPS in front of Uvicorn for microphone access and WebSocket signaling. `Caddyfile.example` and `roomtone.service.example` are examples for production deployment.

### Environment configuration

- `ROOMTONE_DB_PATH` (optional): SQLite database path. Defaults to `roomtone.db` beside `app.py`; production uses `/opt/roomtone/roomtone.db` by default.
- `ROOMTONE_TURN_DOMAIN` and `ROOMTONE_TURN_SECRET` (optional as a pair): enable time-limited TURN REST credentials in `/ice-config`. The domain must resolve to the coturn host, and the secret must match coturn's `static-auth-secret`. Keep it in a root-controlled environment file, never in browser code, source control, or diagnostic logs.
- The provided systemd unit uses one Uvicorn worker because live room state is in memory. Do not increase worker count without moving signaling state to a shared store. The WebSocket endpoint requires a same-host `Origin` header; non-browser clients and test clients must send an explicit matching origin.

## Rooms and permissions

- Public rooms appear in `GET /api/rooms`; private rooms are deliberately omitted from discovery.
- Create rooms with `POST /api/rooms`. Private rooms require a password of at least six characters; passwords are stored as salted PBKDF2 hashes, not plaintext.
- The creator receives a random host capability token. The browser stores it in local storage for that room. Treat browser access and that token as privileged; this is not account-based authentication.
- Host/moderator actions are checked by the signaling server. Host-only actions include room locking, capacity changes, assigning roles, and removing participants. A moderator can request mute for participants but cannot remove the host.
- Room metadata persists in `roomtone.db`; live sockets and participant presence are in memory. Empty rooms are removed when the host leaves, and an active host role is transferred if the host disconnects while others remain.
- The current mesh WebRTC design is intended for small rooms (maximum 12 participants). It is not an SFU and should not be treated as a large-room conferencing platform.
- Role restrictions and remote mute are enforced through signaling policy and browser controls, but peer-to-peer media has no server-side media gate; use an SFU if you need authoritative enforcement against a modified client.
- TURN currently uses UDP/TCP on port 3478 and the configured UDP relay range. TURN-over-TLS on port 443 is not configured, so some restrictive networks may still need an additional TURN/TLS endpoint.

## Voice connectivity

WebRTC tries direct media first and uses the locally installed coturn relay when necessary. The app generates time-limited TURN REST credentials; do not expose the shared secret. UDP relay ports must be reachable from the internet for media to flow. The client attempts ICE recovery when a peer reports `failed` or `disconnected`; it does not automatically reconnect a lost signaling WebSocket, so users must rejoin after signaling loss. A listener still needs to grant microphone access in the current UI even though the client configures its audio transceiver as receive-only. Microphone selection is available in browsers that support device enumeration.

## Tests and database backup

Run the room metadata tests and real-Uvicorn WebSocket authorization/lifecycle integration tests with the project's Python environment:

```bash
python -m pip install -r requirements.txt
python -m unittest -v test_roomtone test_roomtone_signaling
```

Back up the SQLite database before upgrades or manual edits:

```bash
sudo systemctl stop roomtone
sudo cp -a /opt/roomtone/roomtone.db /opt/roomtone/roomtone.db.backup
sudo systemctl start roomtone
```

The application must continue running with one Uvicorn worker because live signaling state is process-local. A SQLite database backup does not preserve active calls.

## Troubleshooting

- **Room discovery returns 404:** check `sudo systemctl show roomtone -p ExecStart -p WorkingDirectory`, then inspect `grep -nE '@app\\.(get|post|websocket)' /opt/roomtone/app.py`. Confirm the deployed application version matches the intended release before replacing files.
- **Microphone unavailable:** use HTTPS, allow microphone permission, check OS input-device permissions, and select an available microphone in the room controls. Browser privacy settings can block device enumeration until permission is granted.
- **Room joins but there is no audio:** check browser console and WebSocket traffic, then inspect `sudo journalctl -u roomtone -u coturn -n 150 --no-pager`. Confirm the cloud firewall permits TCP/UDP 3478 and UDP 49160–49200. A TCP port probe alone does not validate UDP relay allocation; inspect WebRTC ICE candidate-pair statistics from both clients.
- **Works on one network but not another:** restrictive networks may block direct ICE or TURN/UDP. This deployment does not configure TURN-over-TLS on port 443; add and test a separate TURN/TLS endpoint if that network class must be supported.
- **Room disappears after host leaves:** this is expected for an empty room. Active participant state is ephemeral and will be lost when the service restarts; clients must rejoin after interruption.
- **Check service state:** `sudo systemctl status roomtone coturn caddy --no-pager`, `sudo ss -lntup`, and `sudo caddy validate --config /etc/caddy/Caddyfile`.

## Deployment backup and rollback

Before changing a live deployment, create a root-only backup and record the currently deployed commit/version. Stop RoomTone before copying the SQLite database so the snapshot is consistent, and restart it immediately after the copy. Back up at least `app.py`, `requirements.txt`, `/etc/systemd/system/roomtone.service`, `/etc/caddy/Caddyfile`, `/etc/turnserver.conf`, `/etc/roomtone/turn.env`, and `/opt/roomtone/roomtone.db`; protect backups because the TURN environment file contains a secret. Prefer SQLite's online backup facility or preserve SQLite WAL/SHM files consistently if WAL mode is enabled.

For rollback, restore the prior application files and matching dependencies, restore only the configuration files that changed, run `sudo systemctl daemon-reload` if the unit changed, validate Caddy before reloading it, and restart the affected services. Restore the database only when a schema/data change requires it and the backup is known to be consistent; do not overwrite a newer database unnecessarily. Then verify the homepage, `/api/rooms`, `/ice-config`, WebSocket `/signal`, service logs, and a real two-client audio call. A rollback that only restores code does not restore active calls.

## Security and limitations

- Room participants are guests, not authenticated user accounts. Private-room access uses a shared password, and host privileges use a bearer capability stored in browser local storage; use a separate trusted account/identity layer before relying on real-world identities.
- Live signaling state is in memory and requires one Uvicorn worker. Multi-worker or multi-node deployment requires a shared signaling/session store.
- The app does not yet provide enterprise identity, audit logs, persistent bans, or an SFU media topology. Do not treat it as a regulated or high-assurance conferencing service.
- Use a maintained Ubuntu release, keep the VPS updated, and restrict SSH access.
- The installer restricts `/etc/roomtone/turn.env` to `root:roomtone` mode `0640` and `/etc/turnserver.conf` to root mode `0600` because the latter contains the coturn shared secret. Verify these permissions after manual configuration changes.
