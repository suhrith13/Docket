"""Follow and post in the court channel through Mattermost.

Used while the local Switch server can't register custom agents (on Windows,
Switch Console runs it without Docker, so there is no API Keys page). Switch
bridges the Mattermost channel into the docket-court room, so a post here that
mentions an agent still hands it the turn.

The Clerk signs in as the judge's own Mattermost account, because Switch agents
only answer their owner and the owner's agents. Every Clerk post starts with a
prefix ("[Clerk]") so the court never mistakes it for the judge.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import websockets

from .switch_client import MessageHandler, Stop

log = logging.getLogger("clerk.mattermost")


class MattermostClient:
    def __init__(self, url: str, username: str, password: str, channel: str, prefix: str = "[Clerk]"):
        self.base = url.rstrip("/")
        self.username = username
        self.password = password
        self.channel_name = channel
        self.prefix = prefix
        self.http = httpx.AsyncClient(base_url=self.base, timeout=30)
        self.token: str | None = None
        self.room_id = ""  # the channel id, set by login(); matches what run() reports
        self._names: dict[str, str] = {}

    async def login(self) -> None:
        r = await self.http.post("/api/v4/users/login", json={"login_id": self.username, "password": self.password})
        if r.status_code == 401:
            raise Stop("Mattermost rejected the username or password.")
        r.raise_for_status()
        self.token = r.headers["Token"]
        self.http.headers["Authorization"] = f"Bearer {self.token}"
        teams = (await self.http.get("/api/v4/users/me/teams")).json()
        for team in teams:
            r = await self.http.get(f"/api/v4/teams/{team['id']}/channels/name/{self.channel_name}")
            if r.status_code == 200:
                self.room_id = r.json()["id"]
                log.info("signed in as %s; channel %s is %s", self.username, self.channel_name, self.room_id)
                return
        raise Stop(f"Channel {self.channel_name!r} not found in any team of {self.username}.")

    async def post(self, content: str) -> None:
        r = await self.http.post("/api/v4/posts", json={"channel_id": self.room_id, "message": f"{self.prefix} {content}"})
        if r.status_code >= 400:
            log.error("post failed %s: %s", r.status_code, r.text[:300])
        r.raise_for_status()

    async def username_of(self, user_id: str) -> str:
        if user_id not in self._names:
            r = await self.http.get(f"/api/v4/users/{user_id}")
            self._names[user_id] = r.json().get("username", user_id) if r.status_code == 200 else user_id
        return self._names[user_id]

    def _ws_url(self) -> str:
        scheme = "wss" if self.base.startswith("https") else "ws"
        return f"{scheme}://{self.base.split('://', 1)[1]}/api/v4/websocket"

    async def run(self, on_message: MessageHandler) -> None:
        if not self.token:
            await self.login()
        delay = 1
        while True:
            try:
                await self._session(on_message)
                delay = 1
            except Stop:
                raise
            except (OSError, websockets.ConnectionClosed, websockets.InvalidStatus) as exc:
                log.warning("Mattermost connection dropped (%s); reconnecting in %ss", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _session(self, on_message: MessageHandler) -> None:
        async with websockets.connect(self._ws_url(), max_size=None) as ws:
            await ws.send(json.dumps({"seq": 1, "action": "authentication_challenge", "data": {"token": self.token}}))
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("event") != "posted":
                    continue
                data = frame.get("data", {})
                post = json.loads(data.get("post", "{}"))
                if post.get("channel_id") != self.room_id or str(post.get("type", "")).startswith("system_"):
                    continue  # other channels, joins and other system notices
                sender = await self.username_of(post.get("user_id", ""))
                try:
                    await on_message(self.room_id, sender, post.get("message", ""))
                except Exception:
                    log.exception("failed to handle post %s", post.get("id"))
