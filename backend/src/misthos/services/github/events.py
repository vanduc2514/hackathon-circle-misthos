"""GitHub events, turned into lifecycle actions.

The lifecycle is event-driven in reality: a contributor opens a pull request, the
project's checks finish, the publisher merges. Each event is mapped here to the one
store action it stands for, and the store decides whether that action is legal; this
module never writes state itself.

- `installation` and `installation_repositories`: the repositories the App is on,
  connected to the publisher whose linked GitHub login installed it.
- `issues` opened or labeled with the `misthos` label, in a connected repository:
  `publish`. The issue is priced and the price, the drafted criteria and the commands
  are posted on it.
- `issues` edited, labeled or unlabeled: `reprice`, a new proposal while the issue
  is unfunded.
- `issue_comment` created with commands, each on its own line:
  - `/misthos approve` from the publisher's linked login approves the criteria and
    the price, which commits the funds;
  - `/misthos criteria` followed by a list approves those criteria instead;
  - `/misthos claim` from a linked contributor takes the claim;
  - `/misthos help` lists the commands.
  Every command line runs, in order, and the first one refused stops the rest, so a
  refused `/misthos criteria` never falls through to the `/misthos approve` after
  it. A line in a code block, a block quote or an HTML comment is not a command.
  GitHub signs the delivery, so the comment's author is who GitHub says it is.
- `pull_request` opened, reopened or ready_for_review: `submit_pull_request`, which
  moves `CLAIMED` to `IN_REVIEW` for the claimant's pull request only.
- `pull_request` synchronize: `submit_pull_request`, `REWORK` back to `IN_REVIEW`.
- `pull_request` closed and merged: `pull_request_closed`. Merge is acceptance, and
  the payout is released.
- `pull_request` closed without a merge: logged. The claim and the deadline decide
  what happens next.
- `check_run` completed: `record_checks`, whether the project's own checks passed.

A pull request belongs to an issue when its body closes it ("Fixes #87"), the way
GitHub itself links the two. With these, an issue goes from a label to a payout with
nothing done outside GitHub but signing in once (#6).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from misthos.config import settings
from misthos.domain.compliance import ComplianceRefusal
from misthos.domain.issue import IllegalTransition, IssueState
from misthos.domain.policy import PolicyRefusal
from misthos.domain.pricing import UnfundableIssue
from misthos.models.records import IssueRecord
from misthos.repositories import StaleIssue
from misthos.schemas import PublishRequest
from misthos.services.chain import ChainRevert
from misthos.services.coordination import Busy
from misthos.services.github.app import pull_request_from
from misthos.services.github.base import GitHubError
from misthos.store import (
    CriteriaNotApproved,
    NotTheSubmission,
    Store,
    UnreadableIssue,
    UntestableCriteria,
)

log = logging.getLogger("misthos.github.events")

HANDLED_EVENTS = {
    "installation": "connect the App's repositories to the publisher who installed it",
    "installation_repositories": "connect or disconnect the repositories added or removed",
    "issues": "price an issue labelled for Misthos; re-price an unfunded one when it changes",
    "issue_comment": "/misthos approve, /misthos criteria, /misthos claim and /misthos help",
    "pull_request": "submit the claimant's pull request for review; a merge releases the payout",
    "check_run": "record whether the project's own checks passed on the submitted commit",
    "ping": "acknowledged",
}

# Matched against a line of the commenter's own prose, with its indentation removed.
_COMMAND = re.compile(r"/misthos\s+([a-z]+)\b", re.IGNORECASE)
_LIST_ITEM = re.compile(r"(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
_FENCE = re.compile(r"(`{3,}|~{3,})(.*)")
_HTML_COMMENT = re.compile(r"<!--.*?-->")
# A comment is a message from a person, not a script: more commands than this in one
# is more likely a paste than an intention, and each one can move money.
MAX_COMMANDS = 5
HELP = (
    "**Misthos commands**, each on its own line in a comment. They run in order, up to "
    f"{MAX_COMMANDS} a comment, and the first one refused stops the rest. A command in a "
    "code block, a quote or an HTML comment is not run.\n\n"
    "- `/misthos approve`: the publisher approves the criteria and the price, which "
    "commits the funds to escrow.\n"
    "- `/misthos criteria` followed by a list: the publisher approves these criteria "
    "instead of the drafted ones.\n"
    "- `/misthos claim`: a contributor takes the exclusive claim.\n\n"
    "Sign in at {url}/account and link your GitHub account first."
)

_CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:([\w.-]+/[\w.-]+))?#(\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Handled:
    handled: bool
    outcome: str
    issue_ids: tuple[str, ...] = ()


def closed_issues(body: str, repo: str) -> list[int]:
    """The issue numbers in this repository that a pull request's body closes."""
    numbers: list[int] = []
    for match in _CLOSES.finditer(body or ""):
        other, number = match.group(1), int(match.group(2))
        if (other is None or other.lower() == repo.lower()) and number not in numbers:
            numbers.append(number)
    return numbers


def dispatch(store: Store, event: str, payload: dict[str, Any]) -> Handled:
    repo = (payload.get("repository") or {}).get("full_name")
    action = payload.get("action", "")
    if event == "ping":
        return Handled(True, "ping acknowledged")
    if event in {"installation", "installation_repositories"}:
        return _installation(store, event, action, payload)
    if not repo:
        return Handled(False, "ignored: the payload names no repository")
    try:
        if event == "issues":
            return _issue(store, repo, action, payload)
        if event == "issue_comment":
            return _comment(store, repo, action, payload)
        if event == "pull_request":
            return _pull_request(store, repo, action, payload)
        if event == "check_run":
            return _check_run(store, repo, action, payload)
    except (KeyError, TypeError, ValueError) as exc:
        return Handled(False, f"ignored: the payload is missing {exc}")
    return Handled(False, f"ignored: {event} is not an event the platform acts on")


def _installation(store: Store, event: str, action: str, payload: dict[str, Any]) -> Handled:
    installation = int(payload["installation"]["id"])
    who = (payload.get("sender") or {}).get("login", "")
    if event == "installation":
        if action in {"deleted", "suspend"}:
            gone = store.disconnect_repositories(installation_id=installation)
            return Handled(True, f"disconnected {len(gone)} repositories")
        if action not in {"created", "new_permissions_accepted", "unsuspend"}:
            return Handled(False, f"ignored: installation {action}")
        added = [r["full_name"] for r in payload.get("repositories") or []]
        removed: list[str] = []
    else:
        added = [r["full_name"] for r in payload.get("repositories_added") or []]
        removed = [r["full_name"] for r in payload.get("repositories_removed") or []]
    if removed:
        store.disconnect_repositories(removed)
    connected = store.connect_repositories(installation, who, added) if added else []
    owner = connected[0].publisher_id if connected else None
    return Handled(
        True,
        f"connected {len(connected)} and disconnected {len(removed)} repositories"
        + (
            f" for {owner}"
            if owner
            else f"; {who} is not a signed-in publisher yet"
            if added
            else ""
        ),
    )


def _labelled_for_us(action: str, payload: dict[str, Any]) -> bool:
    wanted = settings.github_label.lower()
    if action == "labeled":
        return str((payload.get("label") or {}).get("name", "")).lower() == wanted
    labels = payload["issue"].get("labels") or []
    return any(str(label.get("name", "")).lower() == wanted for label in labels)


def _list_issue(store: Store, repo: str, number: int, payload: dict[str, Any]) -> Handled:
    connection = store.connection(repo)
    if connection is None or connection.publisher_id is None:
        if connection is not None:
            _reply(
                store, repo, number,
                f"Misthos is installed here by @{connection.installed_by}, who has not signed "
                f"in as a publisher yet. Sign in at {settings.public_url}/account and link "
                "this GitHub account, then label the issue again.",
            )  # fmt: skip
        return Handled(False, f"ignored: {repo} has no publisher")
    issue = payload["issue"]
    request = PublishRequest(
        repo=repo,
        number=number,
        title=str(issue.get("title") or f"{repo}#{number}"),
        labels=[str(label.get("name")) for label in issue.get("labels") or []],
        publisher_id=connection.publisher_id,
    )
    try:
        rec = store.publish(request)
    except UnfundableIssue as exc:
        _reply(
            store, repo, number, f"**Misthos will not price this as scoped.** {exc.justification}"
        )
        return Handled(True, f"{repo}#{number} is not fundable as scoped")
    except UnreadableIssue as exc:
        return Handled(False, f"ignored: {exc}")
    assert rec.proposal is not None
    p = rec.proposal
    criteria = "\n".join(f"- {c}" for c in rec.acceptance_criteria)
    _reply(
        store, repo, number,
        f"**Priced on Misthos: {p.recommended}**, in a band of {p.band_low} to "
        f"{p.band_high} (confidence {p.confidence}).\n\n{p.justification}\n\n"
        f"Proposed acceptance criteria:\n\n{criteria}\n\n"
        "The publisher funds it by commenting `/misthos approve`, or approves other "
        "criteria with `/misthos criteria` followed by a list. Nothing is committed until "
        f"then. Details: {settings.public_url}/issues/{rec.id}",
    )  # fmt: skip
    return Handled(True, f"{repo}#{number} priced as {rec.id}", (rec.id,))


def _issue(store: Store, repo: str, action: str, payload: dict[str, Any]) -> Handled:
    number = int(payload["issue"]["number"])
    rec = store.find_open(repo, number)
    if rec is None and action in {"opened", "labeled"} and _labelled_for_us(action, payload):
        return _list_issue(store, repo, number, payload)
    if action not in {"edited", "labeled", "unlabeled"}:
        return Handled(False, f"ignored: issues {action}")
    if rec is None:
        return Handled(False, f"ignored: {repo}#{number} is not listed")
    before = rec.proposal.recommended if rec.proposal else None
    try:
        rec = store.reprice(rec.id)
    except IllegalTransition:
        return Handled(False, f"{rec.id} is funded, so its price stands", (rec.id,))
    after = rec.proposal.recommended if rec.proposal else None
    outcome = f"re-priced at {after}" if after != before else "price unchanged"
    return Handled(True, outcome, (rec.id,))


def _pull_request(store: Store, repo: str, action: str, payload: dict[str, Any]) -> Handled:
    pr = pull_request_from(repo, payload["pull_request"])

    if action == "closed":
        rec = store.find_by_pull_request(repo, pr.number)
        if rec is None:
            return Handled(False, f"ignored: #{pr.number} is not a submission")
        rec = store.pull_request_closed(rec.id, pr.number, merged=pr.merged)
        verb = "merged" if pr.merged else "closed"
        return Handled(True, f"#{pr.number} {verb}; {rec.id} is {rec.state.value}", (rec.id,))

    if action not in {"opened", "reopened", "ready_for_review", "synchronize"}:
        return Handled(False, f"ignored: pull_request {action}")
    if pr.draft:
        return Handled(False, f"ignored: #{pr.number} is a draft")

    moved: list[str] = []
    notes: list[str] = []
    for number in closed_issues(pr.body, repo):
        rec = store.find_open(repo, number)
        if rec is None:
            notes.append(f"#{number} is not listed")
            continue
        try:
            rec = store.submit_pull_request(rec.id, pr)
        except (IllegalTransition, NotTheSubmission) as exc:
            notes.append(f"{rec.id}: {exc}")
            continue
        moved.append(rec.id)
    if moved:
        return Handled(True, f"#{pr.number} submitted for {', '.join(moved)}", tuple(moved))
    return Handled(False, "ignored: " + ("; ".join(notes) or "it closes no listed issue"))


def _check_run(store: Store, repo: str, action: str, payload: dict[str, Any]) -> Handled:
    if action != "completed":
        return Handled(False, f"ignored: check_run {action}")
    sha = payload["check_run"]["head_sha"]
    changed = [
        rec.id
        for rec in store.list_issues()
        if rec.repo.lower() == repo.lower()
        and rec.submission is not None
        and rec.submission.head_sha == sha
        and store.record_checks(rec.id, sha)
    ]
    if changed:
        return Handled(True, f"checks recorded on {', '.join(changed)}", tuple(changed))
    return Handled(False, "no submission changed")


# ------------------------------------------------------------------ commands


def _reply(store: Store, repo: str, number: int, body: str) -> None:
    """A comment back on the issue. A failed post never undoes what was done."""
    try:
        store.github.comment(repo, number, body)
    except GitHubError as exc:
        log.warning("could not reply on %s#%s: %s", repo, number, exc)


@dataclass(frozen=True)
class Command:
    """One `/misthos` line of a comment."""

    name: str
    """Lower case, as written after `/misthos`."""
    items: tuple[str, ...] = ()
    """The list items on the lines after it, up to the next command: the criteria a
    `/misthos criteria` approves."""


def commands_in(body: str) -> list[Command]:
    """The commands in a comment, in the order they were written.

    Only the commenter's own prose counts. A line in a fenced or indented code block,
    a block quote or an HTML comment is not a command: that is how a command is shown
    as an example, quoted back by GitHub's "Quote reply" or left in a template, and
    running one of those can commit a publisher's funds.
    """
    found: list[tuple[str, list[str]]] = []
    for line in _prose(body):
        if command := _COMMAND.match(line):
            found.append((command.group(1).lower(), []))
        elif found and (item := _LIST_ITEM.match(line)):
            found[-1][1].append(item.group(1))
    return [Command(name, tuple(items)) for name, items in found]


def _prose(body: str) -> Iterator[str]:
    """The comment's lines that a reader sees as the commenter's own words, unindented.

    Where reading Markdown exactly would take a parser, this reads it the cautious
    way: a fence opens at any indentation, a line indented four columns is code even
    where Markdown would continue a paragraph with it, and the whole line that closes
    an HTML comment is skipped. Erring that way costs a command typed again; erring
    the other way can cost a publisher's money.
    """
    fence: tuple[str, int] | None = None  # the open fence, and how far it was indented
    in_comment = False
    for raw in body.splitlines():
        line = raw.expandtabs(4)
        if fence is not None:
            if _closes(line, *fence):
                fence = None
            continue
        if in_comment:
            in_comment = "-->" not in line
            continue
        line = _HTML_COMMENT.sub("", line)
        if "<!--" in line:
            line, in_comment = line.split("<!--", 1)[0], True
        text = line.lstrip(" ")
        indent = len(line) - len(text)
        if text.startswith(">"):
            continue
        opened = _FENCE.match(text)
        # A backtick fence's info string cannot hold a backtick; ```x``` is inline code.
        if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            fence = (opened.group(1), indent)
            continue
        if indent < 4 and text.strip():
            yield text


def _closes(line: str, mark: str, opened_at: int) -> bool:
    """Whether a line closes the fence `mark` opened: the same character, at least as
    many, nothing else on the line, and not indented as code inside the fence."""
    text = line.lstrip(" ")
    closer = text.rstrip()
    return (
        len(line) - len(text) <= max(3, opened_at)
        and len(closer) >= len(mark)
        and set(closer) == {mark[0]}
    )


@dataclass(frozen=True)
class _Ran:
    ok: bool
    """Whether the command did what it says. One that did not stops the rest."""
    reply: str | None = None
    """What to say back, or None when the platform already says it."""


def _comment(store: Store, repo: str, action: str, payload: dict[str, Any]) -> Handled:
    if action != "created":
        return Handled(False, f"ignored: issue_comment {action}")
    comment, issue = payload["comment"], payload["issue"]
    author = comment.get("user") or {}
    if author.get("type") == "Bot" or (payload.get("sender") or {}).get("type") == "Bot":
        return Handled(False, "ignored: a bot's comment")
    if issue.get("pull_request"):
        return Handled(False, "ignored: a comment on a pull request")
    found = commands_in(str(comment.get("body") or ""))
    if not found:
        return Handled(False, "ignored: not a command")
    login, number = str(author.get("login", "")), int(issue["number"])

    replies: list[str] = []
    steps: list[str] = []
    issue_ids: list[str] = []
    run = found[:MAX_COMMANDS]
    for position, command in enumerate(run):
        # Read afresh for each command, because the one before may have changed the
        # issue: an approve after a criteria must fund those criteria, not the draft.
        rec = store.find_open(repo, number)
        if rec is not None and rec.id not in issue_ids:
            issue_ids.append(rec.id)
        following = run[position + 1].name if position + 1 < len(run) else None
        try:
            ran = _command(store, rec, command, login, following)
        except (Busy, StaleIssue):
            if not steps:
                raise  # nothing has run yet, so the whole delivery can be retried
            # Retrying the delivery now would run the commands before this one again.
            ran = _Ran(
                False,
                f"The issue was busy, so `/misthos {command.name}` was not run. Comment it again.",
            )
        if ran.reply:
            replies.append(ran.reply)
        if ran.ok:
            steps.append(f"/misthos {command.name} ran")
            continue
        steps.append(f"/misthos {command.name} did not run: {(ran.reply or '-').splitlines()[0]}")
        rest = found[position + 1 :]
        if rest:
            replies.append(
                "The rest of this comment was not run: "
                + _and([f"`/misthos {c.name}`" for c in rest]) + "."
            )  # fmt: skip
            steps.append(f"{len(rest)} after it not run")
        break
    else:
        extra = len(found) - MAX_COMMANDS
        if extra > 0:
            replies.append(
                f"Only the first {MAX_COMMANDS} commands in a comment are run, so the other "
                f"{extra} {'was' if extra == 1 else 'were'} not."
            )
            steps.append(f"{extra} over the limit not run")
    if replies:
        _reply(store, repo, number, "\n\n".join(replies))
    return Handled(True, f"from {login}: " + "; ".join(steps), tuple(issue_ids))


def _command(
    store: Store, rec: IssueRecord | None, command: Command, login: str, following: str | None
) -> _Ran:
    """Run one command. `following` names the command after it in the same comment."""
    name = command.name
    if name == "help":
        return _Ran(True, HELP.format(url=settings.public_url))
    if name not in {"approve", "criteria", "claim"}:
        return _Ran(
            False, f"`/misthos {name}` is not a command. Comment `/misthos help` for the list."
        )
    if rec is None:
        return _Ran(
            False,
            f"This issue is not on Misthos. Add the `{settings.github_label}` label to have it "
            "priced.",
        )
    account = store.account_with_login(login)
    if account is None:
        return _Ran(
            False,
            f"@{login}, sign in at {settings.public_url}/account and link this GitHub account "
            "first.",
        )
    try:
        if name == "claim":
            if account.role != "contributor":
                return _Ran(False, f"@{login}, only a contributor can claim an issue.")
            if rec.state is not IssueState.FUNDED:
                return _Ran(
                    False,
                    f"@{login}, this issue is {rec.state.value.lower()}, so it cannot be claimed.",
                )
            store.claim(rec.id, account.party_id)
            return _Ran(
                True,
                f"Claimed by @{login}, who holds it for 72 hours. Open a pull request that says "
                f"`Fixes #{rec.number}`; it is reviewed against the criteria above.",
            )
        if account.party_id != rec.publisher_id:
            return _Ran(False, f"@{login}, only the publisher of this issue can {name} it.")
        if name == "criteria":
            if not command.items:
                return _Ran(
                    False, "List the criteria under `/misthos criteria`, one per line, as `- ...`."
                )
            store.approve_criteria(rec.id, list(command.items), by=login)
            # Say how to fund them, unless the next line of this comment does it.
            then = (
                ""
                if following == "approve"
                else "\n\nComment `/misthos approve` to commit the funds."
            )
            return _Ran(
                True,
                f"Criteria approved by @{login}:\n\n"
                + "\n".join(f"- {i}" for i in command.items) + then,
            )  # fmt: skip
        # approve: the drafted criteria unless others were approved, then the price.
        if rec.criteria_approved_at is None:
            store.approve_criteria(rec.id, rec.acceptance_criteria, by=login)
        store.approve_price(rec.id, by=login)
        return _Ran(True)  # funding posts the criteria and the price on the issue itself
    except UntestableCriteria as exc:
        lines = "\n".join(f"- Criterion {p.index} {p.reason}." for p in exc.problems)
        return _Ran(False, f"These criteria cannot be judged by a reviewer:\n\n{lines}")
    except IllegalTransition:
        return _Ran(
            False,
            f"This issue is {rec.state.value.lower()}, so `/misthos {name}` does nothing now.",
        )
    except (CriteriaNotApproved, ComplianceRefusal, PolicyRefusal) as exc:
        return _Ran(False, f"Refused: {exc}")
    except ChainRevert as exc:
        return _Ran(False, f"The escrow refused the commitment: {exc}")


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"
