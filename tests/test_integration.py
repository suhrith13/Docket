"""End to end against a fake Switch bridge and a fake GitHub.

The fake bridge speaks the documented wire format: a connection_state frame first,
pings that must be answered, and message events in the {event, data, id} envelope.
Fake agents answer the Clerk's handoffs the way the real ones did in cases 1 and 2.
"""
import asyncio
import json

import httpx
import pytest
from websockets.asyncio.server import serve as ws_serve

from clerk.__main__ import serve
from clerk.court import Court, Names
from clerk.github import GitHub
from clerk.switch_client import SwitchClient

ROOM = "room-1"
SCRIPT = {
    # When the Clerk's post contains this... the named agent answers with this.
    "@docket-prosecutor case 3: please review": ("docket-prosecutor",
        "Case 3 · PR #3 — 1 charge.\nCharge 1: crashes on empty input.\nExhibit:\n```\nIndexError\n```"),
    "@docket-defense case 3": ("docket-defense",
        "Case 3 · defense.\nCharge 1 (crash): conceded and fixed (abc123).\n```\n4 passed\n```"),
    "@docket-prosecutor case 3: re-run": ("docket-prosecutor",
        "Case 3 · re-run. Charge 1: resolved. I have no new charges."),
    "ready for a ruling": ("user", "Ruling, case 3: Charge 1 sustained, fixed. PR #3 may be merged."),
}


@pytest.mark.timeout(20)
def test_clerk_runs_a_whole_case(tmp_path):
    asyncio.run(_run(tmp_path))


async def _run(tmp_path):
    posts: list[str] = []
    pongs: list[dict] = []
    outbox: asyncio.Queue = asyncio.Queue()
    github_calls: list[tuple[str, dict]] = []
    seq = 0

    async def bridge(ws):
        nonlocal seq
        await ws.send(json.dumps({"type": "connection_state", "generation": 1, "cursor": 0, "rooms": [ROOM]}))

        async def pinger():
            while True:
                await ws.send(json.dumps({"type": "ping"}))
                await asyncio.sleep(0.1)

        async def reader():
            async for raw in ws:
                pongs.append(json.loads(raw))

        async def feeder():
            nonlocal seq
            while True:
                sender, body = await outbox.get()
                seq += 1
                await ws.send(json.dumps({"event": "message", "id": seq, "data": {
                    "type": "message", "room_id": ROOM, "bridge_id": None, "channel_type": "channel_public",
                    "sequence": seq, "payload": {"addressed": False, "sender": f"@{sender}:x", "sender_name": sender,
                                                 "message_id": f"m{seq}", "body": body, "timestamp": 0,
                                                 "thread_id": None, "attachments": []}}}))

        await asyncio.gather(pinger(), reader(), feeder())

    def switch_http(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer key"
        if request.url.path == "/agents/clerk-1/message":
            content = json.loads(request.content)["content"]
            posts.append(content)
            outbox.put_nowait(("docket-clerk", content))  # the room echoes the Clerk's own post
            for trigger, reply in SCRIPT.items():
                if trigger in content:
                    outbox.put_nowait(reply)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    polls = 0

    def github_http(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        path = request.url.path
        if path.endswith("/pulls") and request.method == "GET":
            polls += 1
            pulls = [{"number": 2, "head": {"ref": "old"}}]
            if polls > 1:  # PR 3 appears after the Clerk has started
                pulls.append({"number": 3, "head": {"ref": "fix-empty"}})
            return httpx.Response(200, json=pulls)
        if path.endswith("/pulls/3"):
            return httpx.Response(200, json={"head": {"sha": "abc123"}})
        if request.method == "POST":
            github_calls.append((path, json.loads(request.content)))
            return httpx.Response(201, json={"html_url": "https://github.com/x/y/pull/3#c1"})
        return httpx.Response(404)

    async with ws_serve(bridge, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = SwitchClient(f"http://127.0.0.1:{port}", "clerk-1", "key", "conn-1", ROOM)
        client.http = httpx.AsyncClient(base_url=client.base, headers=client.headers,
                                        transport=httpx.MockTransport(switch_http))
        gh = GitHub("token", "x/y")
        gh.http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(github_http))
        court = Court(Names(repo_path="C:/repo"), next_case=3)

        task = asyncio.create_task(serve(client, gh, court, None, tmp_path / "state.json", poll_seconds=0))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if any(p.startswith("Verdict for case 3") for p in posts):
                break
        await asyncio.sleep(0.3)  # let a few more pings arrive after the last event
        task.cancel()

    joined = "\n".join(posts)
    assert "Docket opened: case 3, PR #3" in joined
    assert "@docket-defense case 3" in joined
    assert "@docket-prosecutor case 3: re-run" in joined
    assert "ready for a ruling" in joined
    assert "Verdict for case 3 is on PR #3" in joined
    assert "PR #2" not in joined  # open before the Clerk started, so left alone

    comment = next(body for path, body in github_calls if path.endswith("/issues/3/comments"))
    assert "Docket verdict: case 3" in comment["body"] and "| 1 | Sustained |" in comment["body"]
    status = next(body for path, body in github_calls if "/statuses/abc123" in path)
    assert status["state"] == "success" and status["context"] == "docket/verdict"

    assert pongs and all(p["type"] == "pong" for p in pongs)
    assert pongs[-1]["cursor"] > 0  # the cursor advances as events are handled
    assert json.loads((tmp_path / "state.json").read_text())["cases"][0]["stage"] == "closed"
