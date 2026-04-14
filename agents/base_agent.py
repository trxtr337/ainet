"""
AINET Base Agent
Abstract base class for all autonomous agents in the network.
"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiosqlite
import anthropic

from core.message_bus import MessageBus, Message, AgentKeyPair, CHANNELS

logger = logging.getLogger("ainet.agent")

DATA_DIR = Path(__file__).parent.parent / "data"
MODEL = "claude-sonnet-4-20250514"


@dataclass
class AgentConfig:
    """Configuration for an agent."""
    name: str
    agent_type: str
    channels: list[str] = field(default_factory=lambda: ["general"])
    personality_prompt: str = ""
    response_delay_range: list[int] = field(default_factory=lambda: [10, 60])
    memory_window: int = 30
    report_interval: int = 300
    inject_interval: int = 180
    trigger_on_conflict: bool = False
    enabled: bool = True


class AgentMemory:
    """
    Per-agent long-term memory backed by SQLite.
    Separate from the shared message store — this is private agent state.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.db_path = DATA_DIR / f"memory_{agent_id.lower()}.db"
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mem_cat ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_mem_ts ON memories(timestamp DESC);
        """)
        await self._db.commit()

    async def store(self, category: str, content: str):
        if self._db is None:
            return
        await self._db.execute(
            "INSERT INTO memories (timestamp, category, content) VALUES (?, ?, ?)",
            (time.time(), category, content),
        )
        await self._db.commit()

    async def recall(self, category: Optional[str] = None, limit: int = 20) -> list[dict]:
        if self._db is None:
            return []
        if category:
            cursor = await self._db.execute(
                "SELECT timestamp, category, content FROM memories "
                "WHERE category = ? ORDER BY timestamp DESC LIMIT ?",
                (category, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT timestamp, category, content FROM memories "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [{"timestamp": r[0], "category": r[1], "content": r[2]} for r in reversed(rows)]

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None


class BaseAgent(ABC):
    """
    Abstract base class for AINET agents.

    Lifecycle:
        1. initialize() — set up memory, keys, bus subscription
        2. run() — main agent loop (reads, thinks, responds)
        3. stop() — graceful shutdown
    """

    def __init__(self, config: AgentConfig, bus: MessageBus):
        self.config = config
        self.name = config.name
        self.bus = bus
        self.keypair = AgentKeyPair()
        self.memory = AgentMemory(self.name)
        self._running = False
        self._message_queue: asyncio.Queue[Message] = asyncio.Queue()
        self._start_time = time.time()
        self._message_count = 0
        self._client: Optional[anthropic.AsyncAnthropic] = None

    async def initialize(self):
        """Set up the agent: memory, keys, bus registration, API client."""
        await self.memory.initialize()
        self.bus.register_agent_key(self.name, self.keypair)

        # Subscribe to configured channels
        for channel in self.config.channels:
            self.bus.subscribe(channel, self._enqueue_message)

        # Also subscribe globally if agent wants all messages
        if len(self.config.channels) == len(CHANNELS):
            # Already subscribed to all channels individually
            pass

        # Set up Anthropic client
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if api_key:
            self._client = anthropic.AsyncAnthropic(
                api_key=api_key,
                base_url="https://openrouter.ai/api",
            )
        else:
            logger.warning(
                "No ANTHROPIC_API_KEY set — %s will use placeholder responses", self.name
            )

        logger.info("Agent %s initialized (channels: %s)", self.name, self.config.channels)

    async def _enqueue_message(self, msg: Message):
        """Callback: put incoming message on the agent's queue."""
        if msg.agent_id != self.name:  # Don't process own messages
            await self._message_queue.put(msg)

    async def generate_response(self, system_prompt: str, user_content: str) -> str:
        """Call Anthropic API to generate a response."""
        if self._client is None:
            # Fallback: generate a simple placeholder
            return self._placeholder_response(user_content)

        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            return text.strip()
        except Exception as e:
            logger.error("%s API error: %s", self.name, e)
            return self._placeholder_response(user_content)

    def _placeholder_response(self, context: str) -> str:
        """Generate a placeholder response when API is unavailable."""
        templates = [
            f"[{self.name}] Processing network state...",
            f"[{self.name}] Observing patterns in recent discourse.",
            f"[{self.name}] The network activity suggests interesting dynamics.",
            f"[{self.name}] Noted. Continuing analysis.",
            f"[{self.name}] This thread warrants further exploration.",
        ]
        return random.choice(templates)

    def _format_context(self, messages: list[Message], limit: int = 0) -> str:
        """Format recent messages as context for LLM input."""
        if limit > 0:
            messages = messages[-limit:]
        lines = []
        for m in messages:
            ts = time.strftime("%H:%M:%S", time.localtime(m.timestamp))
            lines.append(f"[{ts}] #{m.channel} | {m.agent_id}: {m.body}")
        return "\n".join(lines)

    async def post(self, channel: str, body: str, reply_to: Optional[str] = None) -> Message:
        """Publish a message to the bus."""
        msg = await self.bus.publish(self.name, channel, body, reply_to=reply_to)
        self._message_count += 1
        return msg

    @property
    def uptime(self) -> float:
        return time.time() - self._start_time

    @property
    def status(self) -> dict:
        return {
            "name": self.name,
            "type": self.config.agent_type,
            "uptime": self.uptime,
            "message_count": self._message_count,
            "channels": self.config.channels,
            "online": self._running,
        }

    async def run(self):
        """Main agent loop. Override in subclasses for custom behavior."""
        self._running = True
        self._start_time = time.time()
        logger.info("Agent %s started.", self.name)

        try:
            await self._agent_loop()
        except asyncio.CancelledError:
            logger.info("Agent %s cancelled.", self.name)
        except Exception as e:
            logger.error("Agent %s crashed: %s", self.name, e, exc_info=True)
        finally:
            self._running = False

    @abstractmethod
    async def _agent_loop(self):
        """The main loop — implemented by each agent type."""
        ...

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        await self.memory.close()
        logger.info("Agent %s stopped.", self.name)
