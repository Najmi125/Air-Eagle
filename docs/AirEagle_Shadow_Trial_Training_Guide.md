# Air Eagle OCC — Shadow Trial & Training Guide

**FTLguard · Air Eagle deployment**

---

## What a shadow trial is

For the duration of this trial, **FTLguard is not in charge of anything.**

You continue to build and publish rosters exactly as you do today. Alongside that, you also do the same work in FTLguard — and then compare. Where the two agree, that is evidence the system can be trusted. Where they disagree, that disagreement is the most valuable thing the trial produces.

Two purposes at once:

1. **You learn the system** while nothing depends on your being right.
2. **The system is tested against your real operation** — not against what someone assumed your operation looks like.

**Nothing you do here affects a real flight.** Assign the wrong pilot, publish the wrong roster, cancel something by mistake — none of it reaches an aircraft. That freedom is the point. Try the things you would not risk later.

---

## The one habit that matters

Whenever FTLguard and you disagree, **write it down before deciding who is right.**

Three things can be true, and only one is a system fault:

| What happened | What it might mean |
|---|---|
| FTLguard refuses something you do routinely | Either a rule is misconfigured — **or your current practice has a compliance issue.** Both matter. |
| FTLguard allows something you would never do | Usually an operator policy nobody has told the system about. |
| FTLguard and you agree | Evidence for reliance. Record it too. |

The second column is why guessing is not good enough. A refusal you dismiss as "the system being wrong" may be the system being right.

---

## Before you start

Tick all of these. Do not begin until every line is true.

- [ ] I can reach the system and it says **Database connected**
- [ ] I know which is the real roster and which is the trial *(the real one is what you do today)*
- [ ] I have the User Guide to hand
- [ ] I have somewhere to record findings — a notebook is fine
- [ ] I know who to report a suspected fault to
- [ ] I understand that **nothing I do here reaches an aircraft**

---

## Stage 1 — The crew

*Skill: keeping the crew record accurate. Everything else depends on this.*

- [ ] Enter every pilot with **every** date filled: date of birth, licence, medical, IR, SIM, route check, SEP, CRM, DG
- [ ] Compare each record against your own source. Note every difference
- [ ] Deliberately leave one date blank on a test pilot. Note what the system does later when you try to assign them
- [ ] Check whether any pilot is missing from the system, or present but no longer flying

**Record:** any date FTLguard has that your records do not, or vice versa. Any pilot whose grade is recorded differently from how you refer to them.

> **Note:** deactivating a pilot cannot currently be undone. Only deactivate someone who has genuinely left.

---

## Stage 2 — Flights

*Skill: the difference between a flight and a duty. This is the concept everything else rests on.*

- [ ] Enter one day's flights exactly as scheduled, in **UTC**
- [ ] For a rotation with two sectors, confirm you can see both as separate flights
- [ ] Enter an aircraft registration and DG flag where they apply
- [ ] Cancel a flight. Confirm it stays visible with status CANCELLED rather than disappearing
- [ ] Record BOTH actual departure and arrival on a flight. Confirm the status becomes **OPERATED** on its own — there is no separate button, because recording both actuals *is* the statement that the flight flew
- [ ] Record only ONE actual time on another flight. Confirm the status does **not** change — one time means in progress, not complete
- [ ] Mark a flight **DISRUPTED** with a reason, then clear it. Note that clearing a flight which has both actual times returns it to OPERATED, not PLANNED

> **Reading the status column — important for reconciliation.**
> `status = 'OPERATED'` does **not** mean "this flight flew", and cannot.
> Status is one column, so OPERATED and DISRUPTED are mutually exclusive:
> a flight you marked disrupted keeps that label even after it flies, because
> "it was disrupted" is recoverable from nothing else, while "it flew" is
> recoverable from the actual times.
>
> So when you reconcile the period — *which flights actually operated?* — the
> honest test is **both actual times being present**, not the status label.
> Status is there to make the list readable and the filter meaningful. Those
> are two different jobs, and a report that confuses them will under-count
> every disrupted flight that still flew.

**Check yourself:** for the nightly pair departing 1900Z — which UTC *date* does it belong to? It departs at midnight local, so the answer is the day before the local date. If that catches you out, it will catch you out again on a report.

**Record:** anything you could not enter, or any field you needed that does not exist.

---

## Stage 3 — Assigning a pair

*Skill: the core daily task, and reading a refusal.*

- [ ] Assign a Commander and Second Pilot to a domestic rotation. **Select both sectors together** — they are one duty
- [ ] Deliberately select only one sector and assign. Note the FDP it calculates, and how it differs
- [ ] Assign the same pair to a rotation the very next day. Note whether it is allowed
- [ ] Do the same on consecutive international days. Note what happens and **why**
- [ ] Assign a pilot whose document you left blank in Stage 1
- [ ] Unassign a pair. Confirm **both** sectors are removed, not one

**Every refusal tells you three things:** which rule, the calculated value, and the limit. Read all three. If it says *"needs 21h 30m rest, only 13h 15m available"* — work out for yourself whether that arithmetic is right.

**Record:** every refusal, with the rule quoted. Then check it against ANO-012 or your own understanding. A refusal you believe is wrong is the single most valuable finding in this trial.

---

## Stage 4 — When things change

*Skill: recording reality, and understanding why it matters.*

- [ ] Take a flight that operated and enter the **actual** times
- [ ] Enter a delay of two or three hours. Note what the system says
- [ ] Look specifically for a **downstream warning** — does it tell you a *later* duty is now affected?
- [ ] Take a pilot with a future duty. Assign them something new that consumes their rest. Note the swap alert and the crew it suggests

**This is the stage that finds problems you would otherwise meet tomorrow.** A delay does not just move a flight — it lengthens the duty and increases the rest required afterwards.

**Record:** whether the downstream warning matched what you would have spotted yourself, and whether it caught anything you would have missed.

---

## Stage 5 — The recurring schedule

*Skill: setting the schedule up once instead of entering it repeatedly.*

- [ ] Create a template for the domestic pair — flight numbers, airports, times, days of week
- [ ] Create a template for the international rotation
- [ ] Expand one week into drafts
- [ ] Look at each draft. Confirm the routes and times are right **before** approving
- [ ] Approve them. Confirm real flights appear in Flight Log
- [ ] Reject one with a reason. Confirm it does not become a flight
- [ ] Run the expansion again. Confirm it does not duplicate anything

**Record:** anything about your real schedule the template could not express — a rotation that varies by day, a seasonal difference, anything.

---

## Stage 6 — Generating a roster

*Skill: reading a generated roster critically rather than accepting it.*

- [ ] Approve two weeks of rotations
- [ ] Run Generate
- [ ] **Read the uncovered seats first.** For each, read the reason and decide whether you agree
- [ ] Look at the duty counts per pilot. Is the spread reasonable?
- [ ] Compare the generated roster against the one **you** built for the same period
- [ ] Where they differ, work out why. Note every difference
- [ ] Reject one proposed assignment by unassigning it on the Roster page
- [ ] Publish. Confirm the rejected one was skipped and the rest published
- [ ] Run Generate again over the same window. Confirm it changes nothing already assigned

**This is the heart of the trial.** A generated roster that matches yours is strong evidence. One that differs is a question worth answering — and the answer is sometimes that the generator found something you had missed.

**Record:** every difference between the generated roster and yours, and the reason for each.

---

## Stage 7 — Ad-hoc and disruption

*Skill: the unplanned work, which is where an OCC actually earns its keep.*

- [ ] Enter a charter through Control Room — flight and crew in one action
- [ ] Try one you know is illegal. Confirm **no flight is created either**
- [ ] Take a real disruption from the period — sickness, AOG, a diversion — and work it through the system
- [ ] Note whether FTLguard reached the same answer you did on the day

**Record:** what the system could not represent. If a real disruption had a dimension the system has no way to record, that is a genuine gap and worth stating plainly.

---

## Stage 8 — Reports

*Skill: getting answers without asking anyone.*

- [ ] Ask the Assistant for one pilot's duties over a date range
- [ ] Ask for document expiry in the next 30 days. Check it against your own records
- [ ] Ask for duty hours per pilot over 28 days
- [ ] Ask for flights with missing crew
- [ ] Ask a regulation question — *"what does D21.1 say about minimum rest"*
- [ ] Ask a question it should refuse — *"who can fly tonight"*. Confirm it declines rather than guessing
- [ ] Download one report as Excel. Confirm the figures match what is on screen

**Always read the interpretation line above the table** — it shows what the system understood. A report about the wrong month looks perfectly correct.

**Record:** any question you wanted to ask that it could not answer.

---

## Exit criteria

The trial is complete when **all** of these are true. Length is set by coverage, not by calendar.

- [ ] At least one **complete roster cycle** covered end to end
- [ ] Both rotation types exercised, including consecutive-day cases
- [ ] At least one **real disruption** worked through
- [ ] At least one document expiry or crew change handled
- [ ] Actual times entered for a meaningful number of flights, including delays
- [ ] A generated roster compared against a manually built one for the same period
- [ ] Every disagreement recorded, and each one resolved as: system fault / configuration issue / missing policy / data problem / **or a genuine finding about current practice**
- [ ] Every open item either fixed, or accepted in writing with its consequence understood

---

## What must be in place before go-live

Separate from the trial itself. All required.

- [ ] **Login**, with every action attributable to a named person
- [ ] **Backups** running, and a restore tested at least once
- [ ] **Paid hosting tier**, so the system does not sleep
- [ ] **Private access** — not publicly reachable
- [ ] Named person responsible for crew data accuracy
- [ ] Agreed fallback if the system is unavailable
- [ ] Known gaps documented and accepted in writing

---

## Recording a finding

Keep it short. Six lines is enough:

```
Date:
What I was doing:
What I expected:
What happened:
Which is right, and why I think so:
Reproducible?  yes / no / not sure
```

**"Not sure" is a valid answer.** A finding you cannot explain is still worth recording — several of the most useful problems found in this system began as something that just looked odd.
