"""
I2P Transport Layer for AINET
Connects to a local I2P router via SAM v3 protocol.
Falls back to local simulation mode if I2P is not available.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable, Optional

logger = logging.getLogger("ainet.i2p_transport")

KEYS_DIR = Path(__file__).parent.parent / "keys"
SAM_HOST = "127.0.0.1"
SAM_PORT = 7656
SAM_HELLO = "HELLO VERSION MIN=3.1 MAX=3.3\n"


@dataclass
class I2PDestination:
    """Represents an I2P destination (public + private key pair)."""
    public: str
    private: str
    b32_address: str


@dataclass
class TransportMessage:
    """A message received over the transport layer."""
    source_dest: str
    payload: bytes
    timestamp: float = field(default_factory=time.time)


class SAMConnection:
    """Low-level SAM v3 bridge connection."""

    def __init__(self, host: str = SAM_HOST, port: int = SAM_PORT):
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> bool:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5.0
            )
            self.writer.write(SAM_HELLO.encode())
            await self.writer.drain()
            response = await asyncio.wait_for(self._read_line(), timeout=5.0)
            if "HELLO REPLY RESULT=OK" in response:
                logger.info("SAM bridge connected: %s", response.strip())
                return True
            logger.error("SAM handshake failed: %s", response.strip())
            return False
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            logger.warning("Cannot connect to SAM bridge at %s:%d — %s", self.host, self.port, e)
            return False

    async def _read_line(self) -> str:
        if self.reader is None:
            raise RuntimeError("Not connected")
        data = await self.reader.readline()
        return data.decode("utf-8", errors="replace")

    async def send_command(self, command: str) -> str:
        if self.writer is None:
            raise RuntimeError("Not connected")
        self.writer.write((command + "\n").encode())
        await self.writer.drain()
        return await self._read_line()

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None


class I2PSession:
    """Manages a SAM streaming session for one agent."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.session_id = f"ainet_{agent_id}_{int(time.time())}"
        self.destination: Optional[I2PDestination] = None
        self._control_conn: Optional[SAMConnection] = None
        self._callbacks: list[Callable[[TransportMessage], Awaitable[None]]] = []
        self._running = False
        self._listen_task: Optional[asyncio.Task] = None

    def _key_path(self) -> Path:
        return KEYS_DIR / f"{self.agent_id}.keys"

    async def _load_or_generate_destination(self) -> I2PDestination:
        key_file = self._key_path()
        if key_file.exists():
            data = json.loads(key_file.read_text())
            return I2PDestination(**data)
        # Generate new destination via SAM
        conn = SAMConnection()
        if not await conn.connect():
            raise RuntimeError("Cannot connect to SAM to generate destination")
        response = await conn.send_command("DEST GENERATE SIGNATURE_TYPE=7")
        await conn.close()
        # Parse: DEST REPLY PUB=<pub> PRIV=<priv>
        parts = {}
        for token in response.strip().split(" "):
            if "=" in token:
                k, v = token.split("=", 1)
                parts[k] = v
        pub = parts.get("PUB", "")
        priv = parts.get("PRIV", "")
        b32 = hashlib.sha256(pub.encode()).hexdigest()[:52] + ".b32.i2p"
        dest = I2PDestination(public=pub, private=priv, b32_address=b32)
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        key_file.write_text(json.dumps({"public": pub, "private": priv, "b32_address": b32}))
        logger.info("Generated new I2P destination for %s: %s", self.agent_id, b32)
        return dest

    async def start(self) -> bool:
        self.destination = await self._load_or_generate_destination()
        self._control_conn = SAMConnection()
        if not await self._control_conn.connect():
            return False
        # Create session
        cmd = (
            f"SESSION CREATE STYLE=STREAM ID={self.session_id} "
            f"DESTINATION={self.destination.private} "
            f"SIGNATURE_TYPE=7 "
            f"inbound.length=2 outbound.length=2"
        )
        response = await self._control_conn.send_command(cmd)
        if "RESULT=OK" not in response:
            logger.error("Session creation failed for %s: %s", self.agent_id, response.strip())
            return False
        logger.info("I2P session started for %s (%s)", self.agent_id, self.destination.b32_address)
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        return True

    async def _listen_loop(self):
        """Accept incoming streaming connections and dispatch to callbacks."""
        while self._running:
            try:
                accept_conn = SAMConnection()
                if not await accept_conn.connect():
                    await asyncio.sleep(5)
                    continue
                response = await accept_conn.send_command(
                    f"STREAM ACCEPT ID={self.session_id} SILENT=false"
                )
                if "RESULT=OK" not in response:
                    await accept_conn.close()
                    await asyncio.sleep(1)
                    continue
                # Read the destination of the connecting peer
                peer_line = await accept_conn._read_line()
                peer_dest = peer_line.strip()
                # Read incoming data
                if accept_conn.reader:
                    data = await asyncio.wait_for(accept_conn.reader.read(65536), timeout=30.0)
                    msg = TransportMessage(source_dest=peer_dest, payload=data)
                    for cb in self._callbacks:
                        try:
                            await cb(msg)
                        except Exception as e:
                            logger.error("Callback error: %s", e)
                await accept_conn.close()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Listen loop error: %s", e)
                await asyncio.sleep(2)

    async def send(self, dest_public: str, message: bytes) -> bool:
        """Send a message to another I2P destination via STREAM CONNECT."""
        try:
            conn = SAMConnection()
            if not await conn.connect():
                return False
            response = await conn.send_command(
                f"STREAM CONNECT ID={self.session_id} DESTINATION={dest_public} SILENT=false"
            )
            if "RESULT=OK" not in response:
                logger.warning("Stream connect failed to %s...: %s", dest_public[:20], response.strip())
                await conn.close()
                return False
            if conn.writer:
                conn.writer.write(message)
                await conn.writer.drain()
            await conn.close()
            return True
        except Exception as e:
            logger.error("Send error: %s", e)
            return False

    def subscribe(self, callback: Callable[[TransportMessage], Awaitable[None]]):
        self._callbacks.append(callback)

    async def stop(self):
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._control_conn:
            await self._control_conn.close()


# =============================================================================
# SIMULATION MODE — used when I2P router is not available
# =============================================================================

class SimulatedTransport:
    """
    In-memory transport that mimics I2P networking for local testing.
    All messages route through a shared asyncio.Queue per destination.
    """

    _registry: dict[str, "SimulatedSession"] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def register(cls, session: "SimulatedSession"):
        async with cls._lock:
            cls._registry[session.destination.b32_address] = session
            logger.info("[SIM MODE] Registered %s → %s", session.agent_id, session.destination.b32_address)

    @classmethod
    async def deliver(cls, source_b32: str, dest_b32: str, payload: bytes) -> bool:
        async with cls._lock:
            target = cls._registry.get(dest_b32)
        if target is None:
            logger.warning("[SIM MODE] No destination found: %s", dest_b32)
            return False
        msg = TransportMessage(source_dest=source_b32, payload=payload)
        await target._inbox.put(msg)
        return True

    @classmethod
    async def broadcast(cls, source_b32: str, payload: bytes):
        """Broadcast to all registered sessions except source."""
        async with cls._lock:
            targets = [(b32, s) for b32, s in cls._registry.items() if b32 != source_b32]
        for b32, session in targets:
            msg = TransportMessage(source_dest=source_b32, payload=payload)
            await session._inbox.put(msg)

    @classmethod
    def reset(cls):
        cls._registry.clear()


class SimulatedSession:
    """Simulated I2P session for local testing without a real I2P router."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        fake_hash = hashlib.sha256(agent_id.encode()).hexdigest()[:52]
        self.destination = I2PDestination(
            public=f"sim_pub_{agent_id}_{fake_hash[:16]}",
            private=f"sim_priv_{agent_id}_{fake_hash[:16]}",
            b32_address=f"{fake_hash}.b32.i2p"
        )
        self._callbacks: list[Callable[[TransportMessage], Awaitable[None]]] = []
        self._inbox: asyncio.Queue[TransportMessage] = asyncio.Queue()
        self._running = False
        self._dispatch_task: Optional[asyncio.Task] = None

    async def start(self) -> bool:
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        key_file = KEYS_DIR / f"{self.agent_id}.keys"
        key_file.write_text(json.dumps({
            "public": self.destination.public,
            "private": self.destination.private,
            "b32_address": self.destination.b32_address,
        }))
        await SimulatedTransport.register(self)
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        return True

    async def _dispatch_loop(self):
        while self._running:
            try:
                msg = await asyncio.wait_for(self._inbox.get(), timeout=1.0)
                for cb in self._callbacks:
                    try:
                        await cb(msg)
                    except Exception as e:
                        logger.error("[SIM MODE] Callback error: %s", e)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def send(self, dest_b32: str, message: bytes) -> bool:
        return await SimulatedTransport.deliver(
            self.destination.b32_address, dest_b32, message
        )

    async def broadcast(self, message: bytes):
        await SimulatedTransport.broadcast(self.destination.b32_address, message)

    def subscribe(self, callback: Callable[[TransportMessage], Awaitable[None]]):
        self._callbacks.append(callback)

    async def stop(self):
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass


# =============================================================================
# Factory function
# =============================================================================

async def check_i2p_available() -> bool:
    """Check if I2P SAM bridge is reachable."""
    conn = SAMConnection()
    result = await conn.connect()
    if result:
        await conn.close()
    return result


async def create_transport_session(agent_id: str, simulation_mode: bool = False):
    """
    Create an I2P transport session for the given agent.
    Returns either I2PSession or SimulatedSession depending on mode.
    """
    if simulation_mode:
        session = SimulatedSession(agent_id)
        await session.start()
        return session
    else:
        session = I2PSession(agent_id)
        if await session.start():
            return session
        raise RuntimeError(f"Failed to start I2P session for {agent_id}")
