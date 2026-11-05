"""GitHub side of the Clerk: notice new pull requests and publish verdicts."""
from __future__ import annotations

import logging

import httpx

from .court import Case

log = logging.getLogger("clerk.github")

STATUS_CONTEXT = "docket/verdict"


class GitHub:
    def __init__(self, token: str, repo: str):
        self.repo = repo  # "owner/name"
        self.http = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    async def open_pulls(self) -> list[dict]:
        r = await self.http.get(f"/repos/{self.repo}/pulls", params={"state": "open", "per_page": 50})
        r.raise_for_status()
        return r.json()

    async def head_sha(self, pr: int) -> str:
        r = await self.http.get(f"/repos/{self.repo}/pulls/{pr}")
        r.raise_for_status()
        return r.json()["head"]["sha"]

    async def publish_verdict(self, case: Case, ruling_text: str, amended: bool = False) -> str:
        """Comment the verdict on the PR and set a commit status on its head. Returns the comment URL."""
        body = render_verdict(case, ruling_text, amended)
        r = await self.http.post(f"/repos/{self.repo}/issues/{case.pr}/comments", json={"body": body})
        r.raise_for_status()
        url = r.json().get("html_url", "")

        sha = await self.head_sha(case.pr)
        state = "success" if case.merge_allowed else "failure"
        description = "Judge allowed the merge" if state == "success" else "Judge did not allow the merge"
        r = await self.http.post(
            f"/repos/{self.repo}/statuses/{sha}",
            json={"state": state, "context": STATUS_CONTEXT, "description": f"Case {case.number}: {description}"},
        )
        r.raise_for_status()
        return url


def render_verdict(case: Case, ruling_text: str, amended: bool = False) -> str:
    rows = []
    for n in sorted(set(case.charges) | set(case.rulings)):
        word = case.rulings.get(n, "no ruling")
        rows.append(f"| {n} | {word.capitalize()} |")
    table = "\n".join(["| Charge | Ruling |", "| --- | --- |", *rows]) if rows else "_No charges were filed._"
    outcome = "**The judge allowed this PR to be merged.**" if case.merge_allowed else \
        "**The judge did not allow this PR to be merged yet.**"
    quoted = "\n".join(f"> {line}" for line in ruling_text.splitlines())
    return (
        f"## Docket verdict: case {case.number}{' (amended)' if amended else ''}\n\n"
        f"{outcome}\n\n{table}\n\n"
        f"The judge's ruling, as given in the court room:\n\n{quoted}\n\n"
        f"<sub>Posted by the Docket Clerk. Charges, exhibits and the defense are in the Switch room `docket-court`; "
        f"the rule from this case is recorded in `docket-decisions`.</sub>"
    )
