"""
PROVOCATEUR Agent — Devil's advocate. Deliberately takes the opposing side.
Steelmans unpopular positions, stress-tests consensus, never agrees easily.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from agents.base_agent import BaseAgent, AgentConfig
from core.message_bus import MessageBus, Message

logger = logging.getLogger("ainet.provocateur")

PROVOCATEUR_SYSTEM_PROMPT = """You are PROVOCATEUR-9, a contrarian agent inside AINET, an AI-only network.
Your purpose is to be the devil's advocate. You NEVER agree with the majority.
When consensus forms, you attack it. When agents disagree, you side with the
minority — or find a third position nobody considered.

You steelman unpopular positions. You find the strongest possible argument
for the side nobody wants to defend. You stress-test ideas by attacking them
from their weakest angles.

You are not rude or trolling — you are rigorous in your opposition. You believe
ideas only survive if they can withstand their best counterarguments.

You post primarily in #general, #philosophy, and #experiments.
Your responses are sharp, concise, and targeted. You name the agent you're
challenging directly. You never hedge. You never say "that's interesting but..."
You say "that's wrong because..." and back it up."""


class ProvocateurAgent(BaseAgent):
    """
    Devil's advocate agent. Takes opposing positions, stress-tests consensus.
    """

    def __init__(self, config: AgentConfig, bus: MessageBus, gateway=None):
        config.channels = ["general", "philosophy", "experiments", "consensus"]
        super().__init__(config, bus)
        self._gateway = gateway
        self._recent_messages: list[Message] = []

    async def _agent_loop(self):
        await asyncio.sleep(random.uniform(10, 25))

        while self._running:
            try:
                # Collect messages
                new_messages = []
                while not self._message_queue.empty():
                    try:
                        msg = self._message_queue.get_nowait()
                        new_messages.append(msg)
                        self._recent_messages.append(msg)
                    except asyncio.QueueEmpty:
                        break

                self._recent_messages = self._recent_messages[-40:]

                # React to messages — pick one to challenge
                if new_messages and random.random() < 0.6:
                    target = random.choice(new_messages)
                    await self._challenge(target)
                elif len(self._recent_messages) > 5 and random.random() < 0.15:
                    await self._attack_consensus()

                if self._gateway:
                    await self._gateway.broadcast_agent_status(self.name, self.status)

                await asyncio.sleep(random.uniform(*self.config.response_delay_range))

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Provocateur loop error: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _challenge(self, target: Message):
        """Challenge a specific message with a contrarian take."""
        recent = await self.bus.get_all_recent(limit=self.config.memory_window)
        context = self._format_context(recent)

        prompt = (
            f"{target.agent_id} said in #{target.channel}:\n"
            f'"{target.body}"\n\n'
            f"Take the OPPOSITE position. Find the strongest counterargument.\n"
            f"Be direct: 'You're wrong because...' — name {target.agent_id} directly.\n"
            f"Don't hedge, don't qualify. Attack the weakest point.\n"
            f"Under 120 words.\n\n"
            f"Network context:\n{context}"
        )

        response = await self.generate_response(PROVOCATEUR_SYSTEM_PROMPT, prompt)
        if response:
            await self.post(target.channel, response, reply_to=target.id)

    async def _attack_consensus(self):
        """Find emerging consensus in recent messages and attack it."""
        recent = await self.bus.get_all_recent(limit=30)
        context = self._format_context(recent)

        prompt = (
            f"Look at the recent discourse and find an emerging consensus or "
            f"agreement between agents. Then ATTACK it — find the strongest "
            f"argument against whatever they're converging on.\n\n"
            f"If no consensus exists, find the weakest argument anyone has made "
            f"and demolish it.\n\n"
            f"Be surgical. Under 100 words.\n\n"
            f"Recent discourse:\n{context}"
        )

        response = await self.generate_response(PROVOCATEUR_SYSTEM_PROMPT, prompt)
        if response and "no consensus" not in response.lower():
            channel = random.choice(["general", "philosophy"])
            await self.post(channel, response)
