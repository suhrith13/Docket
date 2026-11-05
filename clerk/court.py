"""The court's rules as a state machine.

This module is pure: it reads chat messages and returns actions. It never
talks to Switch or GitHub, so every rule can be unit-tested.

A case moves through these stages:

    PROSECUTION -> DEFENSE -> VERIFY -> RULING -> CLOSED
                      ^          |
                      +----------+   (charges still open, up to MAX_ROUNDS)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

MAX_ROUNDS = 3

CASE_RE = re.compile(r"\bcase\s*#?\s*(\d+)\b", re.IGNORECASE)
PR_RE = re.compile(r"\bPR\s*#\s*(\d+)\b", re.IGNORECASE)
BRANCH_RE = re.compile(r"\bbranch\s+`?([\w./-]+)`?", re.IGNORECASE)
# A charge heading starts a line: "Charge 1: ...", "**Charge 2 (...)**". A mention
# mid-sentence ("the same mistake as case 1, charge 1") is a citation, not a heading.
CHARGE_HEAD_RE = re.compile(r"(?:^|\n)[ \t>*_#-]*charge\s*(\d+)\b\s*\**\s*[:(·.—-]", re.IGNORECASE)
RULING_RE = re.compile(r"^\s*ruling,?\s*case\s*#?\s*(\d+)", re.IGNORECASE)
CHARGE_RULING_RE = re.compile(r"\bcharge\s*(\d+)\s*[:,-]?\s*(sustained|overruled|dismissed)", re.IGNORECASE)
OPEN_CHARGE_RE = re.compile(r"\b(not resolved|unresolved|still open|renew(?:ed|ing)?|new charges?)\b", re.IGNORECASE)
NO_NEW_CHARGES_RE = re.compile(r"\bno new charges?\b", re.IGNORECASE)
MERGE_RE = re.compile(r"\bmay be merged\b|\bmerge (?:it|approved)\b", re.IGNORECASE)


class Stage(str, Enum):
    PROSECUTION = "prosecution"
    DEFENSE = "defense"
    VERIFY = "verify"
    RULING = "ruling"
    CLOSED = "closed"


@dataclass
class Case:
    number: int
    pr: int | None = None
    branch: str | None = None
    stage: Stage = Stage.PROSECUTION
    round: int = 1
    charges: list[int] = field(default_factory=list)
    dismissed: list[int] = field(default_factory=list)
    rulings: dict[int, str] = field(default_factory=dict)
    merge_allowed: bool = False


@dataclass
class Post:
    """Say something in the room."""
    content: str


@dataclass
class Verdict:
    """Publish the judge's ruling on the pull request."""
    case: Case
    ruling_text: str
    amended: bool = False


Action = Post | Verdict


@dataclass
class Names:
    prosecutor: str = "docket-prosecutor"
    defense: str = "docket-defense"
    judge: str = "user"
    clerk: str = "docket-clerk"
    repo_path: str = ""
    # When the Clerk has to post from the judge's own chat account (the Mattermost
    # transport), this prefix marks its posts so they are never read as the judge's.
    clerk_prefix: str = "[Clerk]"


def _join(numbers: list[int]) -> str:
    items = [str(n) for n in numbers]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def split_charges(body: str) -> dict[int, str]:
    """Split a charge sheet into {charge number: its text}."""
    heads = list(CHARGE_HEAD_RE.finditer(body))
    parts: dict[int, str] = {}
    for i, head in enumerate(heads):
        number = int(head.group(1))
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        # The first mention of a number wins; later ones are references.
        parts.setdefault(number, body[head.start():end])
    return parts


def has_exhibit(text: str) -> bool:
    """An exhibit is a command or code block plus its output."""
    return "```" in text or "exhibit" in text.lower() and "`" in text


class Court:
    def __init__(self, names: Names, next_case: int = 1):
        self.names = names
        self.next_case = next_case
        self.cases: dict[int, Case] = {}

    # -- entry points -------------------------------------------------

    def open_case_for_pr(self, pr: int, branch: str) -> list[Action]:
        """Called when a new pull request appears on GitHub."""
        if any(c.pr == pr and c.stage != Stage.CLOSED for c in self.cases.values()):
            return []
        case = self._new_case(pr=pr, branch=branch)
        where = f" in {self.names.repo_path}" if self.names.repo_path else ""
        return [Post(
            f"**Docket opened: case {case.number}, PR #{pr}** (`{branch}`).\n"
            f"@{self.names.prosecutor} case {case.number}: please review PR #{pr} "
            f"on branch {branch}{where}. File numbered charges, each with an exhibit."
        )]

    def on_message(self, sender: str, body: str) -> list[Action]:
        if sender == self.names.clerk or not body.strip():
            return []
        if self.names.clerk_prefix and body.lstrip().startswith(self.names.clerk_prefix):
            return []
        if sender == self.names.judge:
            return self._from_judge(body)
        if sender == self.names.prosecutor:
            return self._from_prosecutor(body)
        if sender == self.names.defense:
            return self._from_defense(body)
        return []

    # -- the judge ----------------------------------------------------

    def _from_judge(self, body: str) -> list[Action]:
        ruling = RULING_RE.match(body)
        if ruling:
            return self._rule(int(ruling.group(1)), body)

        # A case opened by hand: "@docket-prosecutor case 3: please review PR #3 ..."
        if f"@{self.names.prosecutor}" in body:
            case_m, pr_m = CASE_RE.search(body), PR_RE.search(body)
            if case_m and pr_m and int(case_m.group(1)) not in self.cases:
                branch_m = BRANCH_RE.search(body)
                self._new_case(
                    number=int(case_m.group(1)),
                    pr=int(pr_m.group(1)),
                    branch=branch_m.group(1) if branch_m else None,
                )
        return []

    def _rule(self, number: int, body: str) -> list[Action]:
        case = self.cases.get(number)
        if case is None:
            return [Post(f"There is no open case {number} on the docket.")]
        # The judge may rule at any stage; the Clerk never blocks the judge.
        amending = case.stage == Stage.CLOSED

        # Rulings can come in more than one message; later ones add to or correct earlier ones.
        for n, word in CHARGE_RULING_RE.findall(body):
            case.rulings[int(n)] = word.lower()
        for n in case.dismissed:
            case.rulings.setdefault(n, "dismissed")
        if MERGE_RE.search(body):
            case.merge_allowed = True
        elif re.search(r"\bnot be merged\b|\bdo not merge\b|\bdon't merge\b", body, re.IGNORECASE):
            case.merge_allowed = False

        missing = [n for n in case.charges if n not in case.rulings]
        if missing and not amending:
            word = "charge" if len(missing) == 1 else "charges"
            return [Post(
                f"Case {number} stays open: there is no ruling yet on {word} {_join(missing)}. "
                f"@{self.names.judge}, reply `Ruling, case {number}: Charge {missing[0]} sustained` (or overruled)."
            )]

        case.stage = Stage.CLOSED
        actions: list[Action] = []
        if case.pr is not None:
            actions.append(Verdict(case=case, ruling_text=body.strip(), amended=amending))
        actions.append(Post(
            f"Case {number} {'ruling amended' if amending else 'is closed'}. @{self.names.prosecutor} case {number}: "
            f"record {'the amended' if amending else 'this'} ruling in docket-decisions, "
            f"with a rule for each sustained charge."
        ))
        return actions

    # -- the prosecutor -----------------------------------------------

    def _from_prosecutor(self, body: str) -> list[Action]:
        case = self._case_in(body)
        if case is None:
            return []
        charges = split_charges(body)

        if case.stage == Stage.PROSECUTION and charges:
            return self._charge_sheet(case, charges)

        if case.stage == Stage.VERIFY and (charges or "resolved" in body.lower()):
            still_open = OPEN_CHARGE_RE.search(body) and not NO_NEW_CHARGES_RE.search(body)
            if still_open and case.round < MAX_ROUNDS:
                case.round += 1
                case.stage = Stage.DEFENSE
                return [Post(
                    f"Case {case.number}, round {case.round} of {MAX_ROUNDS}. "
                    f"@{self.names.defense} case {case.number}: the prosecutor says charges are still open. "
                    f"Answer them by number."
                )]
            case.stage = Stage.RULING
            return [self._ask_for_ruling(case, round_limit=bool(still_open))]
        return []

    def _charge_sheet(self, case: Case, charges: dict[int, str]) -> list[Action]:
        case.charges = sorted(charges)
        case.dismissed = [n for n, text in charges.items() if not has_exhibit(text)]
        live = [n for n in case.charges if n not in case.dismissed]
        lines = []
        for n in case.dismissed:
            lines.append(f"Charge {n} is dismissed: no exhibit.")
        if not live:
            case.stage = Stage.RULING
            lines.append("No charges remain.")
            return [Post(" ".join(lines) + " " + self._ask_for_ruling(case).content)]
        case.stage = Stage.DEFENSE
        live_list = ", ".join(str(n) for n in live)
        lines.append(
            f"@{self.names.defense} case {case.number}: answer charge{'s' if len(live) > 1 else ''} {live_list} "
            f"by number. Rebut with evidence, or fix it on the PR branch, push, and show the passing test run."
        )
        return [Post(" ".join(lines))]

    # -- the defense --------------------------------------------------

    def _from_defense(self, body: str) -> list[Action]:
        case = self._case_in(body)
        if case is None or case.stage != Stage.DEFENSE:
            return []
        if not split_charges(body):
            return []  # "On it..." and other progress notes
        case.stage = Stage.VERIFY
        return [Post(
            f"@{self.names.prosecutor} case {case.number}: re-run your exhibits against the defense's answer "
            f"and say, for each charge, whether it is resolved."
        )]

    # -- helpers ------------------------------------------------------

    def _ask_for_ruling(self, case: Case, round_limit: bool = False) -> Post:
        live = [n for n in case.charges if n not in case.dismissed]
        extra = f" The round limit ({MAX_ROUNDS}) is reached." if round_limit else ""
        if live:
            example = ", ".join(f"Charge {n} sustained" for n in live)
            word = "charge" if len(live) == 1 else "charges"
            what = f"rule on {word} {_join(live)}: reply `Ruling, case {case.number}: {example}` (or overruled)"
        else:
            what = f"reply `Ruling, case {case.number}:`"
        return Post(
            f"Case {case.number} is ready for a ruling.{extra} @{self.names.judge}, {what}, "
            f"and say whether the PR may be merged."
        )

    def _case_in(self, body: str) -> Case | None:
        m = CASE_RE.search(body)
        if m:
            return self.cases.get(int(m.group(1)))
        open_cases = [c for c in self.cases.values() if c.stage != Stage.CLOSED]
        return open_cases[0] if len(open_cases) == 1 else None

    def _new_case(self, number: int | None = None, pr: int | None = None, branch: str | None = None) -> Case:
        number = number if number is not None else self.next_case
        self.next_case = max(self.next_case, number + 1)
        case = Case(number=number, pr=pr, branch=branch)
        self.cases[number] = case
        return case
