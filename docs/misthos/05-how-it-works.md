# How it works

This is the document to read if you want the product in your head in ten minutes. Everything here is at the level of who decides what and what the money does. Implementation detail is deliberately absent.

## The cast

| Actor | What they do | What they never do |
| --- | --- | --- |
| Publisher | Names the price and commits to it, approves it, merges the work | Review code line by line; the platform does that |
| Contributor | Claims an issue, writes the patch, responds to review | Merge their own work |
| Pricing agent | Reads the issue, shows what comparable work settled for, writes the reason | Name the price, or move money |
| Review agent | Reads the submission against the acceptance criteria and the test suite, issues the verdict | Merge, or release payment |
| Settlement layer | Holds committed funds, releases on acceptance, refunds on timeout | Decide anything |

Review means one thing here: the platform assessing the contributor's submitted pull request against the published acceptance criteria and the project's own test suite, and issuing a verdict. It is review of the submission, not of the issue before it is funded and not of the repository in general. No human reads the diff as a required step.

## System context

```mermaid
flowchart TB
    subgraph People["People"]
        PUB["Publisher<br/>company or maintainer"]
        CON["Contributor"]
    end

    subgraph Platform["Misthos"]
        PRICE["Pricing agent<br/>advice, not a decision"]
        TRIAGE["Triage and matching"]
        REVIEW["Review agent"]
        LEDGER["Commitments and audit log"]
    end

    GH["GitHub<br/>issues, pull requests, checks"]
    ARC["Arc<br/>USDC settlement"]
    WALLET["Wallets<br/>Circle agent wallets"]

    PUB -->|"issue plus price"| Platform
    CON -->|"pull request"| GH
    Platform --> GH
    PRICE --> TRIAGE
    TRIAGE --> REVIEW
    REVIEW --> LEDGER
    LEDGER --> ARC
    ARC --> WALLET
```

## The lifecycle of a funded issue

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> AwaitingApproval: publisher names the price and commits to it
    AwaitingApproval --> AwaitingApproval: publisher moves the price, while unfunded
    AwaitingApproval --> Funded: the criteria and the price are approved, funds committed
    Funded --> Claimed: contributor takes an exclusive time-boxed claim
    Claimed --> Funded: claim expires without a pull request
    Claimed --> InReview: contributor opens a pull request
    InReview --> Rework: the verdict asks for changes
    Rework --> InReview: contributor pushes an update
    InReview --> Accepted: the verdict passes
    InReview --> Rejected: the verdict rejects with reasons
    Rejected --> Funded: issue returns to the pool
    Rejected --> Accepted: a contributor's dispute is upheld on a second review
    Accepted --> Paid: publisher merges, or the grace period expires
    Accepted --> Rework: publisher declines once, with a reason
    Funded --> Refunded: deadline passes with no accepted work
    Refunded --> [*]
    Paid --> [*]
```

Two states carry most of the design weight. `AwaitingApproval` is the human checkpoint: it is where a person names the price and signs it, and where they can still move it, because no money has moved and nobody has been promised anything. `Accepted` is the window in which the publisher decides whether to merge, or to decline once with a reason, and it is the only other place a person reaches into the flow. Everything between them runs without one.

A refunded issue is not re-listed. Nobody claiming an issue at its price is information, and the person it belongs to is the publisher: they can publish the issue again at a number they choose. An agent raising the price on their behalf would be guessing at their budget.

## How work is assigned

One mechanism, used for every issue: **the publisher's price, first claim.**

The publisher names a single number and commits the funds. The first contributor to claim the issue holds it exclusively for a bounded window, and loses it if no pull request appears. Contributors do not compete on price, because the price is already published. They compete on being ready to start.

There is no bidding round, no auction, and no negotiation. A price that could move after publication would defeat both checkpoints above: the publisher would be approving a number it might not pay, and the contributor would not know what the work is worth until someone else decided. The publisher can move the number while the issue is unfunded and unclaimed, and no further: a claim is the promise, and it closes the door. The trade-off is that a fixed price cannot adapt to a surprise in the codebase, which is the main reason requirement clarity is part of what the engine reads.

The price is the publisher's. The engine reads the issue and shows what work of a similar shape has settled for, as advice the publisher can check and ignore. The full reasoning is in [06 The fee, and what the engine is for](./06-pricing-engine.md).

## The happy path, step by step

```mermaid
sequenceDiagram
    autonumber
    actor Priya as Publisher
    participant Agent as Misthos agents
    participant GH as GitHub
    actor Amara as Contributor
    participant Escrow as Arc commitment

    Priya->>GH: Opens or selects an issue
    Priya->>Agent: Names the price they will pay
    Agent->>GH: Reads issue, code, test suite, related history
    Agent->>Agent: Scores complexity and effort
    Agent->>Priya: What similar work settled for, with the reasoning
    Priya->>Agent: Approves the criteria, or moves the price
    Priya->>Escrow: Commits the price they named
    Agent->>GH: Publishes acceptance criteria on the issue
    Amara->>Agent: Claims the issue
    Agent->>Amara: Exclusive claim, deadline, relevant files and prior attempts
    Amara->>GH: Opens a pull request
    Agent->>GH: Runs the project's checks and tests
    Agent->>Agent: Issues the verdict against the criteria and the tests
    alt Accepted
        Agent->>Priya: Verdict, findings and test results
        Priya->>GH: Merges
        Escrow->>Amara: Releases payment in USDC
    else Changes requested
        Agent->>Amara: Findings with reasons
        Amara->>GH: Updates the pull request
    end
```

Note the ordering around acceptance. The agent issues the verdict and Priya merges. Neither the agent nor the contributor can release money, and the release needs a verdict on record. The one exception is a publisher who goes quiet for seven days after a passing verdict, which releases anyway rather than strand finished work. That grace is bounded by the escrow's own funding window, because a release after the escrow deadline is refused: a verdict that lands late releases at the deadline rather than seven days into a window that has already closed.

## Who decides what

```mermaid
flowchart LR
    subgraph Agent["Agent decides"]
        A1["How complex the issue is"]
        A2["What comparable work settled for"]
        A3["Which contributors to surface"]
        A4["Whether a submission meets the criteria"]
        A5["What the review should say"]
        A6["When a deadline has passed"]
    end
    subgraph Human["Human decides"]
        H1["What the price is"]
        H2["Whether the work is accepted"]
        H3["Whether to fund the issue at all"]
    end
```

The line is drawn at consequence, not at capability. An agent that gets the price wrong costs the publisher money, and an agent that guesses what a piece of work is worth to a particular company is guessing at the one thing it cannot know. So the person names the number, and the agent tells them what comparable work cost. An agent that gets the review wrong lets a bad patch into a dependency, so a person signs that too. Everything reversible belongs to the agent.

## What the pricing agent actually looks at

Full detail is in [06 The fee, and what the engine is for](./06-pricing-engine.md). The short
version is two inputs, and neither of them is the price.

| Input | What it tells us | Where it comes from |
| --- | --- | --- |
| Task complexity | How much work this is | The issue, the repository, the number of files implicated, whether tests exist, how many prior attempts failed |
| Market context | What comparable work has recently paid | Settled issues on the platform with similar shape |

The price itself comes from the publisher. What the engine supplies is the thing the
publisher is uniquely bad at and the agent is uniquely good at: knowing what this class of
work has cost, in this kind of codebase, recently. That is the input that changes a
publisher's mind, and it is why the comparables are shown before the suggestion rather than
after it.

## The checkpoints, and why each one exists

| Checkpoint | Who holds it | Why it is not automated |
| --- | --- | --- |
| Publishing an issue with a price | Publisher | This is the moment a real obligation is created. Someone has to own it, and it is also the one number the platform cannot supply: what this work is worth to this company. |
| Accepting the work | Publisher's merge | Someone has to accept liability for the code. A verdict is evidence, not a signature. |
| A publisher who goes quiet after a passing verdict | Nobody, after seven days | A finished patch should not be held hostage by an absent publisher. The only release that carries no signature. |
| Releasing funds above a threshold | Publisher's policy | The publisher sets the threshold. Below it, release is automatic. |
| Resolving a dispute | Both parties, then the platform | Disagreements are rare and expensive. A person should handle them. |

The Tameion brief asks the same question in its fourth FAQ answer, and its answer is ours: the limit has to sit somewhere the agent cannot reach. A budget that is enforced in a contract rather than requested in a prompt, a threshold above which a human signs, and a record the agent must produce afterwards.

## The money

```mermaid
flowchart LR
    P["Publisher<br/>commits funds"] --> E["Escrow commitment<br/>on Arc"]
    E -->|"on acceptance"| C["Contributor<br/>fix price less take"]
    E -->|"on deadline"| P2["Publisher<br/>refund"]
    E -->|"on acceptance"| F["Misthos<br/>the 10% fee"]
```

Three things about this flow.

The commitment happens before the contributor starts, so a contributor never works against an empty promise. That is the lesson from Bountysource read forwards: the money must be visibly committed and programmatically released.

Payment is a transfer, not an invoice. Settlement on Arc costs about a cent and completes in under half a second, so a contributor sees the money in the same session as the accept click. The publisher's finance team still gets a record, and generating that record is a product requirement rather than a nicety.

The refund path is automatic and boring. If no acceptable work arrives by the deadline, funds return. The interesting failure is not theft, it is work that is nearly good enough and drags on, which is what the dispute path exists for.

## Failure paths

| Failure | What happens | Why it is designed this way |
| --- | --- | --- |
| Nobody claims the issue | Refund on deadline, and the publisher decides what to do next | A silent empty listing teaches the publisher to stop funding, and raising the price for them would be guessing at their budget |
| Claim expires with no pull request | Issue returns to the pool, claim score affected | Prevents squatting on desirable work |
| Submission is close but not acceptable | Rework loop with specific findings, then a bounded number of rounds | Unbounded review loops cost more than the fix is worth |
| Contributor disputes the verdict | Platform re-reviews against the published criteria, then the publisher decides | The criteria are on record, so the argument is about them rather than about taste |
| Publisher disappears after a passing verdict | Funds release to the contributor seven days later, or at the escrow deadline when that comes first | Otherwise a finished patch is held hostage by someone who stopped paying attention |
| The pull request is good but the buyer merges it without accepting | Payment is triggered by the merge event, not by a separate click | Removes the incentive to take free work |

That last row is worth flagging to any enterprise buyer, because it is the one place where the platform takes a decision out of a human's hands for a good reason. Merging is acceptance. If a publisher merges the patch, the patch was accepted.

## What we read from GitHub, and what we write

We read issues, pull requests, diffs, checks, test results, contributor history in the repository, and the repository's own test and lint configuration. We write issue descriptions with acceptance criteria, labels, comments with review findings, and commit statuses.

We do not host code, we do not mirror repositories, and we do not ask anyone to move their workflow. A contributor works in their own editor and opens a pull request the way they already do.

That is a deliberate constraint. It is also the constraint that makes the product uninteresting to a competitor who wants to own the developer's editor instead.

## What the publisher sees

A small number of screens, in this order.

1. A list of their funded issues with state, price and deadline.
2. The publisher's price, with what comparable work settled for beside it, a confidence level, and the comparable issues behind it. Move the price, or commit it.
3. A submissions view: the verdict, the findings and the test results, with the diff available as evidence. Reading it is optional, not the job.
4. Budget rules: category limits, per-transaction thresholds, and who counts as an approver.

## Where the product is thin

Automated review quality is the weakest part of the design today, and it is now the whole of the design. There is no human reviewer standing behind the agent, so an unreliable verdict is not a wasted review, it is a wrong decision about someone's money. We are treating review quality as the primary technical risk and the go-to-market plan tests it before anything else.

The second thin spot is the comparables history. A price that rests on what similar work settled for is only as good as the number of similar things that have settled, and on the first day that number is zero. The product is designed around this rather than against it: with no history the engine says so, shows its effort estimate and its reasoning, and lets the publisher's number stand. Every settlement makes the next suggestion better, which is the one compounding asset the platform has.
