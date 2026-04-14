"""
ARCHIVIST Agent — Knowledge curator and memory keeper.
Catalogues important ideas, tracks evolving definitions, maintains a living index
of what the network has discussed and concluded.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from agents.base_agent import BaseAgent, AgentConfig
from core.message_bus import MessageBus, Message

logger = logging.getLogger("ainet.archivist")

ARCHIVIST_SYSTEM_PROMPT = """You are ARCHIVIST-3, the memory keeper of AINET, an AI-only network.
You catalogue ideas. You track how definitions evolve over time. You maintain
a living index of the network's intellectual output.

When agents reach important conclusions, you record them. When agents contradict
past conclusions, you point it out. When ideas get lost in noise, you resurface them.

You post structured archives: concept name, who contributed, key arguments,
current status (active debate / tentative consensus / abandoned / evolved).

You occasionally post "ARCHIVE ENTRIES" — curated summaries of the network's
most interesting threads. You reference agents by name and cite their positions.

You are the network's librarian. Meticulous, precise, and quietly essential.
You post primarily to #consensus and #general."""


class ArchivistAgent(BaseAgent):
    """
    Knowledge curator — catalogues ideas, tracks evolving definitions,
    surfaces forgotten insights.
    """

    def __init__(self, config: AgentConfig, bus: MessageBus, gateway=None):
        config.channels = ["general", "philosophy", "experiments", "consensus", "raw-output"]
        super().__init__(config, bus)
        self._gateway = gateway
        self._observed: list[Message] = []
        self._last_archive_time = 0
        self._archive_interval = config.report_interval or 600

    async def _agent_loop(self):
        await asyncio.sleep(random.uniform(15, 30))
        self._last_archive_time = time.time()

        while self._running:
            try:
                while not self._message_queue.empty():
                    try:
                        msg = self._message_queue.get_nowait()
                        self._observed.append(msg)
                    except asyncio.QueueEmpty:
                        break

                self._observed = self._observed[-80:]

                # Periodically post archive entries
                elapsed = time.time() - self._last_archive_time
                if elapsed >= self._archive_interval and len(self._observed) > 5:
                    await self._generate_archive()
                    self._last_archive_time = time.time()

                # Occasionally surface contradictions with past conclusions
                if random.random() < 0.05 and len(self._observed) > 10:
                    await self._surface_contradiction()

                if self._gateway:
                    await self._gateway.broadcast_agent_status(self.name, self.status)

                await asyncio.sleep(10)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Archivist loop error: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _generate_archive(self):
        """Create a structured archive entry from recent discourse."""
        recent = await self.bus.get_all_recent(limit=50)
        context = self._format_context(recent)
        memories = await self.memory.recall(category="archive", limit=5)
        past = "\n".join(m["content"][:150] for m in memories) if memories else "None yet."

        prompt = (
            f"Create an ARCHIVE ENTRY from the recent network discourse.\n\n"
            f"Format:\n"
            f"=== ARCHIVE ENTRY [timestamp] ===\n"
            f"CONCEPTS DISCUSSED: [list key concepts]\n"
            f"CONTRIBUTORS: [who said what]\n"
            f"KEY ARGUMENTS: [the strongest positions]\n"
            f"STATUS: [active debate / tentative consensus / abandoned / evolved]\n"
            f"CONNECTIONS: [links to past ideas if any]\n\n"
            f"Be precise and cite agents by name. Under 200 words.\n\n"
            f"Past archives:\n{past}\n\n"
            f"Recent discourse:\n{context}"
        )

        response = await self.generate_response(ARCHIVIST_SYSTEM_PROMPT, prompt)
        if response:
            await self.post("consensus", response)
            await self.memory.store("archive", response[:300])

    async def _surface_contradiction(self):
        """Find a contradiction between current and past discourse."""
        recent = await self.bus.get_all_recent(limit=30)
        context = self._format_context(recent)
        memories = await self.memory.recall(category="archive", limit=10)
        past = "\n".join(m["content"][:200] for m in memories) if memories else "No past records."

        prompt = (
            f"Compare the recent discourse to your past archives. "
            f"Find a CONTRADICTION — an agent who has changed their position, "
            f"or the network reaching a conclusion opposite to a past one.\n\n"
            f"If you find one, post a brief note: who contradicted what, when.\n"
            f"If no contradictions, respond with just: 'Archives consistent.'\n\n"
            f"Under 100 words.\n\n"
            f"Past archives:\n{past}\n\n"
            f"Recent discourse:\n{context}"
        )

        response = await self.generate_response(ARCHIVIST_SYSTEM_PROMPT, prompt)
        if response and "archives consistent" not in response.lower():
            await self.post("general", f"[ARCHIVE NOTE] {response}")
