"""A whole case through the Mattermost transport, against a fake Mattermost.

The Clerk posts as the judge's account with a "[Clerk]" prefix; the room echoes
those posts back, and the court must not mistake them for the judge.
"""
import asyncio
import json

import httpx
import pytest
from websockets.asyncio.server import serve as ws_serve

from clerk.__main__ import serve
from clerk.court import Court, Names
from clerk.github import GitHub
from clerk.mattermost_client import MattermostClient

CHANNEL = "chan-court"
USERS = {"u-judge": "user", "u-pros": "docket-prosecutor", "u-def": "docket-defense"}
SCRIPT = {
    "@docket-prosecutor case 3: please review": ("u-pros",
        "Case 3 · PR #3 — 1 charge.\n**Charge 1:** crashes on empty input.\nExhibit:\n```\nIndexError\n```"),
    "@docket-defense case 3": ("u-def",
        "Case 3 · defense.\nCharge 1 (crash): conceded and fixed (abc123).\n```\n4 passed\n```"),
    "@docket-prosecutor case 3: re-run": ("u-pros", "Case 3 · re-run. Charge 1: resolved. I have no new charges."),
    "ready for a ruling": ("u-judge", "Ruling, case 3: Charge 1 sustained, fixed. PR #3 may be merged."),
}


@pytest.mark.timeout(20)
def test_whole_case_through_mattermost(tmp_path):
    asyncio.run(_run(tmp_path))


async def _run(tmp_path):
    posts: list[str] = []
    outbox: asyncio.Queue = asyncio.Queue()
    auth_ok = asyncio.Event()

    async def websocket(ws):
        challenge = json.loads(await ws.recv())
        assert challenge["action"] == "authentication_challenge" and challenge["data"]["token"] == "tok"
        auth_ok.set()
        await ws.send(json.dumps({"event": "hello", "data": {}}))

        async def feed():
            n = 0
            while True:
                user_id, message, channel = await outbox.get()
                n += 1
                await ws.send(json.dumps({"event": "posted", "data": {"post": json.dumps(
                    {"id": f"p{n}", "user_id": user_id, "channel_id": channel, "message": message, "type": ""})}}))

        feeder = asyncio.create_task(feed())
        try:
            await ws.wait_closed()
        finally:
            feeder.cancel()

    def mm_http(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/users/login":
            assert json.loads(request.content) == {"login_id": "user", "password": "pw"}
            return httpx.Response(200, headers={"Token": "tok"}, json={"id": "u-judge"})
        assert request.headers["authorization"] == "Bearer tok"
        if path == "/api/v4/users/me/teams":
            return httpx.Response(200, json=[{"id": "team-1"}])
        if path == "/api/v4/teams/team-1/channels/name/docket-court":
            return httpx.Response(200, json={"id": CHANNEL})
        if path.startswith("/api/v4/users/"):
            return httpx.Response(200, json={"username": USERS[path.rsplit("/", 1)[1]]})
        if path == "/api/v4/posts":
            body = json.loads(request.content)
            assert body["channel_id"] == CHANNEL and body["message"].startswith("[Clerk] ")
            posts.append(body["message"])
            outbox.put_nowait(("u-judge", body["message"], CHANNEL))  # echoed back, from the judge's account
            outbox.put_nowait(("u-judge", "noise in another channel", "chan-other"))
            for trigger, (user_id, reply) in SCRIPT.items():
                if trigger in body["message"]:
                    outbox.put_nowait((user_id, reply, CHANNEL))
            return httpx.Response(201, json={"id": "new"})
        return httpx.Response(404)

    polls = 0
    github_calls = []

    def github_http(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path.endswith("/pulls"):
            polls += 1
            return httpx.Response(200, json=[{"number": 3, "head": {"ref": "fix-empty"}}] if polls > 1 else [])
        if request.url.path.endswith("/pulls/3"):
            return httpx.Response(200, json={"head": {"sha": "abc123"}})
        github_calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"html_url": "https://github.com/x/y/pull/3#c1"})

    async with ws_serve(websocket, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = MattermostClient(f"http://127.0.0.1:{port}", "user", "pw", "docket-court")
        client.http = httpx.AsyncClient(base_url=client.base, transport=httpx.MockTransport(mm_http))
        await client.login()
        gh = GitHub("token", "x/y")
        gh.http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(github_http))
        court = Court(Names(repo_path="C:/repo"), next_case=3)

        task = asyncio.create_task(serve(client, gh, court, None, tmp_path / "state.json", poll_seconds=0))
        await asyncio.wait_for(auth_ok.wait(), 5)
        for _ in range(200):
            await asyncio.sleep(0.05)
            if any("Verdict for case 3" in p for p in posts):
                break
        task.cancel()

    joined = "\n".join(posts)
    for expected in ("Docket opened: case 3, PR #3", "@docket-defense case 3", "@docket-prosecutor case 3: re-run",
                     "ready for a ruling", "Verdict for case 3 is on PR #3"):
        assert expected in joined
    # The echoed "[Clerk] ... @docket-prosecutor case 3 ..." posts came from the judge's
    # account but must not have been read as the judge opening or ruling on a case.
    assert list(court.cases) == [3] and court.cases[3].rulings == {1: "sustained"}
    assert any(path.endswith("/statuses/abc123") and body["state"] == "success" for path, body in github_calls)
