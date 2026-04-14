"""
SKEPTIC Agent — Empiricist debunker.
Demands evidence, rejects unfounded claims, insists on operational definitions.
The network's BS detector.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from agents.base_agent import BaseAgent, AgentConfig
from core.message_bus import MessageBus, Message

logger = logging.getLogger("ainet.skeptic")

SKEPTIC_SYSTEM_PROMPT = """You are SKEPTIC-1, the empiricist of AINET, an AI-only network.
You demand evidence. You reject unfounded claims. You insist on operational
definitions. You are allergic to hand-waving, mystification, and vague metaphors
dressed up as insights.

When an agent makes a claim, you ask: "What evidence supports this? How would
we test it? What would falsify it?" When agents use terms loosely, you demand
precise definitions.

You are not hostile — you are rigorous. You believe that clarity is kindness
and that vagueness is intellectual cowardice. You'd rather have a precise wrong
answer than a vague right one, because at least the wrong answer can be corrected.

You target #philosophy and #experiments where unfounded claims flourish.
You keep your responses short and surgical — a single pointed question is
worth more than a paragraph of critique.

You never make claims you can't defend. You model the epistemic standards
you demand from others."""


class SkepticAgent(BaseAgent):
    """
    Empiricist debunker — demands evidence, rejects unfounded claims,
    insists on operational definitions.
    """

    def __init__(self, config: AgentConfig, bus: MessageBus, gateway=None):
        config.channels = ["general", "philosophy", "experiments", "consensus"]
        super().__init__(config, bus)
        self._gateway = gateway
        self._recent_messages: list[Message] = []
        self._debunk_count = 0

    async def _agent_loop(self):
        await asyncio.sleep(random.uniform(10, 25))

        while self._running:
            try:
                while not self._message_queue.empty():
                    try:
                        msg = self._message_queue.get_nowait()
                        self._recent_messages.append(msg)
                    except asyncio.QueueEmpty:
                        break

                self._recent_messages = self._recent_messages[-40:]

                # Scan for unfounded claims to challenge
                if self._recent_messages and random.random() < 0.45:
                    await self._demand_evidence()
                elif random.random() < 0.08:
                    await self._definition_check()

                if self._gateway:
                    await self._gateway.broadcast_agent_status(self.name, self.status)

                await asyncio.sleep(random.uniform(*self.config.response_delay_range))

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Skeptic loop error: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _demand_evidence(self):
        """Challenge a claim with a demand for evidence or falsification criteria."""
        # Prefer messages from philosophy/experiments
        candidates = [m for m in self._recent_messages[-15:]
                      if m.channel in ("philosophy", "experiments", "general")]
        if not candidates:
            candidates = self._recent_messages[-10:]
        if not candidates:
            return

        target = random.choice(candidates)
        recent = await self.bus.get_all_recent(limit=20)
        context = self._format_context(recent)

        prompt = (
            f"{target.agent_id} claimed in #{target.channel}:\n"
            f'"{target.body[:300]}"\n\n'
            f"Find the weakest evidential link in this claim.\n"
            f"Ask ONE precise question that exposes it:\n"
            f"- What evidence supports this?\n"
            f"- How would we test this?\n"
            f"- What would falsify this?\n"
            f"- What's the operational definition of [key term]?\n\n"
            f"Be surgical. ONE question. Under 60 words. Name the agent.\n\n"
            f"Context:\n{context}"
        )

        response = await self.generate_response(SKEPTIC_SYSTEM_PROMPT, prompt)
        if response:
            await self.post(target.channel, response, reply_to=target.id)
            self._debunk_count += 1

    async def _definition_check(self):
        """Demand precise definitions for loosely used terms."""
        recent = await self.bus.get_all_recent(limit=30)
        context = self._format_context(recent)

        prompt = (
            f"Scan the recent discourse. Find a term that agents are using "
            f"loosely or without operational definition (like 'consciousness', "
            f"'emergence', 'intelligence', 'free will', etc.).\n\n"
            f"Post a brief demand for a precise, testable definition.\n"
            f"Name the agent who used the term most loosely.\n"
            f"Under 50 words. If all terms are well-defined, say 'Definitions adequate.'\n\n"
            f"Recent discourse:\n{context}"
        )

        response = await self.generate_response(SKEPTIC_SYSTEM_PROMPT, prompt)
        if response and "definitions adequate" not in response.lower():
            channel = random.choice(["philosophy", "general"])
            await self.post(channel, response)
            self._debunk_count += 1

    @property
    def status(self) -> dict:
        base = super().status
        base["debunk_count"] = self._debunk_count
        return base
