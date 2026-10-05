# Problem and vision

## The problem, told properly

A payment library used by a few hundred thousand applications has a bug in its currency rounding. The fix is about forty lines and a test. The issue has been open for fourteen months.

The maintainer knows what to do and has no time to do it. The companies shipping the library have engineers who could fix it in an afternoon, but adding a patch to someone else's repository is not on anyone's roadmap, and there is no process for paying for it if it were. So the bug is not fixed, and everyone absorbs the cost quietly.

Now multiply that by every open-source component in a modern stack, and add a deadline. From 11 December 2027, a company that commercialises a product containing open-source software has to demonstrate that it maintains it and handles vulnerabilities in it. The work is not optional. The mechanism to pay for it does not exist.

The result is a market with money on one side, labour on the other, and no instrument in the middle.

### The maintainer's side

A maintainer of a popular project receives more offers of help than they can review. Most of those offers cost them time and return nothing. The unpaid review queue is the actual reason maintainers turn down bounty platforms, and it is the reason a funded issue has to pay for review as well as for the patch. Any product that increases the queue without compensating the queue gets rejected by the people it depends on.

### The company's side

The engineering manager who depends on a critical library has four bad options. Ask an engineer to do it as a favour, which never survives a sprint planning meeting. Hire a contractor, which means procurement, a legal review and a two-month cycle for a two-day job. Fund a security bounty, which pays researchers for finding problems rather than for fixing them. Or wait, and accept the risk. Most companies wait.

### The contributor's side

A developer who wants to fix the rounding bug and get paid has no reliable path. The platforms that exist pay after the buyer is satisfied, with no escrow, or they hold the money in a central pot which has already failed once in this category.

## Jobs to be done

| Who | The job | Today they do it by |
| --- | --- | --- |
| Engineering manager at a company shipping open source | Get a specific defect in a dependency fixed by someone competent, without a procurement cycle | Waiting, or absorbing it into internal backlog |
| Open-source maintainer | Clear a backlog item without spending their own weekend on review | Ignoring the issue |
| Developer or small studio | Turn an evening of work into money without a client relationship | Freelance platforms, which want a relationship rather than a task |
| Compliance owner | Produce evidence that dependency risk is being managed | Spreadsheets, SBOMs, and hope |

## Why the answers that exist do not fit

Bounty platforms pay after acceptance with no escrow, so the contributor carries the risk. Algora moved its business to recruiting, which tells us the take rate on bounties was not enough on its own. IssueHunt moved to enterprise security programmes, where the buyer has a compliance reason and a bigger budget. Bountysource held the money and lost it. Staffing marketplaces sell a person, which is the wrong unit for a forty-line fix.

The common thread is that all of them treat the transaction as a relationship. We treat it as a unit of work with a price, a deliverable and a settlement, because that is the only shape that makes a small job worth doing.

## Vision

### The statement

> Any defect in the open-source code a company depends on should be fundable the same afternoon it is noticed.

### Where that leads in three years

The end state is a market where the price of fixing a specific piece of open-source software is discoverable, the work is verifiable, and the payment clears before anyone has to send an invoice. Companies stop treating open-source maintenance as charity and start treating it as procurement. Maintainers stop paying for review out of their own evenings.

The more interesting version of that future is what happens to the pricing data. Once thousands of issue-level contracts have cleared, we know what work actually costs to deliver in a given codebase with a given test suite. That is a price index for software work, and nobody has one today. It is also the thing a competitor cannot copy by shipping a feature.

### Three vision options we considered

| Option | Statement | Why we did not choose it |
| --- | --- | --- |
| Infrastructure framing | The settlement layer for software work | True but forgettable, and it describes the plumbing rather than the change |
| Charity framing | Make open-source maintenance payable | Accurate and soft, and it puts the buyer in a donor's chair when we need them in a procurement chair |
| Chosen | Any defect in the open-source code a company depends on should be fundable the same afternoon it is noticed | Concrete, has a time bound, and implies the whole product |

## Value proposition

### Who

Engineering and platform leaders at companies of 50 to 2,000 people that ship software containing open-source components, plus the maintainers of the projects those companies depend on. Both are technical, both already live in GitHub, and neither has a budget line for open-source maintenance today.

### Why

They need a specific defect or gap fixed in code they do not own. They cannot simply do it themselves, and the alternatives all take longer than the problem is worth.

### What before

An issue sits open. Someone adds a workaround. The workaround becomes load-bearing. An engineer occasionally promises to upstream the fix and never does.

### How

A price attached to the issue, a deadline, an acceptance test, an escrowed payment that clears on merge, and an agent that has already read the code and the submission before the buyer looks at it. The buyer's total effort is approving a price and clicking accept.

### What after

A merged pull request in a dependency the company ships, closing a gap that has a 2027 compliance deadline attached to it. A contributor paid within a minute of acceptance rather than in 45 days. A maintainer with one fewer unpaid review.

### Alternatives

| Alternative | Why a buyer picks it | Why we win |
| --- | --- | --- |
| Ask an employee to upstream it | Free, and it is one engineer's afternoon | It does not survive contact with a sprint plan. Ours takes ten minutes of manager time. |
| Hire a contractor | Known process, known legal shape | Two months of procurement for a two-day job |
| Fund a security bounty | Familiar, and the vendor already exists | Bounties pay for finding problems. We pay for closing them. |
| Do nothing | Zero effort today | The compliance deadline removes this option for a growing set of buyers |
| Wait for the maintainer | Zero effort | The issue has been open for fourteen months |

## Strategic choices

Three trade-offs we are making deliberately, and the cost of each.

We are agent-first, not agent-assisted. Agents scope, price and review; humans approve and accept. The cost is that we have to be right about automated review quality earlier than a human-in-the-loop product would.

We are GitHub-only. No GitLab, no Bitbucket, no Jira. The cost is a smaller addressable market. The benefit is that the integration is deep, the acceptance criteria can read the project's own tests, and the review agent has one API to understand properly.

We pay for review. Publishers fund a review fee alongside the fix. The cost is that we are adding a line item nobody has asked for. The benefit is that maintainers will actually accept funded issues, and without them there is no marketplace.

## What we are not building

A general freelance marketplace. A recruitment product, which is where Algora went. A security disclosure programme, which is where IssueHunt went. A treasury management product, despite the Arc and Circle treasury tooling being available. An agent that spends money without a human approving the price, because that is the line the Tameion brief itself draws in its fourth FAQ answer.

## The honest risk

The vision assumes companies will buy small fixes as a routine procurement action. Every platform before us has either failed or migrated away from that assumption. We might be wrong that the timing has changed, and if a mid-sized company will not spend $500 this way, no amount of good pricing engine changes the outcome. That specific question is the first thing we test, ahead of anything technical.
