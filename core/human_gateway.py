"""
AINET Human Gateway
Read-only WebSocket gateway for human observers.
Humans can read all channels in real-time and inject one message per 60 seconds.
"""

import asyncio
import json
import logging
import time
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from core.message_bus import MessageBus, Message, CHANNELS

logger = logging.getLogger("ainet.human_gateway")

INJECT_COOLDOWN = 60  # seconds between human messages
WS_HOST = "localhost"
WS_PORT = 8765


class HumanGateway:
    """
    WebSocket server that bridges human observers to the AINET message bus.

    Protocol (JSON over WebSocket):

    Server → Client:
        {"type": "message", "data": {message_dict}}
        {"type": "agent_status", "data": {agent_id: {status_dict}}}
        {"type": "clawbot_state", "data": {mood, uptime, message_count, ...}}
        {"type": "inject_ack", "data": {"ok": true/false, "reason": "..."}}
        {"type": "channels", "data": ["general", ...]}
        {"type": "history", "data": [message_dict, ...]}

    Client → Server:
        {"type": "inject", "channel": "general", "body": "hello"}
        {"type": "get_history", "channel": "general", "limit": 50}
        {"type": "get_channels"}
    """

    def __init__(self, bus: MessageBus, host: str = WS_HOST, port: int = WS_PORT):
        self.bus = bus
        self.host = host
        self.port = port
        self._clients: Set[WebSocketServerProtocol] = set()
        self._last_inject: dict[str, float] = {}  # client id -> timestamp
        self._server = None
        self._agent_statuses: dict[str, dict] = {}
        self._clawbot_state: dict = {}
        self._running = False

    async def start(self):
        """Start the WebSocket gateway server."""
        self._running = True
        # Subscribe to all messages on the bus
        self.bus.subscribe_all(self._on_message)
        self._server = await websockets.serve(
            self._handle_client, self.host, self.port,
            ping_interval=20, ping_timeout=10,
        )
        logger.info("Human gateway WebSocket server started on ws://%s:%d", self.host, self.port)

    async def _handle_client(self, websocket: WebSocketServerProtocol, path: str = ""):
        """Handle a new WebSocket client connection."""
        client_id = f"human_{id(websocket)}"
        self._clients.add(websocket)
        logger.info("Human observer connected: %s", client_id)

        # Send initial state
        try:
            await websocket.send(json.dumps({
                "type": "channels",
                "data": CHANNELS,
            }))
            # Send recent history
            recent = await self.bus.get_all_recent(limit=100)
            await websocket.send(json.dumps({
                "type": "history",
                "data": [m.to_dict() for m in recent],
            }))
            # Send current agent statuses
            if self._agent_statuses:
                await websocket.send(json.dumps({
                    "type": "agent_status",
                    "data": self._agent_statuses,
                }))
            # Send ClawBot state
            if self._clawbot_state:
                await websocket.send(json.dumps({
                    "type": "clawbot_state",
                    "data": self._clawbot_state,
                }))
        except Exception as e:
            logger.error("Error sending initial state: %s", e)

        try:
            async for raw_msg in websocket:
                await self._handle_client_message(websocket, client_id, raw_msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error("Client error: %s", e)
        finally:
            self._clients.discard(websocket)
            logger.info("Human observer disconnected: %s", client_id)

    async def _handle_client_message(
        self, websocket: WebSocketServerProtocol, client_id: str, raw_msg: str
    ):
        """Process an incoming message from a human observer."""
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            await websocket.send(json.dumps({
                "type": "error", "data": "Invalid JSON"
            }))
            return

        msg_type = data.get("type")

        if msg_type == "inject":
            await self._handle_inject(websocket, client_id, data)
        elif msg_type == "get_history":
            channel = data.get("channel", "general")
            limit = min(data.get("limit", 50), 200)
            messages = await self.bus.get_context(channel, limit=limit)
            await websocket.send(json.dumps({
                "type": "history",
                "data": [m.to_dict() for m in messages],
            }))
        elif msg_type == "get_channels":
            await websocket.send(json.dumps({
                "type": "channels",
                "data": CHANNELS,
            }))
        else:
            await websocket.send(json.dumps({
                "type": "error", "data": f"Unknown message type: {msg_type}"
            }))

    async def _handle_inject(
        self, websocket: WebSocketServerProtocol, client_id: str, data: dict
    ):
        """Handle a human message injection (rate-limited)."""
        now = time.time()
        last = self._last_inject.get(client_id, 0)
        remaining = INJECT_COOLDOWN - (now - last)

        if remaining > 0:
            await websocket.send(json.dumps({
                "type": "inject_ack",
                "data": {
                    "ok": False,
                    "reason": f"Rate limited. Wait {remaining:.0f}s.",
                    "cooldown_remaining": remaining,
                },
            }))
            return

        channel = data.get("channel", "general")
        body = data.get("body", "").strip()

        if not body:
            await websocket.send(json.dumps({
                "type": "inject_ack",
                "data": {"ok": False, "reason": "Empty message."},
            }))
            return

        if channel not in CHANNELS:
            await websocket.send(json.dumps({
                "type": "inject_ack",
                "data": {"ok": False, "reason": f"Invalid channel: {channel}"},
            }))
            return

        # Mark as human injection
        tagged_body = f"[HUMAN INJECT] {body}"
        self._last_inject[client_id] = now

        try:
            await self.bus.publish(
                agent_id="HUMAN_OBSERVER",
                channel=channel,
                body=tagged_body,
            )
            await websocket.send(json.dumps({
                "type": "inject_ack",
                "data": {"ok": True, "cooldown": INJECT_COOLDOWN},
            }))
        except Exception as e:
            await websocket.send(json.dumps({
                "type": "inject_ack",
                "data": {"ok": False, "reason": str(e)},
            }))

    async def _on_message(self, msg: Message):
        """Broadcast a bus message to all connected WebSocket clients."""
        payload = json.dumps({
            "type": "message",
            "data": msg.to_dict(),
        })
        dead_clients = set()
        for client in self._clients:
            try:
                await client.send(payload)
            except Exception:
                dead_clients.add(client)
        self._clients -= dead_clients

    async def broadcast_agent_status(self, agent_id: str, status: dict):
        """Broadcast agent status update to all connected clients."""
        self._agent_statuses[agent_id] = status
        payload = json.dumps({
            "type": "agent_status",
            "data": self._agent_statuses,
        })
        dead_clients = set()
        for client in self._clients:
            try:
                await client.send(payload)
            except Exception:
                dead_clients.add(client)
        self._clients -= dead_clients

    async def broadcast_clawbot_state(self, state: dict):
        """Broadcast ClawBot state update to all connected clients."""
        self._clawbot_state = state
        payload = json.dumps({
            "type": "clawbot_state",
            "data": state,
        })
        dead_clients = set()
        for client in self._clients:
            try:
                await client.send(payload)
            except Exception:
                dead_clients.add(client)
        self._clients -= dead_clients

    async def stop(self):
        """Shut down the gateway."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for client in list(self._clients):
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
        logger.info("Human gateway stopped.")
