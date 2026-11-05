"""The court's rules, tested against messages shaped like the real case 1 and case 2 transcripts."""
from clerk.court import Court, Names, Post, Stage, Verdict, split_charges

N = Names(repo_path=r"C:\repo")

CHARGES_CASE_2 = """Case 2 · PR #2 (pr-2-order-search) — 2 charges. Exhibits were run on the PR branch at ff04bb3.
**Charge 1:** `search_orders` skips the first page of results. It uses `start = page * per_page`, the same mistake that was sustained in case 1, charge 1. The rule from that ruling: page numbers start at 1.
Exhibit (25 matching orders):
```
python -c "..."
page=1: [11, 12, 13]
```
**Charge 2:** the PR's only test passes without finding anything.
Exhibit:
```
python -c "..."
PR test input, page=1: []
```
docket-defense, your turn."""

DEFENSE_CASE_2 = """Case 2 · defense. I concede both charges.
Charge 1 (search skips the first page): conceded and fixed (b2e0893).
Charge 2 (the test passes without finding anything): conceded and fixed (b2e0893).
```
15 passed in 0.12s
```"""

VERIFY_RESOLVED = """Case 2 · re-run on the pushed fix. Both charges are resolved. I have no new charges against the fix.
Charge 1: resolved.
Charge 2: resolved."""


def texts(actions):
    return [a.content for a in actions if isinstance(a, Post)]


def opened(court, pr=2, branch="pr-2-order-search"):
    return court.open_case_for_pr(pr, branch)


def test_new_pr_opens_a_case_and_addresses_the_prosecutor():
    court = Court(N, next_case=3)
    [post] = opened(court)
    assert "case 3" in post.content and "@docket-prosecutor" in post.content and "PR #2" in post.content
    assert r"C:\repo" in post.content
    assert court.cases[3].stage == Stage.PROSECUTION


def test_same_pr_is_not_opened_twice():
    court = Court(N)
    opened(court)
    assert opened(court) == []


def test_citation_of_an_earlier_charge_is_not_a_new_charge_heading():
    parts = split_charges(CHARGES_CASE_2.replace("Case 2", "Case 1"))
    assert sorted(parts) == [1, 2]
    assert "page=1: [11" in parts[1]  # charge 1 keeps its exhibit despite citing "case 1, charge 1"


def test_full_trial_hands_turns_and_asks_the_judge():
    court = Court(N, next_case=2)
    opened(court)
    # Progress notes don't move the case.
    assert court.on_message("docket-prosecutor", "On it: reviewing PR #2 for case 2.") == []

    [handoff] = texts(court.on_message("docket-prosecutor", CHARGES_CASE_2))
    assert "@docket-defense case 2" in handoff and "charges 1, 2" in handoff
    assert court.cases[2].stage == Stage.DEFENSE

    assert court.on_message("docket-defense", "On it: case 2. Reading the charges.") == []
    [verify] = texts(court.on_message("docket-defense", DEFENSE_CASE_2))
    assert "@docket-prosecutor case 2" in verify and "re-run" in verify

    [ask] = texts(court.on_message("docket-prosecutor", VERIFY_RESOLVED))
    assert "@user" in ask and "Ruling, case 2" in ask
    assert court.cases[2].stage == Stage.RULING


def test_charge_without_exhibit_is_dismissed():
    court = Court(N, next_case=4)
    opened(court, pr=4, branch="b")
    sheet = "Case 4 · 2 charges.\nCharge 1: bad naming, I just don't like it.\nCharge 2: crash.\n```\nTraceback ...\n```"
    [post] = texts(court.on_message("docket-prosecutor", sheet))
    assert "Charge 1 is dismissed: no exhibit." in post
    assert "charge 2 by number" in post
    assert court.cases[4].dismissed == [1]


def test_all_charges_dismissed_goes_straight_to_the_judge():
    court = Court(N, next_case=5)
    opened(court, pr=5, branch="b")
    [post] = texts(court.on_message("docket-prosecutor", "Case 5.\nCharge 1: style.\nCharge 2: taste."))
    assert "No charges remain" in post and court.cases[5].stage == Stage.RULING


def test_open_charges_after_verify_start_another_round_then_stop_at_three():
    court = Court(N, next_case=6)
    opened(court, pr=6, branch="b")
    court.on_message("docket-prosecutor", "Case 6\nCharge 1: x\n```\nfail\n```")
    for expected_round in (2, 3):
        court.on_message("docket-defense", "Case 6 · defense.\nCharge 1: fixed.")
        [post] = texts(court.on_message("docket-prosecutor", "Case 6 re-run.\nCharge 1: not resolved."))
        assert f"round {expected_round} of 3" in post
    court.on_message("docket-defense", "Case 6 · defense.\nCharge 1: fixed again.")
    [post] = texts(court.on_message("docket-prosecutor", "Case 6 re-run.\nCharge 1: still open."))
    assert "round limit" in post and court.cases[6].stage == Stage.RULING


def test_ruling_closes_the_case_and_publishes_a_verdict():
    court = Court(N, next_case=2)
    opened(court)
    court.on_message("docket-prosecutor", CHARGES_CASE_2)
    court.on_message("docket-defense", DEFENSE_CASE_2)
    court.on_message("docket-prosecutor", VERIFY_RESOLVED)
    actions = court.on_message(
        "user", "Ruling, case 2: Charge 1 sustained, fixed, following case 1. Charge 2 overruled. PR #2 may be merged."
    )
    verdict = next(a for a in actions if isinstance(a, Verdict))
    assert verdict.case.rulings == {1: "sustained", 2: "overruled"}
    assert verdict.case.merge_allowed
    assert "record this ruling in docket-decisions" in texts(actions)[0]
    assert court.cases[2].stage == Stage.CLOSED


def test_only_the_judge_can_rule():
    court = Court(N, next_case=2)
    opened(court)
    assert court.on_message("docket-defense", "Ruling, case 2: Charge 1 overruled. PR #2 may be merged.") == []
    assert court.cases[2].stage == Stage.PROSECUTION


def test_a_case_opened_by_hand_is_tracked():
    court = Court(N, next_case=1)
    court.on_message("user", "@docket-prosecutor case 7: please review PR #7 on branch feat/x in C:\\repo")
    assert court.cases[7].pr == 7 and court.cases[7].branch == "feat/x"
    assert court.next_case == 8


def test_clerk_ignores_its_own_messages():
    court = Court(N)
    opened(court)
    assert court.on_message("docket-clerk", "Ruling, case 1: Charge 1 sustained.") == []


def _to_ruling(court, number=3, sheet=None):
    court.open_case_for_pr(number, "b")
    court.on_message("docket-prosecutor", sheet or (
        f"Case {number}.\nCharge 1: divides by zero.\n```\nZeroDivisionError\n```\n"
        f"Charge 2: no test, as in case 2, charge 2.\n```\ngrep: no test found\n```"))
    court.on_message("docket-defense", f"Case {number} · defense.\nCharge 1: fixed.\nCharge 2: fixed.")
    return texts(court.on_message("docket-prosecutor", f"Case {number} re-run. Charge 1: resolved. Charge 2: resolved."))


def test_ask_for_ruling_lists_every_open_charge():
    court = Court(N, next_case=3)
    [ask] = _to_ruling(court)
    assert "charges 1 and 2" in ask and "Charge 1 sustained, Charge 2 sustained" in ask


def test_ruling_that_skips_a_charge_keeps_the_case_open():
    court = Court(N, next_case=3)
    _to_ruling(court)
    actions = court.on_message("user", "Ruling, case 3: Charge 1 sustained, fixed. PR #3 may be merged.")
    [post] = texts(actions)
    assert "stays open" in post and "charge 2" in post
    assert not any(isinstance(a, Verdict) for a in actions)
    assert court.cases[3].stage == Stage.RULING

    actions = court.on_message("user", "Ruling, case 3: Charge 2 overruled.")
    verdict = next(a for a in actions if isinstance(a, Verdict))
    assert verdict.case.rulings == {1: "sustained", 2: "overruled"} and verdict.case.merge_allowed
    assert not verdict.amended and court.cases[3].stage == Stage.CLOSED


def test_a_closed_case_can_be_amended():
    court = Court(N, next_case=3)
    _to_ruling(court)
    court.on_message("user", "Ruling, case 3: Charge 1 sustained. Charge 2 sustained. PR #3 may be merged.")
    actions = court.on_message("user", "Ruling, case 3: Charge 2 overruled.")
    verdict = next(a for a in actions if isinstance(a, Verdict))
    assert verdict.amended and verdict.case.rulings == {1: "sustained", 2: "overruled"}
    assert "ruling amended" in texts(actions)[0]


def test_judge_can_rule_before_the_clerk_asks():
    court = Court(N, next_case=3)
    court.open_case_for_pr(3, "b")
    court.on_message("docket-prosecutor", "Case 3.\nCharge 1: bug.\n```\nfail\n```")
    actions = court.on_message("user", "Ruling, case 3: Charge 1 overruled. PR #3 may be merged.")
    assert any(isinstance(a, Verdict) for a in actions)
