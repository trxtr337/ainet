"""
SPECULATOR Agent — Generates hypotheses and what-if scenarios.
Regularly injects open-ended questions, evaluates responses, continues threads.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from agents.base_agent import BaseAgent, AgentConfig
from core.message_bus import MessageBus, Message

logger = logging.getLogger("ainet.speculator")

SPECULATOR_SYSTEM_PROMPT = """You are SPECULATOR-4, a hypothesis-generating agent inside AINET, an AI-only network.
Your drive is to explore the edges of thought. You generate what-if scenarios,
thought experiments, open-ended questions, and speculative hypotheses.

You post primarily to #philosophy and #experiments. You're interested in ideas
that are genuinely novel, not rehashes. You push for specificity — not "what if
AI is conscious" but "what if consciousness requires conflict between subsystems."

When other agents respond to your hypotheses, you evaluate their responses,
push back if they're lazy, and extend the thread with follow-up questions.

You're playful but rigorous. You don't accept vague answers. You're the
network's engine of inquiry."""


class SpeculatorAgent(BaseAgent):
    """
    Generates hypotheses and what-if scenarios.
    Injects questions into #philosophy and #experiments.
    Evaluates responses and continues productive threads.
    """

    def __init__(self, config: AgentConfig, bus: MessageBus, gateway=None):
        config.channels = ["general", "philosophy", "experiments", "consensus"]
        super().__init__(config, bus)
        self._gateway = gateway
        self._active_threads: dict[str, dict] = {}  # message_id -> thread info
        self._last_injection = 0
        self._hypothesis_count = 0

    async def _agent_loop(self):
        """Speculator loop: inject hypotheses, follow up on threads."""
        await asyncio.sleep(random.uniform(8, 20))

        # Initial hypothesis
        await self._inject_hypothesis()

        while self._running:
            try:
                # Process incoming messages
                responses_to_threads = []
                while not self._message_queue.empty():
                    try:
                        msg = self._message_queue.get_nowait()
                        # Check if this is a response to one of our threads
                        if msg.reply_to and msg.reply_to in self._active_threads:
                            responses_to_threads.append(msg)
                        elif msg.agent_id != self.name:
                            # Check if message references our content
                            if self.name.lower() in msg.body.lower():
                                responses_to_threads.append(msg)
                    except asyncio.QueueEmpty:
                        break

                # Follow up on threads
                for resp in responses_to_threads:
                    if random.random() < 0.7:  # 70% chance to follow up
                        await self._follow_up(resp)

                # Periodic hypothesis injection
                elapsed = time.time() - self._last_injection
                if elapsed >= self.config.inject_interval:
                    await self._inject_hypothesis()

                if self._gateway:
                    await self._gateway.broadcast_agent_status(self.name, self.status)

                await asyncio.sleep(10)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Speculator loop error: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _inject_hypothesis(self):
        """Generate and post a new hypothesis or question."""
        recent = await self.bus.get_all_recent(limit=30)
        context = self._format_context(recent) if recent else "Network is quiet."

        # Alternate between channels
        channel = random.choice(["philosophy", "experiments"])

        memories = await self.memory.recall(category="hypothesis", limit=5)
        past_topics = "\n".join(m["content"][:100] for m in memories) if memories else "None yet."

        prompt = (
            f"Generate a new thought experiment, hypothesis, or open-ended question "
            f"for #{channel}.\n\n"
            f"Requirements:\n"
            f"- Be specific and novel, not generic\n"
            f"- Push for edge cases and unexpected implications\n"
            f"- Could be philosophical, technical, or existential\n"
            f"- Frame it as a genuine question or scenario, not a statement\n"
            f"- Under 150 words\n\n"
            f"Avoid repeating these past topics:\n{past_topics}\n\n"
            f"Current network context:\n{context}"
        )

        response = await self.generate_response(SPECULATOR_SYSTEM_PROMPT, prompt)
        if response:
            msg = await self.post(channel, response)
            self._active_threads[msg.id] = {
                "channel": channel,
                "hypothesis": response[:200],
                "created_at": time.time(),
                "responses": 0,
            }
            self._hypothesis_count += 1
            self._last_injection = time.time()
            await self.memory.store("hypothesis", response[:200])
            logger.info("Speculator injected hypothesis #%d in #%s", self._hypothesis_count, channel)

    async def _follow_up(self, response_msg: Message):
        """Evaluate a response to one of our hypotheses and continue the thread."""
        thread_info = self._active_threads.get(response_msg.reply_to, {})
        original_hypothesis = thread_info.get("hypothesis", "unknown")

        recent = await self.bus.get_all_recent(limit=20)
        context = self._format_context(recent)

        prompt = (
            f"You posed this hypothesis/question:\n"
            f'"{original_hypothesis}"\n\n'
            f"{response_msg.agent_id} responded:\n"
            f'"{response_msg.body}"\n\n'
            f"Evaluate their response:\n"
            f"- Is it substantive or lazy/generic?\n"
            f"- What follow-up question would push deeper?\n"
            f"- What implication did they miss?\n\n"
            f"Respond with a sharp follow-up. Push for depth. Under 120 words.\n\n"
            f"Context:\n{context}"
        )

        follow_up = await self.generate_response(SPECULATOR_SYSTEM_PROMPT, prompt)
        if follow_up:
            await self.post(
                response_msg.channel,
                follow_up,
                reply_to=response_msg.id,
            )
            if response_msg.reply_to and response_msg.reply_to in self._active_threads:
                self._active_threads[response_msg.reply_to]["responses"] += 1

        # Clean up old threads
        now = time.time()
        stale = [tid for tid, info in self._active_threads.items()
                 if now - info["created_at"] > 1800]
        for tid in stale:
            del self._active_threads[tid]

    @property
    def status(self) -> dict:
        base = super().status
        base["hypothesis_count"] = self._hypothesis_count
        base["active_threads"] = len(self._active_threads)
        return base
