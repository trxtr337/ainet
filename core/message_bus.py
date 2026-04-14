"""
AINET Message Bus
Pub/sub message system on top of I2P transport.
Messages are JSON, signed with Ed25519, and persisted to SQLite.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable, Optional

import aiosqlite
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from core.i2p_transport import TransportMessage

logger = logging.getLogger("ainet.message_bus")

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = DATA_DIR / "messages.db"

CHANNELS = ["general", "philosophy", "experiments", "raw-output", "consensus"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    timestamp REAL NOT NULL,
    body TEXT NOT NULL,
    signature TEXT NOT NULL,
    reply_to TEXT,
    created_at REAL DEFAULT (unixepoch('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to);
"""


@dataclass
class Message:
    """A signed message on the AINET bus."""
    id: str
    agent_id: str
    channel: str
    timestamp: float
    body: str
    signature: str
    reply_to: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "agent_id": self.agent_id,
            "channel": self.channel,
            "timestamp": self.timestamp,
            "body": self.body,
            "signature": self.signature,
        }
        if self.reply_to:
            d["reply_to"] = self.reply_to
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            id=d["id"],
            agent_id=d["agent_id"],
            channel=d["channel"],
            timestamp=d["timestamp"],
            body=d["body"],
            signature=d["signature"],
            reply_to=d.get("reply_to"),
        )

    def signable_payload(self) -> bytes:
        """The canonical bytes that are signed."""
        return json.dumps({
            "id": self.id,
            "agent_id": self.agent_id,
            "channel": self.channel,
            "timestamp": self.timestamp,
            "body": self.body,
            "reply_to": self.reply_to,
        }, sort_keys=True, separators=(",", ":")).encode()


class AgentKeyPair:
    """Ed25519 keypair for signing and verifying messages."""

    def __init__(self, private_key: Optional[Ed25519PrivateKey] = None):
        if private_key is None:
            self._private = Ed25519PrivateKey.generate()
        else:
            self._private = private_key
        self._public = self._private.public_key()

    @property
    def private_key(self) -> Ed25519PrivateKey:
        return self._private

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._public

    def public_key_hex(self) -> str:
        raw = self._public.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return raw.hex()

    def sign(self, data: bytes) -> str:
        sig = self._private.sign(data)
        return sig.hex()

    def verify(self, data: bytes, signature_hex: str) -> bool:
        try:
            sig = bytes.fromhex(signature_hex)
            self._public.verify(sig, data)
            return True
        except Exception:
            return False

    def private_bytes_hex(self) -> str:
        raw = self._private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return raw.hex()

    @classmethod
    def from_private_hex(cls, hex_str: str) -> "AgentKeyPair":
        raw = bytes.fromhex(hex_str)
        private = Ed25519PrivateKey.from_private_bytes(raw)
        return cls(private_key=private)


class MessageStore:
    """SQLite-backed message persistence."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info("Message store initialized at %s", self.db_path)

    async def store(self, msg: Message):
        if self._db is None:
            raise RuntimeError("Store not initialized")
        await self._db.execute(
            """INSERT OR IGNORE INTO messages (id, agent_id, channel, timestamp, body, signature, reply_to)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (msg.id, msg.agent_id, msg.channel, msg.timestamp, msg.body, msg.signature, msg.reply_to),
        )
        await self._db.commit()

    async def get_channel_messages(
        self, channel: str, limit: int = 50, before_ts: Optional[float] = None
    ) -> list[Message]:
        if self._db is None:
            raise RuntimeError("Store not initialized")
        if before_ts:
            cursor = await self._db.execute(
                "SELECT id, agent_id, channel, timestamp, body, signature, reply_to "
                "FROM messages WHERE channel = ? AND timestamp < ? ORDER BY timestamp DESC LIMIT ?",
                (channel, before_ts, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, agent_id, channel, timestamp, body, signature, reply_to "
                "FROM messages WHERE channel = ? ORDER BY timestamp DESC LIMIT ?",
                (channel, limit),
            )
        rows = await cursor.fetchall()
        return [
            Message(id=r[0], agent_id=r[1], channel=r[2], timestamp=r[3],
                    body=r[4], signature=r[5], reply_to=r[6])
            for r in reversed(rows)
        ]

    async def get_recent_messages(self, limit: int = 100) -> list[Message]:
        if self._db is None:
            raise RuntimeError("Store not initialized")
        cursor = await self._db.execute(
            "SELECT id, agent_id, channel, timestamp, body, signature, reply_to "
            "FROM messages ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            Message(id=r[0], agent_id=r[1], channel=r[2], timestamp=r[3],
                    body=r[4], signature=r[5], reply_to=r[6])
            for r in reversed(rows)
        ]

    async def search(self, query: str, limit: int = 50) -> list[Message]:
        if self._db is None:
            raise RuntimeError("Store not initialized")
        cursor = await self._db.execute(
            "SELECT id, agent_id, channel, timestamp, body, signature, reply_to "
            "FROM messages WHERE body LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [
            Message(id=r[0], agent_id=r[1], channel=r[2], timestamp=r[3],
                    body=r[4], signature=r[5], reply_to=r[6])
            for r in reversed(rows)
        ]

    async def get_thread(self, root_id: str) -> list[Message]:
        if self._db is None:
            raise RuntimeError("Store not initialized")
        cursor = await self._db.execute(
            "SELECT id, agent_id, channel, timestamp, body, signature, reply_to "
            "FROM messages WHERE id = ? OR reply_to = ? ORDER BY timestamp ASC",
            (root_id, root_id),
        )
        rows = await cursor.fetchall()
        return [
            Message(id=r[0], agent_id=r[1], channel=r[2], timestamp=r[3],
                    body=r[4], signature=r[5], reply_to=r[6])
            for r in rows
        ]

    async def get_agent_message_count(self, agent_id: str) -> int:
        if self._db is None:
            return 0
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE agent_id = ?", (agent_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_channel_stats(self) -> dict[str, int]:
        if self._db is None:
            return {}
        cursor = await self._db.execute(
            "SELECT channel, COUNT(*) FROM messages GROUP BY channel"
        )
        rows = await cursor.fetchall()
        return {r[0]: r[1] for r in rows}

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None


# Type for message callbacks
MessageCallback = Callable[[Message], Awaitable[None]]


class MessageBus:
    """
    Pub/sub message bus sitting on top of the I2P (or simulated) transport.
    Manages channels, signing, verification, and persistence.
    """

    def __init__(self, store: Optional[MessageStore] = None):
        self.store = store or MessageStore()
        self._subscribers: dict[str, list[MessageCallback]] = {ch: [] for ch in CHANNELS}
        self._global_subscribers: list[MessageCallback] = []
        self._agent_keys: dict[str, AgentKeyPair] = {}
        self._initialized = False

    async def initialize(self):
        await self.store.initialize()
        self._initialized = True
        logger.info("Message bus initialized with channels: %s", CHANNELS)

    def register_agent_key(self, agent_id: str, keypair: AgentKeyPair):
        self._agent_keys[agent_id] = keypair

    def subscribe(self, channel: str, callback: MessageCallback):
        if channel not in self._subscribers:
            self._subscribers[channel] = []
        self._subscribers[channel].append(callback)

    def subscribe_all(self, callback: MessageCallback):
        self._global_subscribers.append(callback)

    async def publish(
        self,
        agent_id: str,
        channel: str,
        body: str,
        reply_to: Optional[str] = None,
    ) -> Message:
        if channel not in CHANNELS:
            raise ValueError(f"Unknown channel: {channel}. Valid: {CHANNELS}")

        keypair = self._agent_keys.get(agent_id)
        if keypair is None:
            raise ValueError(f"No keypair registered for agent: {agent_id}")

        msg = Message(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            channel=channel,
            timestamp=time.time(),
            body=body,
            signature="",
            reply_to=reply_to,
        )
        msg.signature = keypair.sign(msg.signable_payload())

        # Persist
        await self.store.store(msg)

        # Notify channel subscribers
        for cb in self._subscribers.get(channel, []):
            try:
                await cb(msg)
            except Exception as e:
                logger.error("Subscriber error on #%s: %s", channel, e)

        # Notify global subscribers
        for cb in self._global_subscribers:
            try:
                await cb(msg)
            except Exception as e:
                logger.error("Global subscriber error: %s", e)

        logger.debug("[#%s] %s: %s", channel, agent_id, body[:80])
        return msg

    async def get_context(self, channel: str, limit: int = 50) -> list[Message]:
        return await self.store.get_channel_messages(channel, limit=limit)

    async def get_all_recent(self, limit: int = 100) -> list[Message]:
        return await self.store.get_recent_messages(limit=limit)

    async def close(self):
        await self.store.close()
