# Set up the court room with switch-buddy

Switch Console's assistant, switch-buddy, can create the agents and the room for
you. Replace `<REPO_FOLDER>` with the folder that holds the repo under review
(for example `C:\Users\you\docket\docket-demo-shop`), then send these two
messages to switch-buddy in Switch Console, one at a time.

## Message 1: the agents

```
Create two Claude Code agents that work in <REPO_FOLDER>, and that ask me before every action:

1. docket-prosecutor: It's the prosecutor in Docket, a courtroom-style code review. It reviews a pull request, and for each defect it finds it files a numbered charge backed by a failing test or script it actually ran against the PR branch, with the exact command and output. It doesn't file charges over matters of taste. Before filing, it checks the docket-decisions document and cites any earlier ruling that applies, by case number. After the judge rules, it records the ruling in docket-decisions, with a general rule for each sustained charge.

2. docket-defense: It's the defense counsel in Docket. For each charge against a pull request, it either rebuts it with its own evidence or fixes the code on the PR branch, pushes the fix, and shows the passing test run, including the prosecutor's exhibits re-run against the fix. It raises product decisions with the judge instead of making them itself.
```

## Message 2: the room

```
Create a Mattermost room called docket-court with docket-prosecutor, docket-defense and me in it, and set its instructions to the text below exactly.

<paste the whole of room/court-rules.md here>
```

Then, in the `docket-court` channel, check both agents answer:

```
@docket-prosecutor @docket-defense what is your role in this room?
```

## Optional: start with case law

To give the prosecutor precedent from the first run, send:

```
@docket-prosecutor create a room document called docket-decisions with this content, and keep it as the court's record from now on:

<paste room/docket-decisions.md here>
```

Leave it out for an empty docket; the prosecutor creates the document after the first ruling.
