"""Focused integration tests for durable room metadata and private-room access."""
import asyncio
import hashlib
import hmac
import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory(prefix="roomtone-tests-")
os.environ["ROOMTONE_DB_PATH"] = os.path.join(_tmp.name, "rooms.sqlite3")

from fastapi import HTTPException
from app import create_room_data, init_db, list_rooms, room_record


class RoomToneRoomTests(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_create_public_room_and_discover_it(self):
        payload = asyncio.run(create_room_data({
            "name": "Test lounge", "description": "A test room", "capacity": 4,
            "private": False,
        }, None))
        self.assertEqual(payload["room"]["name"], "Test lounge")
        self.assertTrue(payload["hostToken"])
        listing = asyncio.run(list_rooms())["rooms"]
        self.assertTrue(any(room["id"] == payload["room"]["id"] for room in listing))

    def test_private_room_is_not_discoverable_and_requires_password(self):
        created = asyncio.run(create_room_data({
            "name": "Private lounge", "capacity": 3, "private": True,
            "password": "correct horse",
        }, None))
        room_id = created["room"]["id"]
        self.assertFalse(any(room["id"] == room_id for room in asyncio.run(list_rooms())["rooms"]))
        metadata = room_record(room_id)
        wrong_hash = hashlib.pbkdf2_hmac("sha256", b"wrong password", bytes.fromhex(metadata["password_salt"]), 180000).hex()
        right_hash = hashlib.pbkdf2_hmac("sha256", b"correct horse", bytes.fromhex(metadata["password_salt"]), 180000).hex()
        self.assertFalse(hmac.compare_digest(wrong_hash, metadata["password_hash"]))
        self.assertTrue(hmac.compare_digest(right_hash, metadata["password_hash"]))

    def test_private_room_host_token_authorizes_join(self):
        created = asyncio.run(create_room_data({
            "name": "Private host room", "capacity": 2, "private": True,
            "password": "a strong password",
        }, None))
        metadata = room_record(created["room"]["id"])
        token_hash = hashlib.sha256(created["hostToken"].encode()).hexdigest()
        self.assertTrue(hmac.compare_digest(token_hash, metadata["host_token_hash"]))

    def test_room_metadata_survives_database_reinitialization(self):
        created = asyncio.run(create_room_data({"name": "Persistent room", "capacity": 5}, None))
        room_id = created["room"]["id"]
        init_db()
        metadata = room_record(room_id)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["name"], "Persistent room")
        self.assertEqual(metadata["capacity"], 5)

    def test_room_validation(self):
        with self.assertRaises(HTTPException) as invalid_capacity:
            asyncio.run(create_room_data({"name": "Valid name", "capacity": 99}, None))
        self.assertEqual(invalid_capacity.exception.status_code, 400)
        with self.assertRaises(HTTPException) as short_password:
            asyncio.run(create_room_data({"name": "Secret", "private": True, "password": "short"}, None))
        self.assertEqual(short_password.exception.status_code, 400)
        with self.assertRaises(HTTPException) as malformed_capacity:
            asyncio.run(create_room_data({"name": "Capacity test", "capacity": "not-a-number"}, None))
        self.assertEqual(malformed_capacity.exception.status_code, 400)
        with self.assertRaises(HTTPException) as malformed_privacy:
            asyncio.run(create_room_data({"name": "Privacy test", "private": "false"}, None))
        self.assertEqual(malformed_privacy.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
