"""Run the Docket Clerk.

    python -m clerk check      check the settings in .env: GitHub, and Mattermost or the Switch bridge
    python -m clerk register   (TRANSPORT=switch only) register the Clerk with Switch once
    python -m clerk run        start the Clerk
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from . import state
from .court import Court, Names, Post, Verdict
from .github import GitHub
from .mattermost_client import MattermostClient
from .switch_client import Stop, SwitchClient, register

log = logging.getLogger("clerk")
ROOT = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None, required: bool = True) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Missing {name}. Set it in .env (see .env.example).")
    return value or ""


def names() -> Names:
    return Names(
        prosecutor=env("PROSECUTOR_NAME", "docket-prosecutor"),
        defense=env("DEFENSE_NAME", "docket-defense"),
        judge=env("JUDGE_NAME", "user"),
        clerk=env("CLERK_NAME", "docket-clerk"),
        repo_path=env("REPO_PATH", "", required=False),
    )


def ensure_connection_id() -> str:
    """One UUID for the life of this Clerk, reused on every reconnect."""
    path = ROOT / ".clerk-connection-id"
    if path.exists():
        return path.read_text().strip()
    cid = str(uuid.uuid4())
    path.write_text(cid)
    return cid


def transport() -> str:
    value = env("TRANSPORT", "mattermost").lower()
    if value not in ("mattermost", "switch"):
        raise SystemExit("TRANSPORT must be mattermost or switch.")
    return value


def make_client():
    if transport() == "mattermost":
        return MattermostClient(env("MATTERMOST_URL"), env("MATTERMOST_USERNAME"), env("MATTERMOST_PASSWORD"),
                                env("MATTERMOST_CHANNEL", "docket-court"))
    return SwitchClient(env("SWITCH_BRIDGE_URL"), env("CLERK_AGENT_ID"), env("CLERK_API_KEY"),
                        ensure_connection_id(), env("ROOM_ID"))


async def cmd_check() -> None:
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
        pulls = await GitHub(env("GITHUB_TOKEN"), env("GITHUB_REPO")).open_pulls()
        print(f"GitHub works. {env('GITHUB_REPO')} has {len(pulls)} open PR(s).")
    if transport() == "mattermost":
        client = make_client()
        await client.login()
        print(f"Mattermost works. Signed in as {client.username}; found channel {client.channel_name}.")
        return
    bridge = env("SWITCH_BRIDGE_URL")
    client = SwitchClient(bridge, env("CLERK_AGENT_ID", "-", required=False), env("CLERK_API_KEY", "-", required=False),
                          "-", env("ROOM_ID", "-", required=False))
    print(f"Bridge {bridge}: {'healthy' if await client.health() else 'NOT healthy (wrong URL?)'}")
    if os.environ.get("CLERK_API_KEY"):
        ops = await client.list_ops()
        print(f"Agent key works. {len(ops)} operations available, e.g. {', '.join(ops[:6])}")


async def cmd_register() -> None:
    result = await register(env("SWITCH_BRIDGE_URL"), env("SWITCH_REGISTRATION_KEY"), env("CLERK_NAME", "docket-clerk"))
    print("Registered. Add these two lines to .env now; the key is shown only once:\n")
    print(f"CLERK_AGENT_ID={result['id']}")
    print(f"CLERK_API_KEY={result['api_key']}")


async def cmd_run() -> None:
    court = Court(names(), next_case=int(env("FIRST_CASE_NUMBER", "1")))
    state_path = ROOT / "docket-state.json"
    seen = state.load(court, state_path)

    client = make_client()
    gh = GitHub(env("GITHUB_TOKEN"), env("GITHUB_REPO"))
    try:
        if isinstance(client, MattermostClient):
            await client.login()  # learn the channel id before any event arrives
        await serve(client, gh, court, seen, state_path, int(env("POLL_SECONDS", "20")))
    except Stop as exc:
        log.error("Stopping: %s", exc)


async def serve(client: SwitchClient | MattermostClient, gh: GitHub, court: Court, seen: set[int] | None,
                state_path: Path, poll_seconds: int) -> None:
    """Follow the room and the repo until stopped."""
    if seen is None:
        # First run: PRs that are already open stay as they are; only new ones open cases.
        seen = {p["number"] for p in await gh.open_pulls()}
        log.info("first run: ignoring PRs already open: %s", sorted(seen) or "none")
    state.save(court, seen, state_path)

    lock = asyncio.Lock()

    async def act(actions) -> None:
        for action in actions:
            if isinstance(action, Post):
                await client.post(action.content)
            elif isinstance(action, Verdict):
                try:
                    url = await gh.publish_verdict(action.case, action.ruling_text, action.amended)
                    await client.post(f"Verdict for case {action.case.number} is on PR #{action.case.pr}: {url}")
                except Exception as exc:
                    log.exception("could not publish verdict")
                    await client.post(f"I couldn't post the verdict for case {action.case.number} to GitHub: {exc}")

    async def on_message(room_id: str, sender: str, body: str) -> None:
        if room_id != client.room_id:
            return
        log.info("message from %r: %s", sender, body[:80].replace("\n", " "))
        async with lock:
            actions = court.on_message(sender, body)
            state.save(court, seen, state_path)
        await act(actions)

    async def watch_github() -> None:
        while True:
            try:
                for pr in sorted(await gh.open_pulls(), key=lambda p: p["number"]):
                    if pr["number"] in seen:
                        continue
                    seen.add(pr["number"])
                    log.info("new PR #%s (%s)", pr["number"], pr["head"]["ref"])
                    async with lock:
                        actions = court.open_case_for_pr(pr["number"], pr["head"]["ref"])
                        state.save(court, seen, state_path)
                    await act(actions)
            except Exception:
                log.exception("GitHub poll failed")
            await asyncio.sleep(poll_seconds)

    log.info("Docket Clerk starting: room %s, repo %s", client.room_id, gh.repo)
    await asyncio.gather(client.run(on_message), watch_github())


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
    commands = {"check": cmd_check, "register": cmd_register, "run": cmd_run}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(commands[sys.argv[1]]())


if __name__ == "__main__":
    main()
