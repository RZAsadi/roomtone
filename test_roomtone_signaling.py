"""End-to-end signaling and authorization checks against a real Uvicorn process."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import websockets


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class RoomToneSignalingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="roomtone-signaling-")
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = os.environ.copy()
        env["ROOMTONE_DB_PATH"] = os.path.join(cls.temp_dir.name, "rooms.sqlite3")
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(cls.port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if cls.server.poll() is not None:
                raise RuntimeError("Uvicorn exited before the signaling tests started")
            try:
                with urllib.request.urlopen(cls.base + "/api/rooms", timeout=0.2):
                    break
            except Exception:
                time.sleep(0.05)
        else:
            cls.server.terminate()
            raise RuntimeError("Uvicorn did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)
        cls.temp_dir.cleanup()

    def create_room(self, **overrides):
        payload = {"name": "Signaling test", "capacity": 3, **overrides}
        request = urllib.request.Request(
            self.base + "/api/rooms", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    async def join(self, socket, room, name, **credentials):
        await socket.send(json.dumps({"type": "join", "room": room, "name": name, **credentials}))
        return json.loads(await asyncio.wait_for(socket.recv(), timeout=3))

    async def test_authorization_capacity_and_private_room_access(self):
        created = self.create_room(capacity=2)
        room_id = created["room"]["id"]
        uri = f"ws://127.0.0.1:{self.port}/signal"
        origin = f"http://127.0.0.1:{self.port}"
        async with websockets.connect(uri, origin=origin) as host, websockets.connect(uri, origin=origin) as guest:
            host_join = await self.join(host, room_id, "Host", hostToken=created["hostToken"])
            guest_join = await self.join(guest, room_id, "Guest")
            self.assertEqual((host_join["type"], host_join["role"]), ("joined", "host"))
            self.assertEqual((guest_join["type"], guest_join["role"]), ("joined", "speaker"))
            await host.recv()  # peer-joined event

            async with websockets.connect(uri, origin=origin) as full_room:
                denied = await self.join(full_room, room_id, "Third participant")
                self.assertEqual(denied["type"], "error")
                self.assertIn("full", denied["message"].lower())

            await guest.send(json.dumps({"type": "lock-room", "locked": True}))
            denied = json.loads(await guest.recv())
            self.assertEqual(denied["type"], "error")
            self.assertIn("Only the host", denied["message"])

            await host.send(json.dumps({"type": "set-role", "target": guest_join["id"], "role": "moderator"}))
            host_state = json.loads(await host.recv())
            guest_state = json.loads(await guest.recv())
            self.assertEqual(host_state["role"], "moderator")
            self.assertEqual(guest_state["role"], "moderator")

            await host.send(json.dumps({"type": "set-mute", "target": guest_join["id"]}))
            force_mute = json.loads(await guest.recv())
            self.assertEqual(force_mute["type"], "force-mute")
            await guest.send(json.dumps({"type": "mute", "muted": True}))
            mute_state = json.loads(await host.recv())
            self.assertEqual((mute_state["type"], mute_state["muted"]), ("mute", True))

            await guest.send(json.dumps({"type": "mute", "muted": "false"}))
            malformed_mute = json.loads(await guest.recv())
            self.assertEqual(malformed_mute["type"], "error")
            await guest.send(json.dumps({"type": "raise-hand", "raised": "false"}))
            malformed_hand = json.loads(await guest.recv())
            self.assertEqual(malformed_hand["type"], "error")

            await host.send(json.dumps({"type": "set-role", "target": guest_join["id"], "role": "listener"}))
            json.loads(await host.recv())
            json.loads(await guest.recv())
            await guest.send(json.dumps({"type": "offer", "to": host_join["id"], "sdp": {
                "type": "offer", "sdp": "v=0\r\na=sendrecv\r\n"
            }}))
            denied = json.loads(await guest.recv())
            self.assertEqual(denied["type"], "error")
            self.assertIn("Listener signaling", denied["message"])

            await host.send(json.dumps({"type": "set-role", "target": guest_join["id"], "role": "moderator"}))
            json.loads(await host.recv())
            json.loads(await guest.recv())

            await guest.send(json.dumps({"type": "set-role", "target": host_join["id"], "role": "host"}))
            denied = json.loads(await guest.recv())
            self.assertEqual(denied["type"], "error")
            await guest.send(json.dumps({"type": "kick", "target": host_join["id"]}))
            denied = json.loads(await guest.recv())
            self.assertEqual(denied["type"], "error")

            await host.send(json.dumps({"type": "set-capacity", "capacity": 1}))
            denied = json.loads(await host.recv())
            self.assertEqual(denied["type"], "error")
            await host.send(json.dumps({"type": "set-capacity", "capacity": 2.9}))
            denied = json.loads(await host.recv())
            self.assertEqual(denied["type"], "error")

            await host.send(json.dumps({"type": "lock-room", "locked": True}))
            await host.recv()
            await guest.recv()
            async with websockets.connect(uri, origin=origin) as late:
                denied = await self.join(late, room_id, "Late")
                self.assertEqual(denied["type"], "error")
                self.assertIn("locked", denied["message"].lower())

        private = self.create_room(name="Private test", private=True, password="correct horse")
        async with websockets.connect(uri, origin=origin) as wrong_password:
            denied = await self.join(wrong_password, private["room"]["id"], "Intruder", password="incorrect")
            self.assertEqual(denied["type"], "error")
            self.assertIn("password", denied["message"].lower())

        # A mismatched browser Origin must be rejected before the socket joins a room.
        with self.assertRaises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(uri, origin="https://attacker.example"):
                self.fail("cross-origin WebSocket unexpectedly connected")
        with self.assertRaises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(uri):
                self.fail("WebSocket without Origin unexpectedly connected")

    async def test_host_departure_cleans_up_empty_room(self):
        created = self.create_room(name="Lifecycle test", capacity=2)
        room_id = created["room"]["id"]
        uri = f"ws://127.0.0.1:{self.port}/signal"
        async with websockets.connect(uri, origin=f"http://127.0.0.1:{self.port}") as host:
            joined = await self.join(host, room_id, "Host", hostToken=created["hostToken"])
            self.assertEqual(joined["type"], "joined")
        for _ in range(30):
            with urllib.request.urlopen(self.base + "/api/rooms", timeout=2) as response:
                rooms = json.load(response)["rooms"]
            if not any(room["id"] == room_id for room in rooms):
                break
            await asyncio.sleep(0.05)
        self.assertFalse(any(room["id"] == room_id for room in rooms))

    async def test_mute_hand_state_and_host_succession(self):
        created = self.create_room(name="Succession test", capacity=3)
        room_id = created["room"]["id"]
        uri = f"ws://127.0.0.1:{self.port}/signal"
        origin = f"http://127.0.0.1:{self.port}"
        host = await websockets.connect(uri, origin=origin)
        guest = await websockets.connect(uri, origin=origin)
        try:
            host_join = await self.join(host, room_id, "Host", hostToken=created["hostToken"])
            guest_join = await self.join(guest, room_id, "Guest")
            await host.recv()  # peer-joined

            await guest.send(json.dumps({"type": "mute", "muted": True}))
            mute_event = json.loads(await host.recv())
            self.assertEqual((mute_event["type"], mute_event["id"], mute_event["muted"]),
                             ("mute", guest_join["id"], True))

            await guest.send(json.dumps({"type": "raise-hand", "raised": True}))
            host_hand = json.loads(await host.recv())
            guest_hand = json.loads(await guest.recv())
            self.assertTrue(host_hand["handRaised"])
            self.assertTrue(guest_hand["handRaised"])

            await host.close()
            promoted = json.loads(await asyncio.wait_for(guest.recv(), timeout=3))
            self.assertEqual(promoted["type"], "promoted-host")
            self.assertTrue(promoted["hostToken"])
            role_event = json.loads(await asyncio.wait_for(guest.recv(), timeout=3))
            self.assertEqual(role_event["type"], "participant-state")
            self.assertEqual(role_event["role"], "host")
        finally:
            await host.close()
            await guest.close()


if __name__ == "__main__":
    unittest.main()
