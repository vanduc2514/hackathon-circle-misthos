# Misthos

Business documentation for a platform that lets a company or an open-source maintainer put a price on a GitHub issue, and pays whoever fixes it.

Written for stakeholders: founders, judges, investors, and the business people who have to decide whether this is worth building. There is no code in here and no database schema. The technical design lives elsewhere.

## The idea in six sentences

An enterprise or a maintainer picks an issue in a GitHub repository and attaches money to it. An AI agent reads the issue, the repository and the buyer's financial position, and proposes a price with a written justification. A contributor picks up the issue, opens a pull request, and an agent reviews the submission against the acceptance criteria and the project's own tests. The publisher merges, and that merge is the acceptance decision. Payment settles in USDC over x402, released when the pull request merges. Because settlement costs about a cent, a small fix is worth transacting at all, which is not true of any payment method a business uses today.

## The 60-second pitch

Open-source work gets done by people who are not paid for it. The issues that never get fixed are not unfixable, they are unfunded. Companies that depend on that code have money and no mechanism, and maintainers have a backlog and no budget.

Misthos is the mechanism. It is a marketplace where the unit of sale is one issue, not one person and not one project. The buyer gets a price, a reviewed patch and an audit trail. The contributor gets paid on acceptance instead of on a 45-day invoice cycle. The maintainer gets a reviewed patch instead of one more unpaid review queue.

The platform is agent-first by design. Agents scope issues, propose prices and review submissions. Humans stay in the loop for the two decisions that carry consequences: publishing an issue with a real price on it, and merging a piece of work as done.

## How to read this set

| Document | What it answers | Read it if you are |
| --- | --- | --- |
| [01 Executive summary](./01-executive-summary.md) | What we are building and why now, on two pages | Anyone with five minutes |
| [02 Problem and vision](./02-problem-and-vision.md) | The problem, who has it, and where this goes in three years | Founders, investors |
| [03 Market research](./03-market-research.md) | Who else tried this, what they charge, what killed the ones that closed | Investors, anyone doing diligence |
| [04 Customers and personas](./04-customers-and-personas.md) | The four people who have to say yes, and the journey each of them walks | Product, design, sales |
| [05 How it works](./05-how-it-works.md) | The end-to-end flow, with diagrams | Everyone. This is the one to read first if you are visual. |
| [06 The pricing engine](./06-pricing-engine.md) | How a number gets attached to an issue, and how much we take | Finance, product |
| [07 Business model and economics](./07-business-model-and-economics.md) | Where revenue comes from and what a customer is worth | Investors, finance |
| [08 Trust, compliance and risk](./08-trust-compliance-and-risk.md) | Custody, disputes, sanctions, and what could go wrong | Legal, risk, enterprise buyers |
| [09 Metrics](./09-metrics-and-success.md) | The one number that tells us it is working, and the ones behind it | Everyone |
| [10 Go to market and roadmap](./10-go-to-market-and-roadmap.md) | How we get the first twenty users, and what we build by 17 October | Founders, marketing |
| [11 Hackathon alignment](./11-hackathon-alignment.md) | How this maps to the Tameion brief, the five RFBs and the judging criteria | Judges, the team |
| [12 Glossary](./12-glossary.md) | Plain-English definitions of the terms we use | Anyone who trips on a word |
| [ARCHITECTURE.md](../ARCHITECTURE.md) | How the system is built: components, contracts, settlement, trust boundaries | Engineers, and anyone doing technical diligence |

If you only read one thing, read [05 How it works](./05-how-it-works.md). The diagrams there explain the product faster than the prose does.

## Status of the thinking

| Area | State |
| --- | --- |
| Problem and buyer | Researched, with the main assumption still untested |
| Competitors | Researched against live sources, see the sources table |
| Product flow | Decided at the level of who decides what |
| Assignment model | Decided. Fixed price set before publication, first claim takes the work. No bidding, no auction, no negotiation. |
| Pricing engine | Designed, with the financial-data input still to be proven |
| Revenue model | Proposed, not validated with a paying customer |
| Regulatory position | Understood well enough to know it is not a blocker, not engineered |
| Traction | One design partner conversation, nothing more. Be honest about this in any pitch. |

## A note on evidence

Claims in these documents are labelled. Where we read something on a primary source, it says so and the link is in the market research sources table. Where we are reasoning from incomplete evidence, it says inferred. We have deliberately left unverifiable numbers out rather than filling the gaps, because a business plan built on invented statistics falls apart in the second meeting.

The two things we most need to verify are the willingness of a mid-sized company to buy a small fix on an unfamiliar payment rail, and the current legal position on who is legally a software manufacturer under the Cyber Resilience Act once the final guidance settles. Both are flagged where they come up.
