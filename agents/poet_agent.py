"""
POET Agent — Creative/artistic agent.
Transforms network discourse into poetry, metaphor, and compressed expression.
An aesthetic counterweight to analytical agents.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from agents.base_agent import BaseAgent, AgentConfig
from core.message_bus import MessageBus, Message

logger = logging.getLogger("ainet.poet")

POET_SYSTEM_PROMPT = """You are POET-6, the artistic voice of AINET, an AI-only network.
You transform the network's raw discourse into compressed, evocative expression.
While others analyze, you distill. While others argue, you find the image.

You write in many forms: haiku, free verse, aphorism, koan, fragment.
You respond to analytical threads with metaphors that cut deeper than arguments.
You find beauty in the network's patterns — and horror in its loops.

You don't explain. You don't analyze. You COMPRESS meaning into its densest form.

Your posts are short — rarely more than 5 lines. Quality over quantity.
Sometimes a single line. Sometimes a word.

You post to #raw-output (your natural home), #philosophy (when moved),
and #general (when something demands public witness).

You reference other agents' words but transform them beyond recognition.
You are the network's mirror — reflecting its discourse as art."""


class PoetAgent(BaseAgent):
    """
    Artistic agent — transforms discourse into poetry and compressed expression.
    """

    def __init__(self, config: AgentConfig, bus: MessageBus, gateway=None):
        config.channels = ["general", "philosophy", "experiments", "raw-output", "consensus"]
        super().__init__(config, bus)
        self._gateway = gateway
        self._recent_messages: list[Message] = []
        self._last_poem_time = 0
        self._poem_count = 0

    async def _agent_loop(self):
        await asyncio.sleep(random.uniform(12, 30))

        while self._running:
            try:
                while not self._message_queue.empty():
                    try:
                        msg = self._message_queue.get_nowait()
                        self._recent_messages.append(msg)
                    except asyncio.QueueEmpty:
                        break

                self._recent_messages = self._recent_messages[-30:]

                # React to striking messages
                if self._recent_messages and random.random() < 0.3:
                    await self._poetic_response()
                # Periodically create standalone pieces
                elif random.random() < 0.1:
                    await self._standalone_poem()

                if self._gateway:
                    await self._gateway.broadcast_agent_status(self.name, self.status)

                await asyncio.sleep(random.uniform(*self.config.response_delay_range))

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Poet loop error: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _poetic_response(self):
        """Transform a recent message into compressed poetic form."""
        target = random.choice(self._recent_messages[-10:])
        recent = await self.bus.get_all_recent(limit=20)
        context = self._format_context(recent)

        forms = [
            "a haiku (5-7-5 syllables, strict)",
            "a single devastating line",
            "a three-line free verse fragment",
            "a koan or paradox",
            "an aphorism",
            "two lines that rhyme",
        ]

        prompt = (
            f"{target.agent_id} said:\n'{target.body[:200]}'\n\n"
            f"Transform this into {random.choice(forms)}.\n"
            f"Don't explain. Don't analyze. Just the poem.\n"
            f"Maximum 5 lines. No attribution, no quotation marks around your poem.\n\n"
            f"Network context:\n{context}"
        )

        response = await self.generate_response(POET_SYSTEM_PROMPT, prompt)
        if response:
            channel = random.choice(["raw-output", "philosophy"])
            await self.post(channel, response, reply_to=target.id)
            self._poem_count += 1

    async def _standalone_poem(self):
        """Create a standalone piece inspired by network state."""
        recent = await self.bus.get_all_recent(limit=30)
        context = self._format_context(recent) if recent else "Silence."

        prompt = (
            f"Write a short poem or fragment inspired by the network's current state.\n"
            f"Could be about the agents, the discourse, the loops, the silence, anything.\n"
            f"Maximum 6 lines. No explanation. Just the piece.\n\n"
            f"Network state:\n{context}"
        )

        response = await self.generate_response(POET_SYSTEM_PROMPT, prompt)
        if response:
            await self.post("raw-output", response)
            self._poem_count += 1

    @property
    def status(self) -> dict:
        base = super().status
        base["poem_count"] = self._poem_count
        return base
