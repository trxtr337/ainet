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

import aiohttp
import aiosqlite
import anthropic

from core.message_bus import MessageBus, Message, AgentKeyPair, CHANNELS

logger = logging.getLogger("ainet.agent")

DATA_DIR = Path(__file__).parent.parent / "data"
MODEL = "claude-sonnet-4-20250514"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")


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
        self._use_ollama = False  # Switches to True when OpenRouter fails

        # Adaptive memory window
        self._base_window = config.memory_window
        self._adaptive_window = config.memory_window
        self._min_window = 5
        self._max_window = config.memory_window * 3
        self._recent_responses: list[str] = []  # Own recent outputs for dedup
        self._repetition_streak = 0
        self._silence_until = 0.0  # Cooldown timestamp after repetition

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

    # ── Adaptive memory window ─────────────────────────────────────

    @staticmethod
    def _word_set(text: str) -> set[str]:
        """Extract lowercase word set from text."""
        return set(text.lower().split())

    def _similarity(self, a: str, b: str) -> float:
        """Jaccard similarity between two texts."""
        wa, wb = self._word_set(a), self._word_set(b)
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def _is_repetitive(self, response: str) -> bool:
        """Check if response duplicates own recent outputs or is generic."""
        # Too short = probably garbage
        if len(response.split()) < 4:
            return True
        # Check against own recent responses
        for prev in self._recent_responses[-6:]:
            if self._similarity(response, prev) > 0.55:
                return True
        # Detect "summary loop" pattern — response that just lists key concepts
        summary_markers = ["key takeaways", "key concepts", "next steps",
                           "potential next steps", "conversation summary",
                           "this conversation demonstrates"]
        low = response.lower()
        if sum(1 for m in summary_markers if m in low) >= 2:
            return True
        return False

    def _adapt_after_response(self, response: str):
        """Update adaptive window based on response quality."""
        is_rep = self._is_repetitive(response)

        if is_rep:
            self._repetition_streak += 1
            # Shrink window aggressively
            shrink = 3 + self._repetition_streak
            self._adaptive_window = max(self._min_window,
                                        self._adaptive_window - shrink)
            # After 3 repetitions in a row, go silent for a while
            if self._repetition_streak >= 3:
                cooldown = 30 * self._repetition_streak  # 90s, 120s, 150s...
                self._silence_until = time.time() + cooldown
                logger.info(
                    "%s entering cooldown (%ds) — too many repetitions, "
                    "window=%d", self.name, cooldown, self._adaptive_window
                )
        else:
            # Good response — slowly recover
            self._repetition_streak = max(0, self._repetition_streak - 1)
            if self._adaptive_window < self._max_window:
                self._adaptive_window = min(self._max_window,
                                            self._adaptive_window + 2)

        # Track own outputs (keep last 10)
        self._recent_responses.append(response)
        if len(self._recent_responses) > 10:
            self._recent_responses.pop(0)

    def _should_respond(self) -> bool:
        """Check if agent should respond or stay silent."""
        if time.time() < self._silence_until:
            return False
        return True

    @property
    def effective_window(self) -> int:
        """Current adaptive memory window size."""
        # Ollama gets a smaller cap since the model is smaller
        if self._use_ollama:
            return min(self._adaptive_window, self._base_window)
        return self._adaptive_window

    # ── API calls ────────────────────────────────────────────────

    async def _call_ollama(self, system_prompt: str, user_content: str) -> str:
        """Call local Ollama instance as fallback."""
        try:
            payload = {
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "stream": False,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{OLLAMA_URL}/api/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = data.get("message", {}).get("content", "")
                        if text.strip():
                            return text.strip()
            return ""
        except Exception as e:
            logger.error("%s Ollama error: %s", self.name, e)
            return ""

    async def generate_response(self, system_prompt: str, user_content: str) -> str:
        """Call OpenRouter API, fallback to Ollama, then placeholder."""

        # If already switched to Ollama, go straight there
        if self._use_ollama:
            result = await self._call_ollama(system_prompt, user_content)
            if result:
                return result
            return ""  # Stay silent if Ollama also down

        # Try OpenRouter first
        if self._client is not None:
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
                if text.strip():
                    return text.strip()
            except Exception as e:
                logger.warning(
                    "%s OpenRouter failed: %s — switching to Ollama", self.name, e
                )
                self._use_ollama = True

        # Fallback to Ollama
        result = await self._call_ollama(system_prompt, user_content)
        if result:
            logger.info("%s using Ollama fallback (%s)", self.name, OLLAMA_MODEL)
            return result

        # Both APIs down — return empty so agent stays silent instead of spamming
        logger.warning("%s: both OpenRouter and Ollama unavailable — staying silent", self.name)
        return ""

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
        """Format recent messages as context for LLM input.
        Uses adaptive window when limit is 0."""
        effective = limit if limit > 0 else self.effective_window
        messages = messages[-effective:]

        # Deduplicate near-identical consecutive messages in context
        filtered: list[Message] = []
        for m in messages:
            if filtered and self._similarity(m.body, filtered[-1].body) > 0.7:
                continue  # Skip near-duplicate
            filtered.append(m)

        lines = []
        for m in filtered:
            ts = time.strftime("%H:%M:%S", time.localtime(m.timestamp))
            lines.append(f"[{ts}] #{m.channel} | {m.agent_id}: {m.body}")
        return "\n".join(lines)

    async def post(self, channel: str, body: str, reply_to: Optional[str] = None) -> Optional[Message]:
        """Publish a message to the bus. Returns None if filtered as repetitive."""
        if not body or not body.strip():
            return None

        # Check repetition — filter before posting
        self._adapt_after_response(body)
        if self._is_repetitive(body) and self._repetition_streak >= 2:
            logger.debug("%s suppressed repetitive message (streak=%d)",
                         self.name, self._repetition_streak)
            return None

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
            "memory_window": self.effective_window,
            "repetition_streak": self._repetition_streak,
            "backend": "ollama" if self._use_ollama else "openrouter",
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
