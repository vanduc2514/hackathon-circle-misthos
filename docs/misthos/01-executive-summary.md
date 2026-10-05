# Executive summary

## The one-paragraph version

Misthos is a marketplace where the unit of sale is a single GitHub issue. A company or a maintainer attaches a price to an issue, an AI agent sets and justifies that price using the issue's complexity and the buyer's own financial position, a contributor submits a pull request, and payment settles in USDC over x402 when the work is accepted. It runs on Arc, Circle's stablecoin-native chain, because settlement there costs about a cent and takes under half a second. That cost structure is what makes small jobs viable: a $40 fix is not worth a bank transfer, but it is worth a cent.

## The problem

Open source is infrastructure that nobody funds. The issues that sit unresolved for years are usually not technically hard. They are unfunded, and the two sides who could fix that never meet. A company that ships a product containing an open-source component has a budget and a legal obligation and no way to direct either at a specific fix. A maintainer has a backlog, no budget, and a review queue that grows every time someone offers free help.

The existing platforms prove the demand exists and then struggle to monetise it. Algora, the best-funded attempt at open-source bounties, now sells recruiting on its homepage. IssueHunt moved to Japanese enterprise security programmes. Bountysource held developers' money, stopped paying, and filed for bankruptcy in November 2023. None of them made the buyer, the price, or the settlement cheap enough for the transaction to become routine.

## What we built

Four pieces, described here at the level of who decides what.

1. Issue publishing with a price on it. A company or maintainer turns an issue into a funded contract with acceptance criteria attached. A human approves it before it goes live.
2. A valuation agent. It reads the issue, the repository, the surrounding code and the buyer's financial context, then proposes a price band with a written reason. The buyer can accept or override.
3. A claim and review pipeline. Contributors claim work, open pull requests, and get an automated first review against the acceptance criteria and the project's test suite. A human makes the final acceptance call.
4. Settlement in USDC. Funds are committed when the issue is published, released when the work is accepted, and refunded on a timeout if nothing acceptable arrives.

Agents do the scoping, pricing, triage and first-pass review. Humans keep the two decisions with consequences: putting a real price on an issue, and declaring work done.

## Why now, in three facts

The regulatory deadline is real. The EU Cyber Resilience Act entered into force on 10 December 2024, reporting obligations apply from 11 September 2026, and the main obligations land on 11 December 2027. The Linux Foundation's reading of the act is that a private company which develops, commercialises or supports open-source software is very likely covered. That turns maintaining the open source you ship into a filing requirement with a budget attached.

The rail got cheap enough. Arc settles in under 500 milliseconds with fees around a cent in USDC, and Circle's Gateway nanopayments reach a millionth of a dollar. Paying a maintainer $20 was economically silly five years ago. It is not now.

The supply of fixes arrived before the market for them. GitHub's 2025 data covers 986 million code pushes, and the cost of producing a candidate fix has fallen far enough that review, not authorship, is the bottleneck. The scarce resource moved. We are building for the new scarcity.

## How we make money

A take rate on matched work, in the range of 10 to 15 percent, split between the publisher and the contributor depending on who brought the demand. Subscription seats for organisations that want budget rules, approval thresholds and reporting are the second line, and the more durable one, because the compliance deadline makes the reporting a requirement rather than a preference.

Settlement costs us roughly a cent per payment, so the gross margin on a $500 fix at a 12 percent take rate is thin in absolute terms and fine in percentage terms. Volume is the whole game, which is why the product has to work for $40 issues as well as $4,000 ones.

## Where we are

The problem and the competitors are researched against live sources. The product flow is decided. The pricing engine is designed but the financial-data input is unproven. There is one design partner conversation and no paying customer.

Two assumptions carry the entire thesis and neither is tested:

That a mid-sized company will buy a $500 fix on a payment rail it has never heard of, rather than asking an employee to fit it in.

That maintainers will accept funded issues at all, given that their stated complaint about bounty platforms is the review burden rather than the money.

We have a plan to test both inside the hackathon window and it is in the go-to-market document.

## What we are asking for

Three weeks of the Tameion program to ship the flow end to end with one real repository and one real budget. After that, introductions to two or three companies with a Cyber Resilience Act obligation on their 2027 calendar, because those are the buyers whose problem has a deadline in it.

---

Read next: [02 Problem and vision](./02-problem-and-vision.md) for the long version, or [05 How it works](./05-how-it-works.md) if you would rather see the flow than read about it.
