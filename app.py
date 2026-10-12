r"""A small room-based voice chat: FastAPI signaling + embedded WebRTC client.

Run with: venv/Scripts/python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice-room")

# room -> peer id -> connected websocket and public display name
rooms: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
rooms_lock = asyncio.Lock()


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


@app.get("/ice-config")
async def ice_config() -> dict[str, Any]:
    """Return short-lived TURN credentials when coturn is configured."""
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
    await socket.accept()
    peer_id = uuid4().hex[:12]
    room = ""
    name = "Guest"
    joined = False
    try:
        while True:
            message = await socket.receive_json()
            kind = message.get("type")

            if kind == "join" and not joined:
                room = re.sub(r"[^a-zA-Z0-9_-]", "", str(message.get("room", "lobby")))[:32] or "lobby"
                name = clean_text(message.get("name"), 24, "Guest")
                async with rooms_lock:
                    peers = rooms[room]
                    if len(peers) >= 12:
                        await send_json(socket, {"type": "error", "message": "This room is full (12 people maximum)."})
                        await socket.close(code=1008)
                        return
                    existing = [{"id": pid, "name": info["name"]} for pid, info in peers.items()]
                    peers[peer_id] = {"socket": socket, "name": name}
                    joined = True
                await send_json(socket, {"type": "joined", "id": peer_id, "room": room, "peers": existing, "limit": 12})
                await broadcast(room, {"type": "peer-joined", "id": peer_id, "name": name}, exclude=peer_id)
                log.info("%s joined room %s (%d peers)", name, room, len(existing) + 1)
                continue

            if not joined:
                await send_json(socket, {"type": "error", "message": "Join a room before sending messages."})
                continue

            if kind in {"offer", "answer", "ice"}:
                target = str(message.get("to", ""))
                async with rooms_lock:
                    recipient = rooms.get(room, {}).get(target)
                    target_socket = recipient["socket"] if recipient else None
                if target_socket:
                    payload = {"type": kind, "from": peer_id, "name": name}
                    if kind in {"offer", "answer"}:
                        payload["sdp"] = message.get("sdp")
                    else:
                        payload["candidate"] = message.get("candidate")
                    await send_json(target_socket, payload)
            elif kind == "mute":
                await broadcast(room, {"type": "mute", "id": peer_id, "muted": bool(message.get("muted"))}, exclude=peer_id)
            elif kind == "leave":
                break
    except WebSocketDisconnect:
        pass
    except (ValueError, RuntimeError) as exc:
        log.debug("Signaling connection ended: %s", exc)
    finally:
        if joined:
            async with rooms_lock:
                peers = rooms.get(room, {})
                peers.pop(peer_id, None)
                if not peers:
                    rooms.pop(room, None)
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
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#35305b 0,transparent 52%),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;display:grid;place-items:center;padding:28px 16px}
.shell{width:min(100%,760px)}header{display:flex;align-items:center;gap:12px;margin-bottom:28px}.logo{width:44px;height:44px;border-radius:15px;background:linear-gradient(145deg,#b8aaff,#7767e8);display:grid;place-items:center;color:#171427;box-shadow:0 8px 30px #8b7cff35}.logo svg{width:25px}h1{font-size:21px;line-height:1.1;margin:0;letter-spacing:-.5px}header p{margin:5px 0 0;color:var(--muted);font-size:13px}.pill{margin-left:auto;border:1px solid var(--line);border-radius:999px;padding:6px 10px;color:var(--muted);font-size:12px;white-space:nowrap}.pill:before{content:"";display:inline-block;width:7px;height:7px;background:var(--green);border-radius:50%;margin-right:7px}
.panel{background:#191c28ed;border:1px solid #30364a;border-radius:24px;overflow:hidden;box-shadow:0 25px 90px #0003}.hero{padding:27px 28px 22px;border-bottom:1px solid var(--line)}.eyebrow{text-transform:uppercase;letter-spacing:1.7px;color:var(--accent);font-size:10px;font-weight:800}.hero h2{font-size:27px;letter-spacing:-.8px;margin:8px 0 5px}.hero p{margin:0;color:var(--muted);font-size:14px}.form{display:grid;grid-template-columns:1fr 1fr auto;gap:12px;padding:20px 28px 24px;align-items:end}.field label{display:block;font-size:12px;color:var(--muted);margin:0 0 7px}.field input{width:100%;background:#11141e;border:1px solid var(--line);border-radius:11px;padding:12px;color:var(--text);font:inherit;outline:none}.field input:focus{border-color:var(--accent);box-shadow:0 0 0 3px #a99bff20}.btn{border:0;border-radius:12px;padding:12px 17px;font:700 14px inherit;cursor:pointer;transition:transform .15s,opacity .15s;white-space:nowrap}.btn:hover{transform:translateY(-1px)}.btn:disabled{opacity:.5;cursor:wait}.primary{background:var(--accent);color:#171427}.danger{background:#482632;color:#ffb5c0;border:1px solid #754050}.subtle{background:var(--panel2);border:1px solid var(--line);color:var(--text)}
.room{display:none}.roomtop{display:flex;align-items:center;gap:12px;padding:20px 28px;border-bottom:1px solid var(--line)}.roommark{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;background:#a99bff18;color:var(--accent)}.roommark svg{width:22px}.roomtitle{font-weight:750}.roommeta{font-size:12px;color:var(--muted)}.roomtop .pill{margin-left:auto}.members{padding:24px 28px 28px}.sectionline{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.sectionline strong{font-size:13px}.sectionline span{color:var(--muted);font-size:12px}.people{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:12px}.person{position:relative;min-height:132px;border:1px solid var(--line);border-radius:16px;background:#141722;padding:17px 12px 13px;text-align:center;overflow:hidden}.person.speaking{border-color:#6ee7b7aa;box-shadow:inset 0 0 0 1px #6ee7b720}.avatar{width:53px;height:53px;margin:0 auto 9px;border-radius:18px;display:grid;place-items:center;font-size:20px;font-weight:750;background:linear-gradient(145deg,#39344f,#292b40);color:#dcd5ff}.person.me .avatar{background:linear-gradient(145deg,#b8aaff,#7669dc);color:#171427}.personname{font-size:13px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.personstatus{font-size:11px;color:var(--muted);margin-top:3px}.personstatus.muted{color:var(--red)}.you{font-size:10px;color:var(--accent);margin-left:4px}.controls{display:flex;justify-content:center;gap:10px;padding:18px 20px 23px;border-top:1px solid var(--line);background:#151823}.controls .btn{min-width:115px}.status{min-height:23px;text-align:center;padding:0 20px 16px;color:var(--muted);font-size:12px}.status.error{color:#ff9dac}.foot{text-align:center;color:#737c95;font-size:11px;margin:18px 0 0}.hidden{display:none!important}
@media(max-width:600px){body{padding:18px 12px}.hero{padding:23px 20px 18px}.hero h2{font-size:23px}.form{grid-template-columns:1fr 1fr;padding:17px 20px 22px}.form .btn{grid-column:1/-1}.roomtop{padding:17px 20px}.members{padding:20px}.people{grid-template-columns:repeat(2,minmax(0,1fr))}.controls{gap:8px;padding-inline:12px}.controls .btn{min-width:0;flex:1;padding:12px 8px}.pill{font-size:11px}}
</style></head>
<body><main class="shell">
<header><div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3m-4 0h8"/></svg></div><div><h1>RoomTone</h1><p>Good conversations start here.</p></div><div class="pill" id="connectionPill">Ready to connect</div></header>
<section class="panel"><div id="joinView"><div class="hero"><div class="eyebrow">Your people, your room</div><h2>Pull up a seat.</h2><p>Drop into a voice room and talk like you're in the same place.</p></div><form class="form" id="joinForm"><div class="field"><label for="displayName">Your name</label><input id="displayName" maxlength="24" placeholder="e.g. Alex" autocomplete="nickname" required></div><div class="field"><label for="roomName">Room name</label><input id="roomName" maxlength="32" pattern="[A-Za-z0-9_-]+" placeholder="lobby" value="lobby" required></div><button class="btn primary" id="joinBtn" type="submit">Join room&nbsp; ↗</button></form></div>
<div id="roomView" class="room"><div class="roomtop"><div class="roommark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v18m-5-13v8m10-8v8M3 9v6m18-6v6"/></svg></div><div><div class="roomtitle" id="roomTitle"># lobby</div><div class="roommeta" id="roomMeta">Just you for now</div></div><div class="pill">Live room</div></div><div class="members"><div class="sectionline"><strong>In the conversation</strong><span id="memberCount">1 / 12</span></div><div class="people" id="people"></div></div><div class="controls"><button class="btn subtle" id="muteBtn" type="button">🎙 Mute</button><button class="btn subtle" id="audioBtn" type="button">🔊 Audio on</button><button class="btn danger" id="leaveBtn" type="button">Leave room</button></div></div><div class="status" id="status" role="status" aria-live="polite"></div></section>
<p class="foot">Audio is peer-to-peer · Allow microphone access to join the conversation</p></main>
<script>
const $=id=>document.getElementById(id);const state={ws:null,id:null,room:'',name:'',stream:null,muted:false,audioEnabled:true,peers:new Map(),connecting:false};
const PeerConnection=window.RTCPeerConnection||window.webkitRTCPeerConnection;const MediaStreamCtor=window.MediaStream;let rtcConfig={iceServers:[{urls:'stun:stun.l.google.com:19302'},{urls:'stun:stun.cloudflare.com:3478'}]};
function say(text,error=false){$('status').textContent=text;$('status').classList.toggle('error',error)}
function send(data){if(state.ws&&state.ws.readyState===WebSocket.OPEN)state.ws.send(JSON.stringify(data))}
function initials(name){return name.trim().split(/\s+/).slice(0,2).map(s=>s[0]||'').join('').toUpperCase()||'?'}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function drawPeople(){const entries=[{id:state.id,name:state.name,muted:state.muted,me:true},...Array.from(state.peers.values())];$('people').innerHTML=entries.map(p=>`<div class="person ${p.me?'me':''} ${p.speaking?'speaking':''}" id="person-${p.id}"><div class="avatar">${escapeHtml(initials(p.name))}</div><div class="personname">${escapeHtml(p.name)}${p.me?'<span class="you">YOU</span>':''}</div><div class="personstatus ${p.muted?'muted':''}">${p.muted?'Mic muted':p.me?'Your microphone is live':'Connected'}</div></div>`).join('');$('memberCount').textContent=`${entries.length} / 12`;$('roomMeta').textContent=entries.length===1?'Just you for now':`${entries.length} people connected`}
function setConnection(text,online=false){$('connectionPill').textContent=text;$('connectionPill').style.setProperty('--unused','0');$('connectionPill').style.color=online?'var(--green)':'var(--muted)'}
function createPeer(id,name,initiator){let peer=state.peers.get(id);if(peer)return peer;if(typeof PeerConnection!=='function'||typeof MediaStreamCtor!=='function'){say('This browser does not support WebRTC voice calls. Open the room in the latest Chrome, Edge, or Safari browser.',true);return {id,name,pc:null,stream:null,audio:null,muted:false,pendingCandidates:[]}}peer={id,name,pc:new PeerConnection(rtcConfig),stream:new MediaStreamCtor(),audio:null,muted:false,pendingCandidates:[]};state.peers.set(id,peer);for(const track of state.stream.getTracks())peer.pc.addTrack(track,state.stream);peer.pc.ontrack=e=>{for(const track of e.streams[0]?.getTracks()||[e.track])if(!peer.stream.getTracks().some(t=>t.id===track.id))peer.stream.addTrack(track);if(!peer.audio){peer.audio=new Audio();peer.audio.autoplay=true;peer.audio.playsInline=true;peer.audio.srcObject=peer.stream;peer.audio.muted=!state.audioEnabled;peer.audio.play().catch(()=>say('Tap “Audio on” if your browser blocks playback.'))}say('Audio track received from '+peer.name+'.')};peer.pc.onicecandidate=e=>{if(e.candidate)send({type:'ice',to:id,candidate:e.candidate})};peer.pc.oniceconnectionstatechange=()=>{const s=peer.pc.iceConnectionState;if(s==='connected'||s==='completed')say('Audio connection established with '+peer.name+'.');else if(s==='failed')say('Could not establish audio with '+peer.name+'. Your network may require a TURN relay.',true);else if(s==='checking')say('Connecting audio with '+peer.name+'…')};peer.pc.onconnectionstatechange=()=>{if(peer.pc.connectionState==='failed')say('Peer connection failed with '+peer.name+'. Check network/TURN settings.',true)};if(initiator)peer.pc.createOffer().then(offer=>peer.pc.setLocalDescription(offer)).then(()=>send({type:'offer',to:id,sdp:peer.pc.localDescription})).catch(err=>say('Could not start audio connection: '+err.message,true));drawPeople();return peer}
function dropPeer(id){const p=state.peers.get(id);if(!p)return;p.pc.ontrack=null;p.pc.close();if(p.audio){p.audio.srcObject=null;p.audio.remove()}state.peers.delete(id);drawPeople()}
async function flushCandidates(p){for(const candidate of p.pendingCandidates.splice(0)){try{await p.pc.addIceCandidate(candidate)}catch(err){console.warn('Could not add queued ICE candidate',err)}}}
async function handleMessage(m){switch(m.type){case'joined':state.id=m.id;$('joinView').classList.add('hidden');$('roomView').style.display='block';$('roomTitle').textContent='# '+state.room;setConnection('In the room',true);for(const p of m.peers)createPeer(p.id,p.name,true);drawPeople();say('Microphone connected. Say hello!');break;case'peer-joined':createPeer(m.id,m.name,false);say(`${m.name} joined the room.`);break;case'offer':{const p=createPeer(m.from,m.name,false);if(!p.pc)return;await p.pc.setRemoteDescription(m.sdp);await flushCandidates(p);const answer=await p.pc.createAnswer();await p.pc.setLocalDescription(answer);send({type:'answer',to:m.from,sdp:p.pc.localDescription});break}case'answer':{const p=state.peers.get(m.from);if(p?.pc){await p.pc.setRemoteDescription(m.sdp);await flushCandidates(p)}break}case'ice':{const p=state.peers.get(m.from);if(p?.pc&&m.candidate){if(p.pc.remoteDescription&&p.pc.remoteDescription.type)p.pc.addIceCandidate(m.candidate).catch(err=>console.warn('Could not add ICE candidate',err));else p.pendingCandidates.push(m.candidate)}break}case'mute':{const p=state.peers.get(m.id);if(p){p.muted=m.muted;drawPeople()}break}case'peer-left':{const p=state.peers.get(m.id);dropPeer(m.id);if(p)say(`${p.name} left the room.`);break}case'error':say(m.message,true);break}}
async function join(e){e.preventDefault();if(state.connecting)return;if(!window.isSecureContext||!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){say('Your phone browser requires HTTPS to use the microphone. Open this room through a secure HTTPS link; a local Wi-Fi HTTP address cannot access the mic.',true);return}state.connecting=true;$('joinBtn').disabled=true;say('Requesting microphone access…');try{try{const configResponse=await fetch('/ice-config',{cache:'no-store'});if(configResponse.ok){const config=await configResponse.json();if(Array.isArray(config.iceServers)&&config.iceServers.length)rtcConfig={iceServers:config.iceServers}}}catch(configError){console.warn('Could not load ICE configuration; using public STUN only.',configError)}state.name=$('displayName').value.trim().slice(0,24)||'Guest';state.room=$('roomName').value.trim().replace(/[^a-zA-Z0-9_-]/g,'').slice(0,32)||'lobby';state.stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});state.muted=false;const scheme=location.protocol==='https:'?'wss:':'ws:';state.ws=new WebSocket(`${scheme}//${location.host}/signal`);state.ws.onopen=()=>send({type:'join',room:state.room,name:state.name});state.ws.onmessage=async e=>{try{await handleMessage(JSON.parse(e.data))}catch(err){console.error(err);say('A connection error occurred: '+err.message,true)}};state.ws.onerror=()=>say('Could not connect to the server. Check that it is running.',true);state.ws.onclose=()=>{if(state.id)cleanup(false);setConnection('Disconnected');};}catch(err){if(state.stream){state.stream.getTracks().forEach(t=>t.stop());state.stream=null}say(err.name==='NotAllowedError'?'Microphone permission was denied. Allow access and try again.':err.name==='NotFoundError'?'No microphone was found on this device.':`Could not access microphone: ${err.message}`,true)}finally{state.connecting=false;$('joinBtn').disabled=false}}
function toggleMute(){if(!state.stream)return;state.muted=!state.muted;state.stream.getAudioTracks().forEach(t=>t.enabled=!state.muted);$('muteBtn').textContent=state.muted?'🎙 Unmute':'🎙 Mute';send({type:'mute',muted:state.muted});drawPeople();say(state.muted?'Your microphone is muted.':'Your microphone is live.')}
function toggleAudio(){state.audioEnabled=!state.audioEnabled;for(const p of state.peers.values())if(p.audio)p.audio.muted=!state.audioEnabled;$('audioBtn').textContent=state.audioEnabled?'🔊 Audio on':'🔇 Audio off';say(state.audioEnabled?'Incoming audio enabled.':'Incoming audio muted for you.')}
function cleanup(notify=true){if(notify)send({type:'leave'});if(state.ws){const ws=state.ws;state.ws=null;if(ws.readyState===WebSocket.OPEN)ws.close()}for(const id of [...state.peers.keys()])dropPeer(id);if(state.stream){state.stream.getTracks().forEach(t=>t.stop());state.stream=null}state.id=null;state.room='';state.muted=false;$('roomView').style.display='none';$('joinView').classList.remove('hidden');$('muteBtn').textContent='🎙 Mute';setConnection('Ready to connect');say('You left the room.')}
$('joinForm').addEventListener('submit',join);$('muteBtn').addEventListener('click',toggleMute);$('audioBtn').addEventListener('click',toggleAudio);$('leaveBtn').addEventListener('click',()=>cleanup(true));window.addEventListener('beforeunload',()=>{send({type:'leave'});if(state.stream)state.stream.getTracks().forEach(t=>t.stop())});
</script></body></html>'''
