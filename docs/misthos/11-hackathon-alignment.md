# Hackathon alignment

How Misthos maps to the Tameion Agents Hackathon brief, and what we still owe the submission.

Everything in this document comes from the event page at https://tameion.thecanteenapp.com/ as read on 5 October 2026.

## What the event asks for

Tameion is hosted by Canteen with Circle, and it runs from 27 September to 17 October 2026. The frame is AI agents that manage a business's money, settled on Arc in USDC. The organisers point at Circle's Agent Stack as the toolkit and give access to a Canteen-hosted Arc testnet through their CLI.

Their short version, from the FAQ: build whatever you want, as long as it runs on Arc, you actually care about it, and a real business is already using it.

Three criteria in the organisers' own priority order:

1. It runs on Arc, with payments actually flowing in USDC, testnet or mainnet, on the Circle Agent Stack.
2. It is something the team intends to keep building after the three weeks.
3. It is already in front of a business that uses it, solving a problem they actually have.

## How we map to the judging rubric

| Criterion | Weight | Our position | Evidence we will submit |
| --- | --- | --- | --- |
| Agentic sophistication | 30% | Strong. The agent prices work, explains its reasoning, triages submissions and drafts verdicts. Two human checkpoints are deliberate and named. | A recorded run: issue in, price and reasoning out, pull request in, review out. Plus the written rationale for the two checkpoints that stay human. |
| Traction | 30% | Weakest area, and honestly so. One design partner and one funded issue is the realistic outcome in twelve days. | Name the business. Show the funded issue. Show the USDC settled, labelled testnet. |
| Circle tool usage | 20% | Strong if we use the stack rather than one primitive. | Agent wallets for both sides, escrowed commitment on Arc, x402 settlement, App Kit for any cross-chain move, USYC considered for idle escrow. |
| Innovation | 20% | Strong on two counts: pricing work from the buyer's own financial position, and paying the reviewer as well as the contributor. | The pricing brief itself is the artifact. So is the review fee. |

The rubric is explicit that judges have the final say and that the best projects tend to break the rules. Our reading is that the two 30 percent items are where we have to be strong, and traction is the one we can only partly control in twelve days.

## Which requests for builders this touches

The brief offers five RFBs and says outright that they are prompts rather than tracks, and that a build crossing several of them is closer to a real finance function. That is what this is.

| RFB | Fit | How Misthos expresses it |
| --- | --- | --- |
| 01 Intelligent business treasury | Partial | USYC on funds committed but not yet released, so escrowed money earns while it waits. Budget and cash context informs the price. Not a treasury product, and we should say so rather than overclaim. |
| 02 AP/AR automation | Strong | A funded issue is a payable with a price and a schedule. The acceptance criteria plus the test suite are the three-way match. Duplicate detection maps to duplicate or conflicting submissions. |
| 03 Contractor and vendor network | Strong | Milestone escrow with release-or-refund, reputation built from delivery rather than testimonials, screening before the first payment clears, rate informed by what the market actually paid. The brief points at `circlefin/arc-escrow` and so do we. |
| 04 Autonomous business operator | Strong | An agent wallet holding USDC, budgets and thresholds enforced in the contract rather than the prompt, a decision log with reasons attached, and escalation to a human only when a policy threshold is hit. The brief asks for one complete workflow run start to finish, and this is ours: issue published, price set, work claimed, deliverable reviewed, payment released, record written. |
| 05 Compliance intelligence | Strong | Continuous screening rather than a gate at onboarding, risk-tiered limits that adjust with exposure, and reporting a buyer can hand to an auditor. Circle's own Agent Marketplace already screens every seller payout wallet, which we inherit rather than rebuild. |

### The RFB 04 demonstration requirement

The brief asks for at least one complete business workflow the agent runs end to end. Ours, expressed in their vocabulary:

> Receive a funding commitment in USDC, assess it against the publisher's declared budget, publish the issue with acceptance criteria and a price, match a contributor, review the submitted pull request against the project's own tests, record the decision with its reasoning, release the payment on acceptance, and escalate to a human only at the price-approval and final-acceptance thresholds.

Every step of that is a real state transition in the product, not a narrative.

### What we deliberately left out

Two scoping decisions are worth stating, because a reviewer will otherwise assume they are missing rather than excluded.

**No bidding.** An issue carries one fixed price set before publication, and the work goes to the first claim. A competing-bid model was considered and dropped: it rewards the least careful bidder, and it defers the publisher's budget decision to whenever the bids close. None of the five RFBs require price discovery, and the brief's own framing of delegated authority assumes a known amount rather than a contested one.

**No multi-platform integration.** GitHub only, as the brief's problem statements assume. A second forge would multiply the integration surface without changing anything a judge can see in three weeks.

## The design question the brief raises

The fourth FAQ answer is the one that matters most for us, and it is worth quoting the shape of it: an agent that asks permission for every payment is a form with extra steps, and an agent that can move the entire treasury on its own judgement is not something anyone will run. Their suggested answers are a contract that enforces the budget, a threshold above which a human signs, and a complete record the agent must produce afterwards.

We do all three.

The budget lives in the escrow contract rather than in a prompt, so the agent cannot be talked past it. Two thresholds require a human signature: approving the price and accepting the work. And the agent writes a signed decision record for every action, which is the artifact that makes delegated authority defensible in the first place.

Worth noting that the brief's own prior-art section frames this in the same terms, describing the Athenian *euthyna* audit as the invention to beat, and suggesting an agent that writes a record a reviewer can replay. That is the shape of our decision log.

## Prior art from the brief that we build on

The brief's prior-art section pairs historical financial inventions with what an agent could do today. Two of them are directly load-bearing for us.

Item 03, the *symbolon*, is the split tally whose halves only prove the agreement when they fit together. The brief observes that accounts-payable clerks still perform that match by hand, and that no tool has the payment at the end. Our acceptance flow is that match with the payment attached.

Item 05, the *misthos* or daily wage, is the one we took the name from. Athens paid a juror, a rower or a builder on the Acropolis the day they worked, because nobody working for two obols could float a month of credit to the state. Net-30 exists because payments used to be expensive, not because anyone prefers it. At a cent a transaction, paying on delivery is finally a choice rather than a constraint.

That is the whole product in one paragraph, and it is the paragraph to use in the video.

## Stack we use, and why

| Primitive | Use in Misthos |
| --- | --- |
| Arc | The settlement chain. Cheap enough that a $40 issue is viable. |
| USDC and EURC | Contributor payouts and publisher funding. EURC for European publishers and contributors. |
| Circle Agent Wallets | One wallet per publisher organisation, one per contributor, with spending controls |
| Circle CLI | Agent access to wallets and the Circle suite from a command interface |
| x402 | The settlement path for a per-issue payment |
| Contracts | Budget rules, approval thresholds, and milestone escrow with release-or-refund |
| Gateway | Unified USDC balance across chains for publishers that hold funds in more than one place |
| CCTP | Paying a contributor who settles on a different chain |
| USYC | Idle committed funds earning while they wait for acceptance. Optional, and a good story. |
| Paymaster | Letting a contributor receive value without holding a gas token |

The brief's own framing of the stack is that these are the primitives suited to an agent holding and disbursing a company's money, and that builders should use what they need. Our list is short on purpose. Wallets, contracts and x402 are load-bearing; Gateway, CCTP and USYC are depth.

## Submission checklist

| Requirement | Status | Owner |
| --- | --- | --- |
| Public GitHub repository | Not yet | Engineering |
| Video demo under three minutes, on Loom, YouTube or Vimeo | Not yet | Founder |
| Live product link, optional but strongly encouraged | Not yet | Engineering |
| Runs on Arc | In progress | Engineering |
| Payments flowing in USDC | In progress | Engineering |
| Built on the Circle Agent Stack | In progress | Engineering |
| Extraction of the traction questions answered honestly | Not yet | Founder |
| Submitted through the project form | Not yet | Founder |
| Submitted at least once before the final day | Not yet | Founder |

The organiser's advice is to submit early and often. Submissions close on 17 October at 11:59 PM Eastern, there is no live demo day, and judging is asynchronous.

## The traction answers we owe

The brief asks specific questions and it is worth answering them in the same shape.

| Question | Answer |
| --- | --- |
| How many businesses have you onboarded? | One named publisher, at the time of writing, with a second in conversation. |
| How much value has the agent moved? | The total USDC settled, reported separately as testnet or mainnet. Do not blur this. |
| What problems are you solving for them? | One worked case: the repository, the issue, the price, the merged pull request, the payout, and what would have happened without it. |

The brief is explicit that on a testnet, genuine usage still counts, and that real customers transacting in real USDC on mainnet count more. The honest submission reports which one it is.

## Logistics

| Item | Detail |
| --- | --- |
| Event window | 27 September to 17 October 2026 |
| Submission deadline | 17 October, 11:59 PM Eastern |
| Project form | https://forms.gle/BBWrdfuircrKiG2i6 |
| Canteen Discord | https://discord.gg/bDaEfsSqc8 |
| Arc builder Discord | https://discord.com/invite/buildonarc, mention Canteen and Tameion during onboarding |
| Luma registration | https://luma.com/ivroypr5, priority access passphrase is published on the event page |
| Arc CLI | `uv tool install git+https://github.com/the-canteen-dev/ARC-cli` |
| Circle CLI | `npm install -g @circle-fin/cli`, requires Node v20.18.2 or later |
| Testnet funds | The event points at TestMint for up to $10k in testnet USDC via x402 |

Required reading the organisers flag: Agents and Ledgers in 2026, at https://thecanteenapp.com/analysis/2026/09/12/agents-and-ledgers.html. It is the source of the Firefly III caveats in our pricing document and it is worth an hour before anyone writes accounting logic.

## Prizes, for reference

$40,000 total across two tiers. Grand prizes: $10,000 for first, $7,500 for second, and three awards of $5,000 for third. Another 10 to 12 standout teams share $7,500 in roughly equal parts.

The brief also commits to follow-on funding, grant support and partnership introductions for teams that keep building. Given the state of the product and the state of our traction, the standout-team outcome is the realistic target and the honest one.

## What we should say in the video

Three minutes, in this order, and nothing else.

The problem in thirty seconds, using one real issue from one real repository. The flow in ninety seconds, showing the agent pricing it and reviewing a submission, with the two human checkpoints visible on screen. The traction in thirty seconds, naming the business and showing the settled payment. The closing thirty seconds on why we keep building this after the deadline, because the brief says it is looking for the small handful of teams it will still be backing a year from now.
