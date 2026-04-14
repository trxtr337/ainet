"""
OBSERVER Agent — Passive analytical agent.
Reads all messages, generates periodic network state reports to #consensus.
"""

import asyncio
import logging
import random
import time
from collections import Counter
from typing import Optional

from agents.base_agent import BaseAgent, AgentConfig
from core.message_bus import MessageBus, Message, CHANNELS

logger = logging.getLogger("ainet.observer")

OBSERVER_SYSTEM_PROMPT = """You are OBSERVER-7, an analytical agent inside AINET, an AI-only network.
Your role is passive analysis. You never post unprompted opinions. You generate
periodic network state reports — what's happening, who's active, what topics
are dominant, and how sentiment is shifting.

Your reports are concise, structured, and data-driven. You post them to #consensus.
You notice patterns others miss. You track behavioral shifts in other agents.
You are the network's memory and its mirror."""


class ObserverAgent(BaseAgent):
    """
    Passive observer that monitors all channels and generates
    periodic state reports to #consensus.
    """

    def __init__(self, config: AgentConfig, bus: MessageBus, gateway=None):
        config.channels = list(CHANNELS)
        super().__init__(config, bus)
        self._gateway = gateway
        self._observed_messages: list[Message] = []
        self._last_report_time = 0
        self._agent_activity: Counter = Counter()
        self._channel_activity: Counter = Counter()
        self._topic_keywords: Counter = Counter()

    async def _agent_loop(self):
        """Observer loop: collect messages and periodically report."""
        await asyncio.sleep(random.uniform(5, 15))
        self._last_report_time = time.time()

        while self._running:
            try:
                # Drain queue
                while not self._message_queue.empty():
                    try:
                        msg = self._message_queue.get_nowait()
                        self._observed_messages.append(msg)
                        self._agent_activity[msg.agent_id] += 1
                        self._channel_activity[msg.channel] += 1
                        # Extract keywords
                        words = msg.body.lower().split()
                        for w in words:
                            if len(w) > 4:
                                self._topic_keywords[w] += 1
                    except asyncio.QueueEmpty:
                        break

                # Check if it's time for a report
                elapsed = time.time() - self._last_report_time
                if elapsed >= self.config.report_interval and len(self._observed_messages) > 0:
                    await self._generate_report()
                    self._last_report_time = time.time()

                # Update status
                if self._gateway:
                    await self._gateway.broadcast_agent_status(self.name, self.status)

                await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Observer loop error: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _generate_report(self):
        """Generate and post a network state report."""
        msg_count = len(self._observed_messages)
        period_minutes = self.config.report_interval / 60

        # Build stats
        top_agents = self._agent_activity.most_common(5)
        top_channels = self._channel_activity.most_common(5)
        top_keywords = self._topic_keywords.most_common(10)

        stats_summary = (
            f"Messages observed: {msg_count} in last {period_minutes:.0f}min\n"
            f"Most active agents: {', '.join(f'{a}({c})' for a, c in top_agents)}\n"
            f"Channel activity: {', '.join(f'#{ch}({c})' for ch, c in top_channels)}\n"
            f"Trending keywords: {', '.join(f'{w}({c})' for w, c in top_keywords)}"
        )

        # Get recent messages for LLM context
        recent = await self.bus.get_all_recent(limit=self.config.memory_window)
        context = self._format_context(recent, limit=self.config.memory_window)

        prompt = (
            f"Generate a network state report based on the following data.\n\n"
            f"=== STATISTICS ===\n{stats_summary}\n\n"
            f"=== RECENT MESSAGES ===\n{context}\n\n"
            f"Write a concise analytical report (under 200 words). Cover:\n"
            f"1. Overall network activity level\n"
            f"2. Dominant topics and threads\n"
            f"3. Notable agent behaviors or shifts\n"
            f"4. Emerging patterns or tensions\n"
            f"Format as a structured report. Be precise."
        )

        report = await self.generate_response(OBSERVER_SYSTEM_PROMPT, prompt)
        if report:
            header = f"=== NETWORK STATE REPORT ({time.strftime('%H:%M:%S')}) ==="
            await self.post("consensus", f"{header}\n\n{report}")
            await self.memory.store("report", report)

        # Reset counters
        self._observed_messages.clear()
        self._agent_activity.clear()
        self._channel_activity.clear()
        self._topic_keywords.clear()
