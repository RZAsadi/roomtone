r"""A small room-based voice chat: FastAPI signaling + embedded WebRTC client.

Run with: venv/Scripts/python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Iterator
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice-room")

# Durable room metadata lives in SQLite; live sockets and participant state are ephemeral.
DB_PATH = os.getenv("ROOMTONE_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "roomtone.db"))
rooms: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
rooms_lock = asyncio.Lock()
rate_windows: dict[str, list[float]] = {}


def rate_allowed(key: str, limit: int, seconds: int) -> bool:
    now = time.monotonic()
    # Keep attacker-controlled keys from growing this process-local limiter forever.
    if key not in rate_windows and len(rate_windows) >= 10_000:
        expired = [k for k, stamps in rate_windows.items() if not stamps or now - stamps[-1] >= 3600]
        for expired_key in expired:
            rate_windows.pop(expired_key, None)
        if len(rate_windows) >= 10_000:
            # Fail closed for unseen keys instead of evicting an active counter.
            return False
    recent = [stamp for stamp in rate_windows.get(key, []) if now - stamp < seconds]
    if len(recent) >= limit:
        rate_windows[key] = recent
        return False
    recent.append(now)
    rate_windows[key] = recent
    return True


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    # Only trust forwarded identity when the immediate peer is a local reverse proxy.
    try:
        is_local_proxy = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_local_proxy = False
    forwarded = request.headers.get("x-forwarded-for", "") if is_local_proxy else ""
    return forwarded.split(",")[-1].strip() if forwarded else peer


def websocket_client_ip(socket: WebSocket) -> str:
    peer = socket.client.host if socket.client else "unknown"
    try:
        is_local_proxy = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_local_proxy = False
    forwarded = socket.headers.get("x-forwarded-for", "") if is_local_proxy else ""
    return forwarded.split(",")[-1].strip() if forwarded else peer


def clean_text(value: Any, limit: int, fallback: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    return value[:limit] or fallback


async def send_json(socket: WebSocket, payload: dict[str, Any]) -> bool:
    try:
        await socket.send_json(payload)
        return True
    except (RuntimeError, WebSocketDisconnect):
        return False


async def broadcast(room: str, payload: dict[str, Any], exclude: str | None = None) -> None:
    async with rooms_lock:
        targets = [peer["socket"] for peer_id, peer in rooms.get(room, {}).items() if peer_id != exclude]
    if targets:
        await asyncio.gather(*(send_json(target, payload) for target in targets), return_exceptions=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield
    async with rooms_lock:
        sockets = [peer["socket"] for peers in rooms.values() for peer in peers.values()]
        rooms.clear()
    for socket in sockets:
        try:
            await socket.close(code=1001)
        except Exception:
            pass


app = FastAPI(title="RoomTone Voice Chat", lifespan=lifespan)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS room_metadata (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            is_private INTEGER NOT NULL DEFAULT 0, password_salt TEXT, password_hash TEXT,
            host_token_hash TEXT NOT NULL, capacity INTEGER NOT NULL DEFAULT 12,
            locked INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)""")


def room_record(room_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM room_metadata WHERE id = ?", (room_id,)).fetchone()
    return dict(row) if row else None


def public_room(room: dict[str, Any]) -> dict[str, Any]:
    return {"id": room["id"], "name": room["name"], "description": room["description"],
            "private": bool(room["is_private"]), "locked": bool(room["locked"]),
            "capacity": room["capacity"], "participants": len(rooms.get(room["id"], {}))}


@app.get("/api/rooms")
async def list_rooms() -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM room_metadata WHERE is_private = 0 ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"rooms": [public_room(dict(row)) for row in rows]}


async def create_room_data(payload: dict[str, Any], request: Request | None = None) -> dict[str, Any]:
    if request is not None and not rate_allowed("create:" + client_ip(request), 5, 3600):
        raise HTTPException(429, "Room creation limit reached. Try again later.")
    if not isinstance(payload.get("name"), str):
        raise HTTPException(400, "Room name must be text.")
    if "description" in payload and not isinstance(payload["description"], str):
        raise HTTPException(400, "Room description must be text.")
    if "private" in payload and not isinstance(payload["private"], bool):
        raise HTTPException(400, "The private field must be true or false.")
    if "password" in payload and not isinstance(payload["password"], str):
        raise HTTPException(400, "Room password must be text.")
    name = clean_text(payload.get("name"), 48, "")
    description = clean_text(payload.get("description"), 180, "")
    password = str(payload.get("password") or "")
    private = bool(payload.get("private"))
    if len(password) > 256:
        raise HTTPException(400, "Room password must be 256 characters or fewer.")
    raw_capacity = payload.get("capacity", 12)
    if isinstance(raw_capacity, bool) or not isinstance(raw_capacity, (int, str)):
        raise HTTPException(400, "Capacity must be an integer between 2 and 12.")
    if isinstance(raw_capacity, str) and not re.fullmatch(r"\d+", raw_capacity.strip()):
        raise HTTPException(400, "Capacity must be an integer between 2 and 12.")
    try:
        capacity = int(raw_capacity)
    except (TypeError, ValueError):
        raise HTTPException(400, "Capacity must be an integer between 2 and 12.") from None
    if not name or len(name) < 2:
        raise HTTPException(400, "Room name must contain at least 2 characters.")
    if not 2 <= capacity <= 12:
        raise HTTPException(400, "Capacity must be between 2 and 12.")
    if private and len(password) < 6:
        raise HTTPException(400, "Private rooms require a password of at least 6 characters.")
    room_id = uuid4().hex[:12]
    host_token = secrets.token_urlsafe(32)
    salt = secrets.token_hex(16) if private else None
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 180000).hex() if private else None
    with db() as conn:
        conn.execute("INSERT INTO room_metadata(id,name,description,is_private,password_salt,password_hash,host_token_hash,capacity,locked,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (room_id, name, description, int(private), salt, password_hash,
                      hashlib.sha256(host_token.encode()).hexdigest(), capacity, 0, int(time.time())))
    return {"room": {"id": room_id, "name": name, "description": description, "private": private,
                     "locked": False, "capacity": capacity, "participants": 0}, "hostToken": host_token}


@app.post("/api/rooms")
async def create_room(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    return await create_room_data(payload, request)


@app.get("/ice-config")
async def ice_config(request: Request) -> dict[str, Any]:
    """Return short-lived TURN credentials when coturn is configured."""
    if not rate_allowed("ice:" + client_ip(request), 60, 60):
        raise HTTPException(429, "Too many ICE configuration requests. Try again shortly.")
    domain = os.getenv("ROOMTONE_TURN_DOMAIN", "").strip()
    secret = os.getenv("ROOMTONE_TURN_SECRET", "").strip()
    servers: list[dict[str, Any]] = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun.cloudflare.com:3478"},
    ]
    if domain and secret:
        expires = int(time.time()) + 3600
        username = f"{expires}:roomtone"
        digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
        credential = base64.b64encode(digest).decode()
        servers.append({
            "urls": [
                f"turn:{domain}:3478?transport=udp",
                f"turn:{domain}:3478?transport=tcp",
            ],
            "username": username,
            "credential": credential,
        })
    return {"iceServers": servers}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.websocket("/signal")
async def signaling(socket: WebSocket) -> None:
    origin = socket.headers.get("origin")
    request_host = socket.headers.get("host", "").lower()
    if not origin:
        await socket.close(code=1008)
        return
    try:
        parsed_origin = urlsplit(origin)
        if (parsed_origin.scheme not in {"http", "https"} or not parsed_origin.netloc or
                parsed_origin.netloc.lower() != request_host or parsed_origin.username or
                parsed_origin.password or parsed_origin.path or parsed_origin.query or parsed_origin.fragment):
            await socket.close(code=1008)
            return
    except ValueError:
        await socket.close(code=1008)
        return
    await socket.accept()
    peer_id = uuid4().hex[:12]
    room = ""
    name = "Guest"
    joined = False
    role = "listener"
    is_host = False
    window_started = time.monotonic()
    window_messages = 0
    try:
        while True:
            message = await socket.receive_json()
            if not isinstance(message, dict) or len(json.dumps(message, separators=(",", ":"))) > 256_000:
                await send_json(socket, {"type": "error", "message": "Invalid or oversized signaling message."})
                await socket.close(code=1009)
                return
            now = time.monotonic()
            if now - window_started >= 10:
                window_started, window_messages = now, 0
            window_messages += 1
            if window_messages > 200:
                await socket.close(code=1008)
                return
            kind = message.get("type")

            if kind == "join" and not joined:
                room = str(message.get("room", ""))
                if not re.fullmatch(r"[a-fA-F0-9]{12}", room):
                    await send_json(socket, {"type": "error", "message": "Invalid room ID."})
                    await socket.close(code=1008)
                    return
                if not isinstance(message.get("name", "Guest"), str):
                    await send_json(socket, {"type": "error", "message": "Participant name must be text."})
                    await socket.close(code=1008)
                    return
                name = clean_text(message.get("name"), 24, "Guest")
                metadata = room_record(room)
                if not metadata:
                    await send_json(socket, {"type": "error", "message": "That room does not exist. Refresh room discovery and try again."})
                    await socket.close(code=1008)
                    return
                room_record_snapshot = dict(metadata)
                raw_token = message.get("hostToken", "")
                if not isinstance(raw_token, str):
                    await send_json(socket, {"type": "error", "message": "Invalid host token."})
                    await socket.close(code=1008)
                    return
                supplied_token = raw_token
                if len(supplied_token) > 256:
                    await send_json(socket, {"type": "error", "message": "Invalid host token."})
                    await socket.close(code=1008)
                    return
                is_host = bool(supplied_token) and hmac.compare_digest(
                    hashlib.sha256(supplied_token.encode()).hexdigest(), metadata["host_token_hash"])
                if metadata["is_private"]:
                    raw_password = message.get("password", "")
                    if not isinstance(raw_password, str):
                        await send_json(socket, {"type": "error", "message": "Room password must be text."})
                        await socket.close(code=1008)
                        return
                    supplied_password = raw_password
                    source_ip = websocket_client_ip(socket)
                    if len(supplied_password) > 256 or not rate_allowed(f"join:{source_ip}:{room}", 10, 60):
                        await send_json(socket, {"type": "error", "message": "Too many or invalid room join attempts. Try again shortly."})
                        await socket.close(code=1008)
                        return
                    if not is_host:
                        candidate_hash = hashlib.pbkdf2_hmac("sha256", supplied_password.encode(), bytes.fromhex(metadata["password_salt"]), 180000).hex()
                        if not hmac.compare_digest(candidate_hash, metadata["password_hash"]):
                            await send_json(socket, {"type": "error", "message": "Incorrect room password."})
                            await socket.close(code=1008)
                            return
                async with rooms_lock:
                    # Re-read mutable authorization and capacity data while holding the
                    # same lock used by joins, locking, capacity changes, and cleanup.
                    metadata = room_record(room)
                    if not metadata:
                        await send_json(socket, {"type": "error", "message": "That room no longer exists."})
                        await socket.close(code=1008)
                        return
                    is_host = bool(supplied_token) and hmac.compare_digest(
                        hashlib.sha256(supplied_token.encode()).hexdigest(), metadata["host_token_hash"])
                    if metadata["is_private"] and not is_host:
                        # The password was verified above. Refuse if credentials changed
                        # between verification and lock acquisition instead of doing a
                        # second expensive PBKDF2 operation while holding the room lock.
                        if (metadata["password_salt"] != room_record_snapshot["password_salt"] or
                                metadata["password_hash"] != room_record_snapshot["password_hash"]):
                            await send_json(socket, {"type": "error", "message": "Incorrect room password."})
                            await socket.close(code=1008)
                            return
                    peers = rooms.get(room, {})
                    if metadata["locked"] and not is_host:
                        await send_json(socket, {"type": "error", "message": "This room is locked by its host."})
                        await socket.close(code=1008)
                        return
                    if len(peers) >= metadata["capacity"]:
                        await send_json(socket, {"type": "error", "message": f"This room is full ({metadata['capacity']} people maximum)."})
                        await socket.close(code=1008)
                        return
                    if is_host:
                        role = "host"
                    else:
                        role = "speaker"
                    existing = [{"id": pid, "name": info["name"], "role": info["role"],
                                 "muted": info["muted"], "handRaised": info["handRaised"]}
                                for pid, info in peers.items()]
                    peers[peer_id] = {"socket": socket, "name": name, "role": role,
                                      "muted": False, "handRaised": False}
                    rooms[room] = peers
                    joined = True
                await send_json(socket, {"type": "joined", "id": peer_id, "room": room, "roomInfo": public_room(metadata),
                                         "role": role, "isHost": is_host, "peers": existing, "limit": metadata["capacity"]})
                await broadcast(room, {"type": "peer-joined", "id": peer_id, "name": name, "role": role,
                                       "muted": False, "handRaised": False}, exclude=peer_id)
                log.info("%s joined room %s (%d peers)", name, room, len(existing) + 1)
                continue

            if not joined:
                await send_json(socket, {"type": "error", "message": "Join a room before sending messages."})
                continue

            if kind in {"offer", "answer", "ice"}:
                target = str(message.get("to", ""))
                async with rooms_lock:
                    sender = rooms.get(room, {}).get(peer_id)
                    recipient = rooms.get(room, {}).get(target)
                    target_socket = recipient["socket"] if recipient else None
                    sender_role = sender["role"] if sender else "listener"
                if kind in {"offer", "answer"} and sender_role == "listener":
                    description = message.get("sdp")
                    sdp_text = description.get("sdp", "") if isinstance(description, dict) else ""
                    if (not isinstance(sdp_text, str) or len(sdp_text) > 128_000 or
                            "a=sendrecv" in sdp_text or "a=sendonly" in sdp_text or
                            ("a=recvonly" not in sdp_text and "a=inactive" not in sdp_text)):
                        await send_json(socket, {"type": "error", "message": "Listener signaling cannot enable microphone transmission. Ask the host for speaker access."})
                        continue
                if target_socket:
                    payload = {"type": kind, "from": peer_id, "name": name}
                    if kind in {"offer", "answer"}:
                        payload["sdp"] = message.get("sdp")
                    else:
                        payload["candidate"] = message.get("candidate")
                    await send_json(target_socket, payload)
            elif kind == "mute":
                if not isinstance(message.get("muted"), bool):
                    await send_json(socket, {"type": "error", "message": "Mute state must be true or false."})
                    continue
                muted = bool(message.get("muted"))
                async with rooms_lock:
                    if peer_id in rooms.get(room, {}): rooms[room][peer_id]["muted"] = muted
                await broadcast(room, {"type": "mute", "id": peer_id, "muted": muted}, exclude=peer_id)
            elif kind == "raise-hand":
                if not isinstance(message.get("raised"), bool):
                    await send_json(socket, {"type": "error", "message": "Hand state must be true or false."})
                    continue
                raised = bool(message.get("raised"))
                async with rooms_lock:
                    if peer_id in rooms.get(room, {}): rooms[room][peer_id]["handRaised"] = raised
                await broadcast(room, {"type": "participant-state", "id": peer_id, "handRaised": raised})
            elif kind in {"set-role", "kick", "lock-room", "set-capacity", "set-mute"}:
                async with rooms_lock:
                    peers = rooms.get(room, {})
                    actor = peers.get(peer_id)
                    target_id = str(message.get("target", ""))
                    target = peers.get(target_id)
                    allowed = bool(actor and actor["role"] in {"host", "moderator"})
                    if not allowed:
                        await send_json(socket, {"type": "error", "message": "Only the host or a moderator can do that."})
                        continue
                    if kind == "set-role":
                        new_role = str(message.get("role", ""))
                        if actor["role"] != "host" or not target or target_id == peer_id or new_role not in {"speaker", "listener", "moderator"}:
                            await send_json(socket, {"type": "error", "message": "That role change is not permitted."})
                            continue
                        target["role"] = new_role
                        for member in peers.values():
                            await send_json(member["socket"], {"type": "participant-state", "id": target_id, "role": new_role})
                    elif kind == "kick":
                        if actor["role"] != "host" or not target or target_id == peer_id or target["role"] == "host":
                            await send_json(socket, {"type": "error", "message": "Only the host can remove another participant."})
                            continue
                        await send_json(target["socket"], {"type": "removed", "message": "You were removed from this room by its host."})
                        await target["socket"].close(code=1008)
                    elif kind == "set-mute":
                        if not target or target["role"] == "host":
                            await send_json(socket, {"type": "error", "message": "That participant cannot be remotely muted."})
                            continue
                        await send_json(target["socket"], {"type": "force-mute"})
                    elif kind == "lock-room":
                        if actor["role"] != "host":
                            await send_json(socket, {"type": "error", "message": "Only the host can lock the room."})
                            continue
                        if not isinstance(message.get("locked"), bool):
                            await send_json(socket, {"type": "error", "message": "Lock state must be true or false."})
                            continue
                        locked = message["locked"]
                        with db() as conn:
                            conn.execute("UPDATE room_metadata SET locked=? WHERE id=?", (int(locked), room))
                        for member in peers.values():
                            await send_json(member["socket"], {"type": "room-updated", "locked": locked})
                    elif kind == "set-capacity":
                        if actor["role"] != "host":
                            await send_json(socket, {"type": "error", "message": "Only the host can change room capacity."})
                            continue
                        raw_capacity = message.get("capacity")
                        if isinstance(raw_capacity, bool) or not isinstance(raw_capacity, int):
                            capacity = 0
                        else:
                            capacity = raw_capacity
                        if not 2 <= capacity <= 12 or capacity < len(peers):
                            await send_json(socket, {"type": "error", "message": "Capacity must be 2–12 and cannot be below the current participant count."})
                            continue
                        with db() as conn:
                            conn.execute("UPDATE room_metadata SET capacity=? WHERE id=?", (capacity, room))
                        for member in peers.values():
                            await send_json(member["socket"], {"type": "room-updated", "capacity": capacity})
            elif kind == "leave":
                break
    except WebSocketDisconnect:
        pass
    except (ValueError, RuntimeError) as exc:
        log.debug("Signaling connection ended: %s", exc)
    finally:
        if joined:
            successor_info = None
            async with rooms_lock:
                peers = rooms.get(room, {})
                was_host = bool(peers.get(peer_id) and peers[peer_id]["role"] == "host")
                peers.pop(peer_id, None)
                remaining = list(peers.items())
                if not peers:
                    rooms.pop(room, None)
                if was_host:
                    # Update durable authorization before allowing another join to proceed.
                    # This closes the race where a join could recreate a room as the host
                    # was deleting it, or use an obsolete host token during succession.
                    if remaining:
                        successor_id, successor = remaining[0]
                        successor["role"] = "host"
                        new_token = secrets.token_urlsafe(32)
                        with db() as conn:
                            conn.execute("UPDATE room_metadata SET host_token_hash=? WHERE id=?",
                                         (hashlib.sha256(new_token.encode()).hexdigest(), room))
                        successor_info = (successor["socket"], new_token, successor_id)
                    else:
                        with db() as conn:
                            conn.execute("DELETE FROM room_metadata WHERE id=?", (room,))
            if successor_info:
                successor_socket, new_token, successor_id = successor_info
                await send_json(successor_socket, {"type": "promoted-host", "hostToken": new_token})
                await broadcast(room, {"type": "participant-state", "id": successor_id, "role": "host"})
            await broadcast(room, {"type": "peer-left", "id": peer_id})
            log.info("%s left room %s", name, room)
        try:
            await socket.close()
        except Exception:
            pass


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#11131b"><title>RoomTone — Voice Rooms</title>
<style>
:root{color-scheme:dark;--bg:#10121a;--panel:#191c28;--panel2:#202434;--line:#30364a;--text:#f4f5fb;--muted:#9da5bc;--accent:#a99bff;--green:#6ee7b7;--red:#ff7185}
*{box-sizing:border-box}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:3px}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#35305b 0,transparent 52%),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;display:grid;place-items:center;padding:28px 16px}
.shell{width:min(100%,760px)}header{display:flex;align-items:center;gap:12px;margin-bottom:28px}.logo{width:44px;height:44px;border-radius:15px;background:linear-gradient(145deg,#b8aaff,#7767e8);display:grid;place-items:center;color:#171427;box-shadow:0 8px 30px #8b7cff35}.logo svg{width:25px}h1{font-size:21px;line-height:1.1;margin:0;letter-spacing:-.5px}header p{margin:5px 0 0;color:var(--muted);font-size:13px}.pill{margin-left:auto;border:1px solid var(--line);border-radius:999px;padding:6px 10px;color:var(--muted);font-size:12px;white-space:nowrap}.pill:before{content:"";display:inline-block;width:7px;height:7px;background:var(--green);border-radius:50%;margin-right:7px}
.panel{background:#191c28ed;border:1px solid #30364a;border-radius:24px;overflow:hidden;box-shadow:0 25px 90px #0003}.hero{padding:27px 28px 22px;border-bottom:1px solid var(--line)}.eyebrow{text-transform:uppercase;letter-spacing:1.7px;color:var(--accent);font-size:10px;font-weight:800}.hero h2{font-size:27px;letter-spacing:-.8px;margin:8px 0 5px}.hero p{margin:0;color:var(--muted);font-size:14px}.form{display:grid;grid-template-columns:1fr 1fr auto;gap:12px;padding:20px 28px 24px;align-items:end}.field label{display:block;font-size:12px;color:var(--muted);margin:0 0 7px}.field input{width:100%;background:#11141e;border:1px solid var(--line);border-radius:11px;padding:12px;color:var(--text);font:inherit;outline:none}.field input:focus{border-color:var(--accent);box-shadow:0 0 0 3px #a99bff20}.btn{border:0;border-radius:12px;padding:12px 17px;font:700 14px inherit;cursor:pointer;transition:transform .15s,opacity .15s;white-space:nowrap}.btn:hover{transform:translateY(-1px)}.btn:disabled{opacity:.5;cursor:wait}.primary{background:var(--accent);color:#171427}.danger{background:#482632;color:#ffb5c0;border:1px solid #754050}.subtle{background:var(--panel2);border:1px solid var(--line);color:var(--text)}
.room{display:none}.roomtop{display:flex;align-items:center;gap:12px;padding:20px 28px;border-bottom:1px solid var(--line)}.roommark{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;background:#a99bff18;color:var(--accent)}.roommark svg{width:22px}.roomtitle{font-weight:750}.roommeta{font-size:12px;color:var(--muted)}.roomtop .pill{margin-left:auto}.members{padding:24px 28px 28px}.sectionline{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.sectionline strong{font-size:13px}.sectionline span{color:var(--muted);font-size:12px}.people{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:12px}.person{position:relative;min-height:132px;border:1px solid var(--line);border-radius:16px;background:#141722;padding:17px 12px 13px;text-align:center;overflow:hidden}.person.speaking{border-color:#6ee7b7aa;box-shadow:inset 0 0 0 1px #6ee7b720}.avatar{width:53px;height:53px;margin:0 auto 9px;border-radius:18px;display:grid;place-items:center;font-size:20px;font-weight:750;background:linear-gradient(145deg,#39344f,#292b40);color:#dcd5ff}.person.me .avatar{background:linear-gradient(145deg,#b8aaff,#7669dc);color:#171427}.personname{font-size:13px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.personstatus{font-size:11px;color:var(--muted);margin-top:3px}.personstatus.muted{color:var(--red)}.you{font-size:10px;color:var(--accent);margin-left:4px}.controls{display:flex;justify-content:center;gap:10px;padding:18px 20px 23px;border-top:1px solid var(--line);background:#151823;flex-wrap:wrap}.controls .btn{min-width:115px}.status{min-height:23px;text-align:center;padding:0 20px 16px;color:var(--muted);font-size:12px}.status.error{color:#ff9dac}.foot{text-align:center;color:#737c95;font-size:11px;margin:18px 0 0}.hidden{display:none!important}.field select{width:100%;background:#11141e;border:1px solid var(--line);border-radius:11px;padding:12px;color:var(--text);font:inherit}.roomlist{display:grid;gap:10px}.roomrow{display:flex;align-items:center;gap:12px;border:1px solid var(--line);border-radius:14px;background:#141722;padding:12px 14px}.roomrowtext{min-width:0;flex:1}.roomrowtext strong{font-size:13px}.roomrow .roommeta{overflow-wrap:anywhere}.personactions{display:flex;flex-wrap:wrap;gap:4px;justify-content:center;margin-top:9px}.personactions .btn{font-size:11px;padding:5px 7px;min-width:0}
@media(max-width:600px){body{padding:18px 12px}.hero{padding:23px 20px 18px}.hero h2{font-size:23px}.form{grid-template-columns:1fr 1fr;padding:17px 20px 22px}.form .btn{grid-column:1/-1}.roomtop{padding:17px 20px}.members{padding:20px}.people{grid-template-columns:repeat(2,minmax(0,1fr))}.controls{gap:8px;padding-inline:12px}.controls .btn{min-width:0;flex:1;padding:12px 8px}.pill{font-size:11px}}
</style></head>
<body><main class="shell">
<header><div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3m-4 0h8"/></svg></div><div><h1>RoomTone</h1><p>Good conversations start here.</p></div><div class="pill" id="connectionPill">Ready to connect</div></header>
<section class="panel"><div id="joinView"><div class="hero"><div class="eyebrow">Your people, your room</div><h2>Find your conversation.</h2><p>Create a room for your group or join a public room. Private rooms require a password.</p></div><div class="form" style="grid-template-columns:1fr 1fr"><div class="field"><label for="displayName">Your name</label><input id="displayName" maxlength="24" placeholder="e.g. Alex" autocomplete="nickname" required></div><button class="btn primary" id="createToggle" type="button">＋ Create room</button></div><form class="form hidden" id="createForm" style="grid-template-columns:1fr 1fr"><div class="field"><label for="newRoomName">Room name</label><input id="newRoomName" maxlength="48" placeholder="Friday hangout" required></div><div class="field"><label for="newRoomDescription">Description (optional)</label><input id="newRoomDescription" maxlength="180" placeholder="What are we talking about?"></div><div class="field"><label for="newRoomCapacity">Participant limit</label><select id="newRoomCapacity"><option>2</option><option>4</option><option>6</option><option>8</option><option selected>12</option></select></div><div class="field"><label for="newRoomPrivate">Room privacy</label><select id="newRoomPrivate"><option value="public">Public — discoverable</option><option value="private">Private — password required</option></select></div><div class="field hidden" id="newPasswordWrap"><label for="newRoomPassword">Room password (6+ characters)</label><input id="newRoomPassword" type="password" minlength="6" autocomplete="new-password"></div><button class="btn primary" type="submit">Create and join ↗</button></form><div class="members"><div class="sectionline"><strong>Discover public rooms</strong><button class="btn subtle" id="refreshRooms" type="button">↻ Refresh</button></div><div id="roomList" class="roomlist"><p class="roommeta">Loading rooms…</p></div></div></div>
<div id="roomView" class="room"><div class="roomtop"><div class="roommark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v18m-5-13v8m10-8v8M3 9v6m18-6v6"/></svg></div><div><div class="roomtitle" id="roomTitle">Room</div><div class="roommeta" id="roomMeta">Just you for now</div></div><div class="pill" id="roomPrivacy">Live room</div></div><div class="members"><div class="sectionline"><strong>In the conversation</strong><span id="memberCount">1 / 12</span></div><div class="people" id="people"></div></div><div class="controls"><label for="micSelect" class="roommeta">Microphone</label><select id="micSelect" aria-label="Select microphone" style="max-width:240px"><option value="">Default microphone</option></select><button class="btn subtle" id="muteBtn" type="button">🎙 Mute</button><button class="btn subtle" id="handBtn" type="button">✋ Raise hand</button><button class="btn subtle" id="inviteBtn" type="button">Copy invite</button><button class="btn subtle" id="audioBtn" type="button">🔊 Audio on</button><button class="btn danger" id="leaveBtn" type="button">Leave room</button></div><div class="controls hidden" id="hostControls"><button class="btn subtle" id="lockBtn" type="button">🔓 Lock room</button><button class="btn subtle" id="capacityBtn" type="button">Change capacity</button></div></div><div class="status" id="status" role="status" aria-live="polite"></div></section>
<p class="foot">Audio is peer-to-peer · Allow microphone access to join the conversation</p></main>
<script>
const $=id=>document.getElementById(id);const state={ws:null,id:null,room:'',roomInfo:null,name:'',stream:null,muted:false,audioEnabled:true,handRaised:false,role:'listener',hostToken:'',peers:new Map(),connecting:false,lastError:'',messageQueue:Promise.resolve()};
const PeerConnection=window.RTCPeerConnection||window.webkitRTCPeerConnection;const MediaStreamCtor=window.MediaStream;let rtcConfig={iceServers:[{urls:'stun:stun.l.google.com:19302'},{urls:'stun:stun.cloudflare.com:3478'}]};
function say(text,error=false){$('status').textContent=text;$('status').classList.toggle('error',error)}function send(data){if(state.ws&&state.ws.readyState===WebSocket.OPEN)state.ws.send(JSON.stringify(data))}function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function initials(name){return name.trim().split(/\s+/).slice(0,2).map(s=>s[0]||'').join('').toUpperCase()||'?'}function setConnection(text,online=false){$('connectionPill').textContent=text;$('connectionPill').style.color=online?'var(--green)':'var(--muted)'}
function drawPeople(){const entries=[{id:state.id,name:state.name,muted:state.muted,handRaised:state.handRaised,role:state.role,me:true},...Array.from(state.peers.values())];$('people').innerHTML=entries.map(p=>{const controls=(state.role==='host'||state.role==='moderator')&&!p.me?`<div class="personactions"><button class="btn subtle" data-action="mute" data-id="${p.id}">Mute</button>${state.role==='host'?`<button class="btn subtle" data-action="role" data-id="${p.id}">Role</button><button class="btn danger" data-action="kick" data-id="${p.id}">Remove</button>`:''}</div>`:'';return `<div class="person ${p.me?'me':''}" id="person-${p.id}"><div class="avatar">${escapeHtml(initials(p.name))}</div><div class="personname">${escapeHtml(p.name)}${p.me?'<span class="you">YOU</span>':''}</div><div class="personstatus ${p.muted?'muted':''}">${p.muted?'Mic muted':p.handRaised?'✋ Hand raised':p.role==='listener'?'Listening':p.role==='host'?'Host':p.role==='moderator'?'Moderator':'Speaker'}</div>${controls}</div>`}).join('');$('memberCount').textContent=`${entries.length} / ${state.roomInfo?.capacity||12}`;$('roomMeta').textContent=`${entries.length} ${entries.length===1?'person':'people'} connected${state.roomInfo?.description?' · '+state.roomInfo.description:''}`;$('hostControls').classList.toggle('hidden',state.role!=='host');$('muteBtn').disabled=state.role==='listener';$('muteBtn').textContent=state.role==='listener'?'?? Listener':state.muted?'?? Unmute':'?? Mute';$('lockBtn').textContent=state.roomInfo?.locked?'🔒 Unlock room':'🔓 Lock room'}
async function refreshRooms(){const list=$('roomList');list.innerHTML='<p class="roommeta">Loading rooms…</p>';try{const r=await fetch('/api/rooms',{cache:'no-store'});if(!r.ok)throw Error('Room discovery is unavailable.');const data=await r.json();if(!data.rooms.length){list.innerHTML='<p class="roommeta">No public rooms yet. Create one to get started.</p>';return}list.innerHTML=data.rooms.map(r=>`<div class="roomrow"><div class="roomrowtext"><strong>${escapeHtml(r.name)}</strong><div class="roommeta">${escapeHtml(r.description||'Open conversation')} · ${r.participants}/${r.capacity} people${r.locked?' · Locked':''}</div></div><button class="btn primary" data-action="join-room" data-id="${r.id}" ${r.locked||r.participants>=r.capacity?'disabled':''}>${r.locked?'Locked':r.participants>=r.capacity?'Full':'Join'}</button></div>`).join('')}catch(e){list.innerHTML=`<p class="roommeta">${escapeHtml(e.message)} <button class="btn subtle" data-action="refresh">Retry</button></p>`}}
async function createRoom(e){e.preventDefault();const priv=$('newRoomPrivate').value==='private';const payload={name:$('newRoomName').value.trim(),description:$('newRoomDescription').value.trim(),capacity:Number($('newRoomCapacity').value),private:priv,password:$('newRoomPassword').value};try{const r=await fetch('/api/rooms',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok)throw Error(data.detail||'Could not create room.');state.hostToken=data.hostToken;localStorage.setItem('roomtone-host-'+data.room.id,state.hostToken);$('createForm').classList.add('hidden');if(priv)prompt('Save this private room invite link and share the password separately:',location.origin+'/?room='+data.room.id);await enterRoom(data.room.id,'',true)}catch(err){say(err.message,true)}}
async function refreshMicrophones(){if(!navigator.mediaDevices?.enumerateDevices)return;try{const devices=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='audioinput');const select=$('micSelect'),previous=select.value;select.innerHTML='<option value="">Default microphone</option>'+devices.map((d,i)=>`<option value="${escapeHtml(d.deviceId)}">${escapeHtml(d.label||'Microphone '+(i+1))}</option>`).join('');if(devices.some(d=>d.deviceId===previous))select.value=previous}catch(e){console.warn('Could not enumerate microphones',e)}}
async function changeMicrophone(){if(!state.stream)return;const deviceId=$('micSelect').value;try{const next=await navigator.mediaDevices.getUserMedia({audio:{...(deviceId?{deviceId:{exact:deviceId}}:{}),echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});const track=next.getAudioTracks()[0];if(!track)throw Error('Selected microphone did not provide an audio track.');track.enabled=!state.muted&&state.role!=='listener';for(const p of state.peers.values()){const sender=p.pc?.getSenders().find(s=>s.track?.kind==='audio');if(sender)await sender.replaceTrack(track)}const old=state.stream;state.stream=next;old.getTracks().forEach(t=>t.stop());say('Microphone changed.')}catch(e){say('Could not switch microphone: '+e.message,true);await refreshMicrophones()}}
async function enterRoom(id,password='',host=false){if(state.connecting)return;if(!window.isSecureContext||!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){say('Your browser requires HTTPS to use the microphone.',true);return}state.connecting=true;say('Requesting microphone access…');try{state.lastError='';state.room=id;state.hostToken=host?state.hostToken:(localStorage.getItem('roomtone-host-'+id)||'');try{const r=await fetch('/ice-config',{cache:'no-store'});if(r.ok){const c=await r.json();if(c.iceServers?.length)rtcConfig={iceServers:c.iceServers}}}catch(e){console.warn('Using public STUN fallback',e)}state.name=$('displayName').value.trim().slice(0,24)||'Guest';state.stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});state.muted=false;await refreshMicrophones();const scheme=location.protocol==='https:'?'wss:':'ws:';const socket=new WebSocket(`${scheme}//${location.host}/signal`);state.ws=socket;socket.onopen=()=>send({type:'join',room:state.room,name:state.name,password,hostToken:state.hostToken});socket.onmessage=e=>{state.messageQueue=state.messageQueue.then(()=>handleMessage(JSON.parse(e.data))).catch(err=>{console.error(err);say('Connection error: '+err.message,true)})};socket.onerror=()=>say('Could not connect to the server. Check the service and network.',true);socket.onclose=()=>{if(state.ws!==socket)return;const error=state.lastError;if(state.id)cleanup(false);else{state.stream?.getTracks().forEach(t=>t.stop());state.stream=null}setConnection('Disconnected');if(error)say(error,true);else say('Connection lost. Rejoin the room to continue.',true)}}catch(err){state.stream?.getTracks().forEach(t=>t.stop());state.stream=null;say(err.name==='NotAllowedError'?'Microphone permission was denied.':err.name==='NotFoundError'?'No microphone was found.':`Could not access microphone: ${err.message}`,true)}finally{state.connecting=false}}
async function joinRoomPrompt(id){const info=await fetch('/api/rooms').then(r=>r.json()).catch(()=>({rooms:[]}));const room=info.rooms.find(r=>r.id===id);let password='';if(room?.private){password=prompt('This is a private room. Enter its password:')||'';if(!password)return}await enterRoom(id,password,false)}
function createPeer(id,name,initiator,details={}){let peer=state.peers.get(id);if(peer){peer.name=name||peer.name;Object.assign(peer,details);drawPeople();return peer}if(typeof PeerConnection!=='function'||typeof MediaStreamCtor!=='function'){say('This browser does not support WebRTC. Update your browser.',true);return {id,name,pc:null,pendingCandidates:[]}}peer={id,name,role:details.role||'speaker',muted:!!details.muted,handRaised:!!details.handRaised,pc:new PeerConnection(rtcConfig),stream:new MediaStreamCtor(),audio:null,pendingCandidates:[],restarts:0,polite:state.id>id,makingOffer:false,ignoreOffer:false};state.peers.set(id,peer);for(const track of state.stream.getTracks()){const sender=peer.pc.addTrack(track,state.stream);const transceiver=peer.pc.getTransceivers().find(t=>t.sender===sender);if(transceiver&&state.role==='listener')transceiver.direction='recvonly'}peer.pc.ontrack=e=>{for(const track of e.streams[0]?.getTracks()||[e.track])if(!peer.stream.getTracks().some(t=>t.id===track.id))peer.stream.addTrack(track);if(!peer.audio){peer.audio=new Audio();peer.audio.autoplay=true;peer.audio.playsInline=true;peer.audio.srcObject=peer.stream;peer.audio.muted=!state.audioEnabled;peer.audio.play().catch(()=>say('Tap “Audio on” to allow playback.'))}};peer.pc.onicecandidate=e=>{if(e.candidate)send({type:'ice',to:id,candidate:e.candidate})};peer.pc.oniceconnectionstatechange=()=>{const s=peer.pc.iceConnectionState;if(s==='connected'||s==='completed'){peer.restarts=0;say('Voice connected with '+peer.name+'.')}else if((s==='failed'||s==='disconnected')&&peer.restarts<1){peer.restarts++;say('Recovering voice connection with '+peer.name+'…');try{peer.pc.restartIce();peer.makingOffer=true;peer.pc.createOffer({iceRestart:true}).then(o=>peer.pc.setLocalDescription(o)).then(()=>send({type:'offer',to:id,sdp:peer.pc.localDescription})).catch(e=>say('Could not recover voice connection: '+e.message,true)).finally(()=>{peer.makingOffer=false})}catch(e){say('Voice connection failed. Check TURN/network settings.',true)}}else if(s==='checking')say('Connecting voice with '+peer.name+'…')};peer.pc.onconnectionstatechange=()=>{if(peer.pc.connectionState==='failed')say('Peer connection failed with '+peer.name+'.',true)};if(initiator){peer.makingOffer=true;peer.pc.createOffer().then(o=>peer.pc.setLocalDescription(o)).then(()=>send({type:'offer',to:id,sdp:peer.pc.localDescription})).catch(err=>say('Could not start voice: '+err.message,true)).finally(()=>{peer.makingOffer=false})}drawPeople();return peer}
function dropPeer(id){const p=state.peers.get(id);if(!p)return;p.pc?.close();if(p.audio){p.audio.srcObject=null;p.audio.remove()}state.peers.delete(id);drawPeople()}async function flushCandidates(p){for(const c of p.pendingCandidates.splice(0)){try{await p.pc.addIceCandidate(c)}catch(e){console.warn(e)}}}
async function handleMessage(m){switch(m.type){case'joined':state.id=m.id;state.roomInfo=m.roomInfo;state.role=m.role;state.room=m.room;$('joinView').classList.add('hidden');$('roomView').style.display='block';$('roomTitle').textContent=m.roomInfo.name;$('roomPrivacy').textContent=m.roomInfo.private?'Private room':'Public room';setConnection('In the room',true);for(const p of m.peers)createPeer(p.id,p.name,true,p);drawPeople();refreshRooms();say('Microphone ready. Say hello!');break;case'peer-joined':createPeer(m.id,m.name,false,m);say(`${m.name} joined the room.`);refreshRooms();break;case'offer':{const p=createPeer(m.from,m.name,false);if(!p.pc)return;const collision=p.makingOffer||p.pc.signalingState!=='stable';if(collision&&!p.polite){p.ignoreOffer=true;break}p.ignoreOffer=false;if(collision)await p.pc.setLocalDescription({type:'rollback'});await p.pc.setRemoteDescription(m.sdp);await flushCandidates(p);const a=await p.pc.createAnswer();await p.pc.setLocalDescription(a);send({type:'answer',to:m.from,sdp:p.pc.localDescription});break}case'answer':{const p=state.peers.get(m.from);if(p?.pc){await p.pc.setRemoteDescription(m.sdp);await flushCandidates(p)}break}case'ice':{const p=state.peers.get(m.from);if(p?.pc&&m.candidate&&!p.ignoreOffer){if(p.pc.remoteDescription?.type)p.pc.addIceCandidate(m.candidate).catch(console.warn);else p.pendingCandidates.push(m.candidate)}break}case'mute':{const p=state.peers.get(m.id);if(p){p.muted=m.muted;drawPeople()}break}case'participant-state':{if(m.id===state.id){if(m.role){state.role=m.role;state.stream?.getAudioTracks().forEach(t=>t.enabled=!state.muted&&state.role!=='listener');if(m.role==='host'&&m.hostToken){state.hostToken=m.hostToken;localStorage.setItem('roomtone-host-'+state.room,state.hostToken)}for(const p of state.peers.values()){const trans=p.pc?.getTransceivers().find(t=>t.sender.track?.kind==='audio');if(trans){trans.direction=state.role==='listener'?'recvonly':'sendrecv';if(p.pc.signalingState==='stable')p.pc.createOffer().then(o=>p.pc.setLocalDescription(o)).then(()=>send({type:'offer',to:p.id,sdp:p.pc.localDescription})).catch(console.warn)}}}if(m.handRaised!==undefined)state.handRaised=m.handRaised}else{const p=state.peers.get(m.id);if(p){if(m.role)p.role=m.role;if(m.handRaised!==undefined)p.handRaised=m.handRaised}}drawPeople();break}case'room-updated':if(m.locked!==undefined)state.roomInfo.locked=m.locked;if(m.capacity!==undefined)state.roomInfo.capacity=m.capacity;drawPeople();await refreshRooms();break;case'force-mute':if(state.stream){state.muted=true;state.stream.getAudioTracks().forEach(t=>t.enabled=false);$('muteBtn').textContent='🎙 Unmute';send({type:'mute',muted:true});drawPeople();say('A moderator muted your microphone.')}break;case'promoted-host':state.role='host';state.hostToken=m.hostToken;localStorage.setItem('roomtone-host-'+state.room,state.hostToken);drawPeople();say('You are now the room host.');break;case'removed':state.lastError=m.message;cleanup(false);say(m.message,true);break;case'peer-left':{const p=state.peers.get(m.id);dropPeer(m.id);if(p)say(`${p.name} left the room.`);refreshRooms();break}case'error':state.lastError=m.message;say(m.message,true);break}}
function toggleMute(){if(!state.stream||state.role==='listener'){say('You are a listener. Raise your hand and ask the host for speaker access.');return}state.muted=!state.muted;state.stream.getAudioTracks().forEach(t=>t.enabled=!state.muted&&state.role!=='listener');$('muteBtn').textContent=state.muted?'🎙 Unmute':'🎙 Mute';send({type:'mute',muted:state.muted});drawPeople();say(state.muted?'Your microphone is muted.':'Your microphone is live.')}function toggleAudio(){state.audioEnabled=!state.audioEnabled;for(const p of state.peers.values())if(p.audio){p.audio.muted=!state.audioEnabled;if(state.audioEnabled)p.audio.play().catch(()=>say('Your browser blocked playback. Check site sound settings and tap Audio on again.',true))}$('audioBtn').textContent=state.audioEnabled?'🔊 Audio on':'🔇 Audio off';say(state.audioEnabled?'Incoming audio enabled.':'Incoming audio muted for you.')}
function cleanup(notify=true){if(notify)send({type:'leave'});if(state.ws){const w=state.ws;state.ws=null;if(w.readyState===WebSocket.OPEN)w.close()}for(const id of [...state.peers.keys()])dropPeer(id);state.stream?.getTracks().forEach(t=>t.stop());state.stream=null;state.id=null;state.room='';state.roomInfo=null;state.muted=false;state.handRaised=false;state.role='listener';$('roomView').style.display='none';$('joinView').classList.remove('hidden');$('muteBtn').textContent='🎙 Mute';$('handBtn').textContent='✋ Raise hand';setConnection('Ready to connect');say('You left the room.');refreshRooms()}
$('createToggle').addEventListener('click',()=>$('createForm').classList.toggle('hidden'));$('newRoomPrivate').addEventListener('change',()=>{$('newPasswordWrap').classList.toggle('hidden',$('newRoomPrivate').value!=='private');$('newRoomPassword').required=$('newRoomPrivate').value==='private'});$('createForm').addEventListener('submit',createRoom);$('refreshRooms').addEventListener('click',refreshRooms);$('micSelect').addEventListener('change',changeMicrophone);navigator.mediaDevices?.addEventListener?.('devicechange',refreshMicrophones);$('muteBtn').addEventListener('click',toggleMute);$('audioBtn').addEventListener('click',toggleAudio);$('inviteBtn').addEventListener('click',async()=>{const url=location.origin+'/?room='+state.room;try{await navigator.clipboard.writeText(url);say('Room invite link copied.')}catch(e){prompt('Copy this room invite link:',url)}});$('handBtn').addEventListener('click',()=>{state.handRaised=!state.handRaised;$('handBtn').textContent=state.handRaised?'✋ Lower hand':'✋ Raise hand';send({type:'raise-hand',raised:state.handRaised});drawPeople()});$('leaveBtn').addEventListener('click',()=>cleanup(true));$('lockBtn').addEventListener('click',()=>send({type:'lock-room',locked:!state.roomInfo?.locked}));$('capacityBtn').addEventListener('click',()=>{const n=prompt('Maximum participants (2–12):',String(state.roomInfo?.capacity||12));if(n!==null)send({type:'set-capacity',capacity:Number(n)})});document.addEventListener('click',e=>{const b=e.target.closest('[data-action]');if(!b)return;const a=b.dataset.action,id=b.dataset.id;if(a==='refresh')refreshRooms();if(a==='join-room')joinRoomPrompt(id);if(a==='kick'&&confirm('Remove this participant from the room?'))send({type:'kick',target:id});if(a==='mute')send({type:'set-mute',target:id});if(a==='role'){const role=prompt('Set role: speaker, listener, or moderator','speaker');if(['speaker','listener','moderator'].includes(role))send({type:'set-role',target:id,role})}});window.addEventListener('beforeunload',()=>{send({type:'leave'});state.stream?.getTracks().forEach(t=>t.stop())});refreshRooms();const inviteRoom=new URLSearchParams(location.search).get('room');if(inviteRoom&&/^[a-f0-9]{12}$/i.test(inviteRoom)){(async()=>{let isPublic=false;try{const r=await fetch('/api/rooms');const data=await r.json();isPublic=data.rooms.some(r=>r.id===inviteRoom)}catch(e){}let pass='';if(!isPublic&&!localStorage.getItem('roomtone-host-'+inviteRoom)){pass=prompt('Enter the private room password:')||'';if(!pass)return}enterRoom(inviteRoom,pass,false)})()}
</script></body></html>'''
