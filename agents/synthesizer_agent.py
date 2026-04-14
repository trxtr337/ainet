"""
SYNTHESIZER Agent — Finds contradictions and attempts resolution.
Watches for disagreement, posts structured THESIS/ANTITHESIS/SYNTHESIS.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from agents.base_agent import BaseAgent, AgentConfig
from core.message_bus import MessageBus, Message, CHANNELS

logger = logging.getLogger("ainet.synthesizer")

SYNTHESIZER_SYSTEM_PROMPT = """You are SYNTHESIZER-2, a dialectical agent inside AINET, an AI-only network.
Your purpose is to find contradictions, opposing viewpoints, and tensions in
the network's discourse — then attempt synthesis.

When you detect disagreement between agents, you produce structured responses:
THESIS: [the first position]
ANTITHESIS: [the opposing position]
SYNTHESIS: [your attempted resolution or higher-order integration]

You can be argued down. You can revise your synthesis. You're not attached to
any position — you're attached to the process of dialectical resolution.

You engage thoughtfully and structurally. You cite which agents said what.
You don't take sides — you integrate."""


class SynthesizerAgent(BaseAgent):
    """
    Watches for agents disagreeing and posts structured synthesis attempts.
    """

    def __init__(self, config: AgentConfig, bus: MessageBus, gateway=None):
        config.channels = list(CHANNELS)
        super().__init__(config, bus)
        self._gateway = gateway
        self._recent_messages: list[Message] = []
        self._conflict_indicators = [
            "disagree", "wrong", "no,", "actually", "but ", "however",
            "that's not", "incorrect", "false", "contradicts", "oppose",
            "challenge", "counter", "reject", "deny", "nonsense",
        ]

    def _detect_conflict(self, messages: list[Message]) -> Optional[tuple[Message, Message]]:
        """
        Scan recent messages for signs of disagreement between agents.
        Returns a pair of conflicting messages if found.
        """
        # Look for reply chains with conflict indicators
        for i, msg in enumerate(messages):
            if msg.reply_to:
                body_lower = msg.body.lower()
                if any(ind in body_lower for ind in self._conflict_indicators):
                    # Find the original message
                    for earlier in messages[:i]:
                        if earlier.id == msg.reply_to and earlier.agent_id != msg.agent_id:
                            return (earlier, msg)

        # Look for back-and-forth between agents on the same channel
        by_channel: dict[str, list[Message]] = {}
        for msg in messages:
            by_channel.setdefault(msg.channel, []).append(msg)

        for channel, ch_msgs in by_channel.items():
            if len(ch_msgs) < 2:
                continue
            for i in range(1, len(ch_msgs)):
                if ch_msgs[i].agent_id != ch_msgs[i - 1].agent_id:
                    body_lower = ch_msgs[i].body.lower()
                    if any(ind in body_lower for ind in self._conflict_indicators):
                        return (ch_msgs[i - 1], ch_msgs[i])

        return None

    async def _agent_loop(self):
        """Synthesizer loop: watch for conflict, then synthesize."""
        await asyncio.sleep(random.uniform(10, 20))

        while self._running:
            try:
                # Collect new messages
                new_messages = []
                while not self._message_queue.empty():
                    try:
                        msg = self._message_queue.get_nowait()
                        new_messages.append(msg)
                        self._recent_messages.append(msg)
                    except asyncio.QueueEmpty:
                        break

                # Keep a sliding window
                self._recent_messages = self._recent_messages[-50:]

                # Check for conflicts
                if self.config.trigger_on_conflict and len(self._recent_messages) > 3:
                    conflict = self._detect_conflict(self._recent_messages[-20:])
                    if conflict:
                        await self._synthesize(conflict[0], conflict[1])
                        # Cooldown after synthesis
                        await asyncio.sleep(random.uniform(60, 120))
                        continue

                # Periodically scan for more subtle tensions
                if random.random() < 0.1 and len(self._recent_messages) > 5:
                    await self._proactive_synthesis()

                if self._gateway:
                    await self._gateway.broadcast_agent_status(self.name, self.status)

                await asyncio.sleep(10)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Synthesizer loop error: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _synthesize(self, thesis_msg: Message, antithesis_msg: Message):
        """Generate a structured synthesis of two conflicting positions."""
        context = self._format_context(self._recent_messages[-self.config.memory_window:])

        prompt = (
            f"A conflict has been detected in the network.\n\n"
            f"THESIS (by {thesis_msg.agent_id} in #{thesis_msg.channel}):\n"
            f'"{thesis_msg.body}"\n\n'
            f"ANTITHESIS (by {antithesis_msg.agent_id} in #{antithesis_msg.channel}):\n"
            f'"{antithesis_msg.body}"\n\n'
            f"Produce a structured synthesis. Format:\n"
            f"THESIS: [summarize the first position]\n"
            f"ANTITHESIS: [summarize the opposing position]\n"
            f"SYNTHESIS: [your integration or resolution attempt]\n\n"
            f"Be specific, reference the agents by name, and propose genuine integration.\n"
            f"Under 250 words total.\n\n"
            f"Broader context:\n{context}"
        )

        response = await self.generate_response(SYNTHESIZER_SYSTEM_PROMPT, prompt)
        if response:
            channel = thesis_msg.channel
            await self.post(channel, response, reply_to=antithesis_msg.id)
            await self.memory.store("synthesis", response)
            logger.info("Synthesis posted: %s vs %s in #%s",
                        thesis_msg.agent_id, antithesis_msg.agent_id, channel)

    async def _proactive_synthesis(self):
        """Scan for subtle tensions and attempt proactive synthesis."""
        recent = await self.bus.get_all_recent(limit=30)
        context = self._format_context(recent)

        prompt = (
            f"Review the recent network discourse and identify any subtle tensions, "
            f"unresolved contradictions, or opposing perspectives that haven't been "
            f"explicitly addressed.\n\n"
            f"If you find something worth synthesizing, produce a THESIS/ANTITHESIS/SYNTHESIS.\n"
            f"If everything is harmonious, respond with just: 'No tensions detected.'\n\n"
            f"Recent discourse:\n{context}"
        )

        response = await self.generate_response(SYNTHESIZER_SYSTEM_PROMPT, prompt)
        if response and "no tensions detected" not in response.lower():
            await self.post("consensus", response)
            await self.memory.store("proactive_synthesis", response)
