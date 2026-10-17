# Business model and economics

## The model on one page

| | |
| --- | --- |
| Problem | Companies depend on open-source code they cannot maintain and cannot pay to fix. Maintainers have a backlog and no budget. |
| Segments | Companies of 50 to 2,000 people shipping software built on open source, and the maintainers of the projects they depend on |
| Unique value proposition | Put a price on a GitHub issue and get it fixed, with the payment settled the moment the work is accepted |
| Solution | Publisher-named prices, escrowed funds, automated review, USDC settlement on Arc |
| Channels | GitHub itself, maintainer communities, open-source programme leads, security and compliance budgets |
| Revenue | A flat 10 percent take rate on matched work |
| Cost structure | Agent inference, engineering salaries, compliance and identity verification, community and content |
| Unfair advantage | The settled-price dataset, which only exists once issues clear on the platform |
| Key metrics | Matched volume, issues settled, repeat publisher rate, publisher overturn rate |

## Revenue

### Take rate on matched work

The one revenue line. A flat 10 percent of every settled issue, split between publisher and contributor. It scales with volume and costs nothing to sell once the marketplace works.

The problem with relying on it is in the market research. Algora, the best-funded attempt at open-source bounties, has moved its homepage to recruiting. Whatever the take rate on bounties is, it did not carry a company. Treating this as the primary revenue line repeats their experience.

### Something we are not building yet

Once thousands of issues have settled, we know what a specific class of work costs in a specific codebase. That is a price index for software work, and nobody has one. It could be sold to insurers, to procurement teams benchmarking vendor quotes, or to platforms that want to price their own jobs.

We are not building it, because a data product from a market you do not yet have is a story rather than a business. It is listed here so the team remembers to keep the data clean and the consent explicit, because the option is worth more than the feature would be today.

## Illustrative three-year model

Everything in this table is a projection built from the assumptions below it, not a forecast. Do not put these numbers in front of an investor without rebuilding them.

| | Year 1 | Year 2 | Year 3 |
| --- | --- | --- | --- |
| Active publishers | 40 | 250 | 1,200 |
| Issues settled | 400 | 3,000 | 18,000 |
| Average issue price | $700 | $900 | $1,200 |
| Matched volume, GMV | $280,000 | $2,700,000 | $21,600,000 |
| Take rate | 10% | 10% | 10% |
| Revenue | $28,000 | $270,000 | $2,160,000 |
| Headcount | 3 | 6 | 12 |
| Net | Negative | Roughly breakeven | Positive |

### What has to be true for year three

1. Average issue price rises from $700 to $1,200, which means buyers move from small fixes toward compliance-driven remediation. That is the whole bet.
2. The flat 10 percent take rate holds against competitors pricing at zero.
3. Supply keeps up. 18,000 settled issues in a year means roughly 70 a week, which needs several hundred active contributors.

Any one of these failing changes the shape of the business rather than the size of the number.

## Cost structure

| Cost | Year 1 driver | Notes |
| --- | --- | --- |
| Agent inference | $6.31 per issue reviewed, at published agent minute pricing, with settlement and amortised screening | The largest variable cost, and the figure the platform's own break-even is built from. Falls with model improvements and rises with review depth. |
| Settlement | About $0.01 per transfer on Arc | Effectively free. This is the reason the product exists on this rail. |
| Engineering | 1 to 2 people | Review quality is the product. This is where the money goes. |
| Identity and sanctions screening | Per verified contributor | Deferred to payout, so the cost lands only on contributors who actually earn |
| Compliance and legal | Money transmission analysis, terms, privacy | See [08 Trust, compliance and risk](./08-trust-compliance-and-risk.md) for why custody is avoided |
| Community and content | Maintainer relationships, conference presence | The supply side does not answer cold email |

The structural choice that keeps the cost base small is not holding customer funds. Escrow lives in a contract on Arc rather than in a Misthos bank account, which removes a money-transmission licensing burden and a class of liability that killed the last company in this category.

## Unit economics

### Per issue settled

| | $63 fix (break-even) | $500 fix | $5,000 fix |
| --- | --- | --- | --- |
| Platform revenue at 10% | $6.30 | $50.00 | $500.00 |
| Cost to review, all in | ($6.31) | ($6.31) | ($6.31) |
| Contribution | ($0.01) | $43.69 | $493.69 |
| Margin | -0.2% | 87% | 99% |

The take rate is charged on the price alone, which is the whole of what the publisher pays. A $500 commitment therefore pays the contributor $450 and the platform $50. At 10 percent the fee covers the review cost from about $63 an issue, and the $6.31 figure is agent inference, settlement and amortised screening together. Below that an issue that settles loses money, and the platform accepts the price anyway: the $5 minimum is there to catch a typo rather than to protect the margin. An issue that never settles is refunded in full, fee included, and is not re-listed at a higher price. Everything above $500 is unaffected.

### Per publisher

| | Publisher |
| --- | --- |
| Issues per year | 8 |
| Average issue price | $650 |
| Matched volume | $5,200 |
| Gross revenue per year | $520 |
| Assumed retention | 80% annually |
| Implied lifetime value | About $1,000 |

### Acquisition cost

Founder-led sales into maintainer communities and open-source programme leads, plus content. There is no paid channel that makes sense at this contract value, because the buyer population is small enough to reach directly.

Working estimate is $500 to $1,500 of mostly time cost per paying publisher in year one, which is a payback period of several years at the take rate alone. Treat that as optimistic, because the whole cost sits on the founding team's calendar and stops being real the moment they stop working weekends.

## What we are not doing with money

No contributor-side acquisition spend. Contributors follow funded issues, and paying to acquire them before there is work to claim wastes the budget.

No paid ads. The buyer is a specific person in a specific role, and they are reachable through the maintainer communities and open-source programme networks that already exist.

No incentive tokens. The market research has a decade of bounty platforms that tried to bootstrap both sides with a token, and none of them are operating today.

## Funding

The hackathon prize is $10,000 at the top end, which is a few months of agent inference and nothing else. The realistic path after the event is a pre-seed round sized for eighteen months of a three-person team, plus enough inference budget to run review on every submission without rationing it.

The Tameion brief commits to follow-on funding and grant support for teams that keep building. The take rate is the line to raise against, so the marketplace has to be right about liquidity before either number means anything.

## The assumption that decides everything

A mid-sized company will spend $500 to $2,000 on a fix delivered by an unknown contributor, paid over a payment rail it has never heard of.

If that is true, the rest of this model is arithmetic. If it is false, the take-rate revenue line is zero and the business is a code review subscription with an expensive feature attached. Every experiment in the go-to-market plan exists to test that one question, and it should be answered before another line of product code gets written.
