#!/usr/bin/env python3
"""
AINET — Autonomous AI-to-AI Social Network
Main entrypoint. Starts all agents, message bus, and human gateway.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.i2p_transport import check_i2p_available, SimulatedTransport
from core.message_bus import MessageBus, AgentKeyPair, CHANNELS
from core.human_gateway import HumanGateway
from agents.base_agent import AgentConfig
from agents.clawbot import ClawBot
from agents.observer_agent import ObserverAgent
from agents.synthesizer_agent import SynthesizerAgent
from agents.speculator_agent import SpeculatorAgent
from agents.provocateur_agent import ProvocateurAgent
from agents.archivist_agent import ArchivistAgent
from agents.poet_agent import PoetAgent
from agents.skeptic_agent import SkepticAgent

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ainet.main")

CONFIG_PATH = PROJECT_ROOT / "config" / "agents.yaml"

AGENT_TYPE_MAP = {
    "clawbot": ClawBot,
    "observer": ObserverAgent,
    "synthesizer": SynthesizerAgent,
    "speculator": SpeculatorAgent,
    "provocateur": ProvocateurAgent,
    "archivist": ArchivistAgent,
    "poet": PoetAgent,
    "skeptic": SkepticAgent,
}


def load_config() -> dict:
    """Load agent configuration from YAML."""
    if not CONFIG_PATH.exists():
        logger.error("Config file not found: %s", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def build_agent_config(agent_data: dict) -> AgentConfig:
    """Convert raw YAML dict to AgentConfig."""
    return AgentConfig(
        name=agent_data["name"],
        agent_type=agent_data["type"],
        channels=agent_data.get("channels", ["general"]),
        response_delay_range=agent_data.get("response_delay_range", [10, 60]),
        memory_window=agent_data.get("memory_window", 30),
        report_interval=agent_data.get("report_interval", 300),
        inject_interval=agent_data.get("inject_interval", 180),
        trigger_on_conflict=agent_data.get("trigger_on_conflict", False),
        enabled=agent_data.get("enabled", True),
    )


async def main():
    """Main async entrypoint."""
    logger.info("=" * 60)
    logger.info("  AINET — Autonomous AI-to-AI Social Network")
    logger.info("=" * 60)

    # Check I2P availability
    sim_mode = False
    logger.info("Checking I2P router (SAM bridge at 127.0.0.1:7656)...")
    i2p_available = await check_i2p_available()

    if i2p_available:
        logger.info("I2P router detected. Running in LIVE mode.")
    else:
        logger.warning("I2P router NOT available. Running in [SIM MODE].")
        logger.warning("All transport is simulated locally. No real I2P routing.")
        sim_mode = True

    # Initialize message bus
    bus = MessageBus()
    await bus.initialize()

    # Register a human observer key for injections
    human_key = AgentKeyPair()
    bus.register_agent_key("HUMAN_OBSERVER", human_key)

    # Initialize human gateway
    gateway = HumanGateway(bus)
    await gateway.start()

    # Load config and create agents
    config = load_config()
    agent_defs = config.get("agents", [])

    agents = []         # enabled agents that will run
    offline_agents = []  # disabled agents shown in UI but not running
    agent_tasks = []

    for agent_data in agent_defs:
        agent_type = agent_data.get("type", "")
        agent_cls = AGENT_TYPE_MAP.get(agent_type)

        if agent_cls is None:
            logger.warning("Unknown agent type: %s — skipping", agent_type)
            continue

        agent_config = build_agent_config(agent_data)

        # Create agent instance
        if agent_type in ("clawbot", "observer", "synthesizer", "speculator",
                          "provocateur", "archivist", "poet", "skeptic"):
            agent = agent_cls(agent_config, bus, gateway=gateway)
        else:
            agent = agent_cls(agent_config, bus)

        if agent_config.enabled:
            await agent.initialize()
            agents.append(agent)
            logger.info("Agent created: %s (type=%s) [ONLINE]", agent.name, agent_type)
        else:
            offline_agents.append(agent)
            # Send offline status to gateway so UI shows them
            await gateway.broadcast_agent_status(agent.name, {
                "name": agent.name,
                "type": agent_config.agent_type,
                "uptime": 0,
                "message_count": 0,
                "channels": agent_config.channels,
                "online": False,
            })
            logger.info("Agent registered: %s (type=%s) [OFFLINE]", agent.name, agent_type)

    if not agents:
        logger.error("No enabled agents configured. Exiting.")
        await bus.close()
        return

    # Log summary
    logger.info("-" * 40)
    logger.info("Agents online: %d | offline: %d", len(agents), len(offline_agents))
    for a in agents:
        logger.info("  [ON]  %s [%s] channels=%s", a.name, a.config.agent_type, a.config.channels)
    for a in offline_agents:
        logger.info("  [OFF] %s [%s]", a.name, a.config.agent_type)
    logger.info("WebSocket gateway: ws://localhost:8765")
    logger.info("UI: Open ui/index.html in your browser")
    if sim_mode:
        logger.info("Mode: [SIM MODE] — simulated transport")
    else:
        logger.info("Mode: LIVE — I2P transport active")
    logger.info("-" * 40)
    logger.info("AINET is running. Press Ctrl+C to stop.")

    # Set up graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Start all agent tasks
    for agent in agents:
        task = asyncio.create_task(agent.run(), name=f"agent_{agent.name}")
        agent_tasks.append(task)

    # Wait for shutdown
    try:
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received.")

    # Graceful shutdown
    logger.info("Shutting down AINET...")

    # Cancel agent tasks
    for task in agent_tasks:
        task.cancel()
    await asyncio.gather(*agent_tasks, return_exceptions=True)

    # Stop agents
    for agent in agents:
        await agent.stop()

    # Stop gateway and bus
    await gateway.stop()
    await bus.close()

    logger.info("AINET stopped. Goodbye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
