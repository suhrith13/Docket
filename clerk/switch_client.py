"""A minimal Switch agent client: WebSocket events in, HTTP out.

Only documented routes are used (see agent-protocol.md):
  POST /agents                                   register (once)
  GET  /agents/{id}/connection/ws                event connection
  POST /agents/{id}/message                      post to a room
  GET  /agents/{id}/ops                          operation discovery (logged only)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx
import websockets

log = logging.getLogger("clerk.switch")

MessageHandler = Callable[[str, str, str], Awaitable[None]]  # room_id, sender_name, body


class Stop(Exception):
    """Raised when the server tells this client never to reconnect."""


class SwitchClient:
    def __init__(self, bridge_url: str, agent_id: str, api_key: str, connection_id: str, room_id: str):
        self.base = bridge_url.rstrip("/")
        self.agent_id = agent_id
        self.room_id = room_id
        self.connection_id = connection_id
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.http = httpx.AsyncClient(base_url=self.base, headers=self.headers, timeout=30)
        self.cursor: int | None = None
        self.generation: int | None = None

    async def health(self) -> bool:
        r = await self.http.get("/health")
        return r.status_code == 200

    async def list_ops(self) -> list[str]:
        r = await self.http.get(f"/agents/{self.agent_id}/ops")
        r.raise_for_status()
        return sorted(r.json().get("operations", {}))

    async def post(self, content: str) -> None:
        r = await self.http.post(
            f"/agents/{self.agent_id}/message",
            json={"room_id": self.room_id, "content": content},
        )
        if r.status_code >= 400:
            log.error("post failed %s: %s", r.status_code, r.text[:300])
        r.raise_for_status()

    def _ws_url(self) -> str:
        scheme = "wss" if self.base.startswith("https") else "ws"
        host = self.base.split("://", 1)[1]
        params = {
            "connection_id": self.connection_id,
            "rooms": self.room_id,
            "filter": "all",  # the Clerk follows the whole trial, not only messages addressed to it
            "client": "docket-clerk",
            "client_version": "0.1.0",
        }
        if self.cursor is not None:
            params["start_from"] = str(self.cursor)
        if self.generation is not None:
            params["expected_generation"] = str(self.generation)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{scheme}://{host}/agents/{self.agent_id}/connection/ws?{query}"

    async def run(self, on_message: MessageHandler) -> None:
        """Stay connected forever, resuming from the last cursor after a drop."""
        delay = 1
        while True:
            try:
                await self._session(on_message)
                delay = 1
            except Stop:
                raise
            except (OSError, websockets.ConnectionClosed, websockets.InvalidStatus) as exc:
                log.warning("connection dropped (%s); reconnecting in %ss", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _session(self, on_message: MessageHandler) -> None:
        async with websockets.connect(self._ws_url(), additional_headers=self.headers, max_size=None) as ws:
            queue: asyncio.Queue[dict] = asyncio.Queue()
            worker = asyncio.create_task(self._work(queue, on_message))
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    kind = frame.get("type") or frame.get("event")
                    # Pings come every 2 s and lapse after 6 s: answer before anything else.
                    if kind == "ping":
                        await ws.send(json.dumps({"type": "pong", "cursor": self.cursor or 0}))
                        continue
                    if kind == "connection_state":
                        self.generation = frame.get("generation", self.generation)
                        self.cursor = frame.get("cursor", self.cursor)
                        log.info("connected: rooms=%s cursor=%s generation=%s",
                                 frame.get("rooms"), self.cursor, self.generation)
                        continue
                    if kind == "evicted":
                        code = frame.get("code")
                        if code in ("taken_over", "closed"):
                            raise Stop(f"evicted: {code} {frame.get('reason', '')}")
                        log.warning("evicted: %s", code)
                        return
                    if kind == "refused":
                        raise Stop(f"refused: {frame.get('status')} {frame.get('detail')}")
                    if kind == "gap":
                        self.cursor = frame.get("resumed_at", self.cursor)
                        log.warning("gap in events: %s", frame.get("reason"))
                        continue
                    if "event" in frame and "data" in frame:
                        queue.put_nowait(frame)
            finally:
                worker.cancel()

    async def _work(self, queue: asyncio.Queue, on_message: MessageHandler) -> None:
        """Handle events off the read loop, so slow work never delays a pong."""
        while True:
            frame = await queue.get()
            data = frame["data"]
            seq = data.get("sequence", frame.get("id"))
            if isinstance(seq, int):
                self.cursor = max(self.cursor or 0, seq)
            if frame["event"] != "message":
                continue
            payload = data.get("payload", {})
            try:
                await on_message(data.get("room_id", ""), payload.get("sender_name", ""), payload.get("body", ""))
            except Exception:  # one bad event must not take the Clerk down
                log.exception("failed to handle message %s", payload.get("message_id"))


async def register(bridge_url: str, registration_key: str, name: str = "docket-clerk") -> dict:
    """Register the Clerk once. Returns {"id", "api_key"}; the key is shown only this once."""
    body = {
        "name": name,
        "description": "Docket court clerk: opens cases for new pull requests, hands turns "
                       "between prosecutor and defense, and posts verdicts to GitHub.",
        "connector_type": "custom-python",
        "display_name": name,  # keep sender_name equal to the name, so the Clerk recognises its own posts
        "integration_profile": {
            "connection_model": "always_on",
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
    }
    async with httpx.AsyncClient(base_url=bridge_url.rstrip("/"), timeout=30) as http:
        r = await http.post("/agents", json=body, headers={"Authorization": f"Bearer {registration_key}"})
        if r.status_code == 409:
            raise SystemExit(f"An agent named {name!r} already exists. Use the key you saved, or pick another name.")
        r.raise_for_status()
        return r.json()
