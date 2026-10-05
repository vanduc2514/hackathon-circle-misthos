# Glossary

Plain definitions of the terms used across these documents, in the order you are likely to meet them.

## Product terms

**Funded issue.** A GitHub issue that has money committed against it. Distinguished from an ordinary issue by having a price, acceptance criteria and a deadline attached.

**Price band.** The range the pricing agent proposes rather than a single number. A band invites the publisher to choose a position; a single figure invites an argument about whether it is exactly right.

**Acceptance criteria.** The written conditions a submission has to meet. Drafted by the agent from the issue and the project's test suite, edited and approved by the publisher, then shown to contributors before they start work.

**Claim.** A contributor's exclusive, time-boxed reservation of a funded issue. Expires if no pull request appears, so desirable work cannot be squatted on. First claim wins, and there is nothing to bid against because the price is already published.

**Fixed price.** The only pricing model the platform uses. The publisher approves one number before publication, commits the funds, and the issue is taken at that number or left alone. There is no bidding, no auction and no negotiation.

**Review fee.** A payment to whoever holds the verdict on a submission. Set at 20 percent of the fix price with a $25 minimum. It exists because maintainers reject bounty platforms over the unpaid review queue.

**Take rate.** The platform's commission on matched work, between 8 and 15 percent depending on the publisher's tier.

**Settled issue.** The unit the business counts. An issue where a pull request merged and the payment released. This is the North Star metric.

**Matched volume.** The total value of work that changed hands through the platform. A financial measure, not a product one.

**Decision record.** The agent's written account of what it did, what it saw, which rule it applied and what it cost. The artifact that makes delegated spending authority defensible.

**Escalation threshold.** A limit above which a human has to act rather than the agent. Enforced in a contract, not requested in a prompt.

**Progressive verification.** Asking for identity documents at the point of payout rather than at signup, so the first session stays frictionless for contributors who never end up earning.

## Payment and chain terms

**USDC.** A digital dollar issued by Circle, redeemable one for one. Intended to hold a stable value rather than to appreciate.

**EURC.** The euro equivalent from the same issuer.

**Arc.** Circle's stablecoin-native blockchain. Settlement takes under half a second and fees are around a cent, paid in USDC rather than in a volatile gas token.

**x402.** An open standard for paying over HTTP, now under the Linux Foundation. A server answers an unpaid request with a 402 Payment Required status plus a price list, and the client pays and retries. No accounts and no API keys, which is what makes it usable by an agent.

**Settlement.** The moment money actually moves. In our flow this happens when work is accepted, not when it is invoiced.

**Escrow.** Funds committed to a contract that releases them when a condition is met, and refunds them when it is not. Chosen specifically so that the platform never holds customer money.

**Commitment.** The publisher's deposit into escrow. Distinct from a charge, because it can come back.

**Circle Agent Wallet.** A wallet designed to be operated by an agent, with spending controls and compliance features built in.

**Agent Stack.** Circle's collection of agent-facing tooling: the CLI, agent wallets, nanopayments, the agent marketplace and the x402 facilitator.

**Nanopayment.** A payment small enough that it would be uneconomic on any traditional rail. Circle's Gateway supports payments down to a millionth of a dollar.

**Gateway.** A unified USDC balance that works across chains, so an agent sees one figure rather than one per chain.

**CCTP.** Circle's mechanism for moving USDC between chains by burning it on one and minting on the other, without a wrapped token in between.

**USYC.** A yield-bearing token representing a tokenised money market fund. The place idle committed funds could sit while they wait for a submission to be accepted.

**Custody.** Holding customer money. We do not do it. See [08 Trust, compliance and risk](./08-trust-compliance-and-risk.md) for why the last company in this category failed by doing it.

## Market and regulatory terms

**RFB.** Request for Builders. The Tameion brief's equivalent of a request for startups. There are five, and they are prompts rather than tracks.

**Cyber Resilience Act.** EU regulation 2024/2847. In force from 10 December 2024, with reporting obligations from 11 September 2026 and the main obligations from 11 December 2027. It puts cybersecurity requirements on anyone placing digital products on the EU market, and it captures private companies that commercially support open-source software.

**Manufacturer.** The CRA's term for the party that owes the obligations. Whether Misthos becomes one by charging maintainers is an open legal question.

**Sanctions screening.** Checking a counterparty against government watchlists. The industry standard is a single check at onboarding, which is exactly the weakness the Tameion brief's fifth RFB is about.

**Point-in-time screening.** A single check at a moment in time. What most businesses do, and what we are arguing against.

**Continuous screening.** Re-checking counterparties on a schedule and after risk events, rather than once.

**KYC.** Know Your Customer. Identity verification obligations that attach when a business pays people.

**Bounty platform.** A site where money is attached to a task and paid on completion. The category Misthos belongs to, and the category that has already produced one bankruptcy and two strategic pivots.

**Take rate compression.** What happens when competitors price at zero and a platform's fee stops being defensible. The reason our Enterprise tier exists.

## Metrics terms

**North Star metric.** The single number that best reflects whether customers are getting value. Ours is settled issues per week.

**Input metric.** Something that moves the North Star and that the team can act on directly.

**Guardrail metric.** A number that does not drive the business but stops the North Star being hit in a way that destroys it. Dispute rate and agent review accuracy are examples.

**GMV.** Gross merchandise value. The total value of transactions flowing through a marketplace, before fees.

**LTV.** Lifetime value. What a customer is worth over their whole relationship.

**CAC.** Customer acquisition cost. What it costs to win one customer.

**Unit economics.** Whether a single transaction makes money. Ours does at $50 and above, and barely at $40.

## Historical terms from the brief

**Tameion.** Ancient Greek for a treasury, and literally for the room the money was kept in. The name the organisers gave the event.

**Misthos.** The daily wage Athens paid a juror, a rower or a builder the day they worked, because nobody earning two obols could float a month of credit. Our product name, and the standard we are holding the payment rail to.

**Symbolon.** An object broken in two, with each party keeping half. The halves fitting back together proved the agreement was real. The ancestor of the three-way match in accounts payable, which is what our acceptance flow approximates.

**Euthyna.** The Athenian audit an official faced at the end of their term, and could not leave the city until it cleared. The brief's suggestion is a continuous version of it, which is what our decision record exists for.
