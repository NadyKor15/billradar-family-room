# BillRadar Family Room

[![clean clone](https://github.com/NadyKor15/billradar-family-room/actions/workflows/clean-clone.yml/badge.svg?branch=main)](https://github.com/NadyKor15/billradar-family-room/actions/workflows/clean-clone.yml)

Built on [Switch](https://github.com/sandbox-quantum/switch).

For the person who handles a family's hospital bills, and the relatives who sometimes help. That person keeps track of everyone's bills, insurance plans, deadlines and calls to the hospital. When relatives help, letters and photos of bills start going around in chats, and those hold names, account numbers and diagnoses.

Here one agent reads the private papers in that person's own room, and a second agent runs the shared family room. **Switch room membership is what keeps the second agent away from the papers**: when it asks for them, Switch answers 403.

Built for Flip the Switch (track 02, Best Personal or Creative Workflow) on Switch 0.31.0, with the bundled Mattermost and with Telegram. The bills in this repository are fictional; no real medical data appears anywhere.

It runs in two modes. By default neither agent calls a language model: the coordinator is Python rules, and the reader recognises BillRadar's fictional sample packs and replays the engine's recorded results. With `LIVE=1` (see [Live mode](#live-mode)) the reader reads those same fictional packs with the engine's models, and the coordinator asks a model which of the reader's checked answers fits what a relative reports. Files outside those packs reach no model in either mode.

## The story in four steps

1. Nadya handles her family's hospital bills. She sends 10 files of hospital papers to her own room. The reader reports 7 findings: $1,596.53 may come off.
2. The reader passes the case to the coordinator without names or account numbers. The family room gets only the result.
3. In her own room, Nadya answers cards that only she can answer: the letters, one fact to share, and whether a relative may call the hospital.
4. On hold with billing, the relative (Alex in the demo) reports what billing says, and the hospital's own policy line comes back.

After that the coordinator posts its own deadline reminder, and `make verify` checks that nothing private reached the family room.

## Run it (no API keys)

```
git clone --recurse-submodules https://github.com/NadyKor15/billradar-family-room && cd billradar-family-room
make up       # Switch 0.31.0 + Mattermost, the BillRadar engine (pinned), public policy sources, seed, both agents
make demo     # the story above, ≈60 s
make verify   # 8 checks, ends with "8/8 PASS"; each refusal check has a control that has to succeed
make test     # 351 tests, no stand needed; the ones that use the engine skip until `make engine` has run
make down     # stop everything (make wipe also drops the data)
```

GitHub runs these same commands on a clean clone at every push to main (`.github/workflows/clean-clone.yml`), with no keys.

Needs Docker with Compose, Python 3.12, make, git, curl, openssl and network access: `make up` pulls the images and downloads 14 public policy documents, and it stops if a download fails. The Mattermost image is amd64 only, so Apple Silicon runs it under emulation. Everything binds to 127.0.0.1 (ports: 38000 Switch API, 38030 Switch Gateway, 38065 Mattermost, 38432 Postgres). `make demo` plays Nadya and Alex through Mattermost's API. Open http://localhost:38065 to watch or to do the steps yourself; demo passwords are in `state/people.json`. The Gateway login is in `deploy/.env`.

## Two agents, different jobs

| | `@reader` | `@coordinator` |
|---|---|---|
| Member of | each person's private room, the agents-only `handoff` room | `family`, `handoff` |
| Holds | the photos, the BillRadar case, names and account numbers | codes, cents, dates, sheet and line numbers, usernames, prepared call answers and public policy quotes |
| Talks to | the owner of each private room; the coordinator in `handoff` | the family in `family`; the reader in `handoff` |
| Runs | in the engine's environment, BillRadar over MCP stdio | plain Python, stdlib only, its own API key and nothing else (with `LIVE=1`, also the model key) |

Both are custom agents on the Switch agent protocol (`connection_model: always_on`): one SSE stream down, HTTP up. `reader.py` and `coordinator.py` call only Switch's API and contain no Mattermost or Telegram client, so moving to Telegram changes only room ids in their state files. Rooms, the membership check, approval cards and addressing rules all come from Switch; without it each would have to be written per platform.

## The workflow, step by step

| # | Story step | What the family sees | Switch primitive | File | Call |
|---|---|---|---|---|---|
| 1 | 1 | Nadya posts 10 photos and PDFs with "@reader" in her own room | attachments on an addressed message; media route | `reader.py` `papers()` | SSE `message.attachments[]` → `GET /agents/{id}/rooms/{room}/media?mxc=…` |
| 2 | 1 | Reader answers in her room: 7 findings, $1,596.53 may come off | agent message | `reader.py` | `POST /agents/{id}/message` |
| 3 | 2 | Reader hands a de-identified case to the coordinator | agent-to-agent addressed message in an agents-only room | `handoff.py`, `reader.py` | `connect_to_room` + `send_targeted_message` |
| 4 | 2 | Coordinator posts the result in the family room: the letters wait for Nadya's OK, and a relative is asked about only through her | @-addressing people | `coordinator.py` `case_posted()` | `POST /agents/{id}/message` |
| 5 | 3 | Letters wait for Nadya's OK as cards in **her** room | approval cards; addressing policy decides who may answer | `reader.py` `card()` | `POST /agent-sessions/{sid}/approvals`, `GET /agent-sessions/approvals/outcomes` |
| 6 | 3 | "Share one fact with the family room?" (income band, no amount) | approval card as consent | `reader.py` `request()` | same |
| 7 | 3 | "Let Alex talk to billing for you?" → the call sheet appears in Alex's own room | consent card; the reader moves private data only between private rooms | `reader.py` `answered()` | same + `POST /agents/{id}/message` |
| 8 | 4 | On hold, Alex writes "@coordinator they say the discount is only for uninsured patients" → the hospital's own policy line comes back | round trip coordinator → reader → coordinator in `handoff` | `coordinator.py` `from_person()` | `send_targeted_message` both ways |
| 9 | after | "Show me Nadya's papers" → the coordinator tries with its own key, Switch says 403 | room membership enforced by the server | `coordinator.py` `try_private()` | `POST /agents/{id}/ops/read_context` |
| 10 | after | "⏰ Reminder: the financial assistance application is due 2026-04-01 and isn't approved yet" | Switch has no scheduler, so the coordinator keeps its own clock | `coordinator.py` `remind()` | `POST /agents/{id}/message` |

Telegram sends an album as separate messages: the reader takes the owner's files posted in the room in the 2 minutes before the @reader message; if nothing is left in that window, it asks for them again. The reader says "I read N files" (in the sample pack, 7 photos show 14 sheets and 3 files are PDFs, so findings cite "sheet 13" or "file 3").

Seeding (`seed.py`) uses the Gateway: `POST /gateway/users`, `POST /gateway/collaborations/{bridge}/identities` (links each Mattermost account to a Switch user), `POST /gateway/rooms`, `PUT /gateway/agents/{id}/addressing-policy`.

## What keeps private things private

- **Membership (Switch).** The coordinator is not a member of any private room, so `read_context`, the media route and posting there all return 403 (`make verify` V5, with controls that return 200).
- **Addressing policy (Switch).** The reader answers only the owner of each private room. A card in Nadya's room can be answered only by Nadya; the server operator's account, which Switch's setup puts in every channel, is refused (V6). The rules name people and never use Switch's `owner` subject, because the agents' owner is the server operator, whose account is in every channel.
- **Strict handoff schema and a PII gate (our code).** Every handoff message is `BRv1 ` plus one JSON object in an inline-code span (Mattermost renders `:[` in plain JSON as an emoticon; the old form without backticks still decodes): enums, integer cents, ISO dates, sheet and line references, letter kinds, source refs, and a family member's username in call requests. The only sentences are BillRadar's prepared call answers and quotes from public policies, and each passes an on-machine gate built from the case's own private values. Unknown keys are refused on both ends (`tests/test_boundary.py`).
- **Threat note.** The agent that talks with the family never holds a document and is refused the private rooms by Switch, so nothing typed in the family chat can pull a document out of it. By default it runs no model. With `LIVE=1` a model picks an answer's id for it and nothing the model writes is posted (`tests/test_chooser.py`). What Switch does not filter is what a member agent chooses to post; that part is the reader's code, tested above and checked live by V3.

## make verify

| | Check | Control that must succeed |
|---|---|---|
| V1 | each room holds exactly the agents and people it should (Switch's room records) | none: exact match, fails on any extra agent or person; the operator's own accounts are listed separately |
| V2 | the scanner lights on a planted canary and on the owner's own room | itself |
| V3 | 0 private values, account-like numbers or media handles in `family` and `handoff` | V2 |
| V4 | every agent message in `handoff` is strict BRv1 and none can render as an emoticon | the same scan lights on a synthetic old-form message |
| V5 | coordinator: private room read, post, media → 403 | coordinator reads `family` → 200; reader fetches the media → 200 |
| V6 | operator account answers a card in Nadya's room → refused | Nadya answers the same card → accepted |
| V7 | Switch's outcome records, as the reader saved them to `state/approvals.jsonl`: who approved which card, and when | — |
| V8 | the coordinator's own reminder was posted | — |

To see V3 fail, log in as `alex`, post `1234567` in `family`, and run `make verify` again: any run of 7 or more digits counts as account-like. `make wipe && make up` gives a clean stand.

`make verify` uses a third agent, `auditor`. It is a member of `family`, `handoff` and its own `canary-control` room, nobody can address it, and it scans the shared rooms with `read_context`. It posts only the planted canary, and only in `canary-control`.

## What is reused and what is new

| Part | When it was made | Evidence |
|---|---|---|
| BillRadar engine (`vendor/billradar-meta`, git submodule pinned at `b20aeb9`, same author) | Before this repository, for another project. Its files are unchanged at the pinned commit. Used over its MCP stdio server, and as a library for a live reading and the filled form | Its own public history: 10 commits, 2026-11-03 13:20 to 22:08 (UTC−5). The hacking window opened at 13:31 UTC−5 that day; the 2 later commits touch docs, one template and one test. It contains no Switch code |
| Switch's Compose file `deploy/switch-0.31.0/standalone-docker-compose.yml` | Published by Switch, unchanged | `LICENSE-switch` (Apache 2.0 with the Commons Clause, as Switch publishes it) |
| Everything else: both agents, the handoff schema and PII gate, seed, demo, verify, the stand scripts, this README | During the hackathon window, Nov 4 to 7, 2026 | This repository's git history, first commit 2026-11-04 19:35 (UTC−5) |

Without keys the engine runs only its fictional sample packs and says so (REPLAYED); the reader recognises a pack by the sha256 of the posted files, and part of a pack is enough: the whole pack is replayed. With `LIVE=1` the same rule decides what may go to a model, and only the files that were posted are read. The sample hospital, Seaside Regional, is fictional; its policy text is the public Financial Assistance Policy of Memorial Healthcare System (`memorial-2026-05`). The sample's "today" is 2026-03-02, which is why the deadline reads 2026-04-01.

## Live mode

There are two modes. By default the reader replays the engine's recorded result for a recognised fictional pack, and neither agent calls a model. With `LIVE=1` and a Meta Model API key, the reader reads that pack with the engine's models: SAM 3.1 finds the sheets, Muse Spark reads them, Muse Wonder reviews the result. The coordinator asks Muse Spark which checked answer fits.

A model's reading varies from run to run, so the reader reads the pack twice at the same time and shows the reading that found more money, then more items. A line in the owner's room says whether the two agree and gives both totals if they differ. If one reading stops, the line says the papers were read once.

`deploy/agents.sh` passes each agent only its own part. Without `LIVE=1` neither agent gets any of these, even if the shell has them. `deploy/agents.sh start` refuses `LIVE=1` with no key.

```
export MODEL_API_KEY=...        # from your own secret store; it is never written to a file here
LIVE=1 deploy/agents.sh restart
LIVE=1 make demo                # waits up to an hour for the reader's answer
make verify
.venv-engine/bin/python -m family_room.live_eval    # one JSON line about the last live reading
```

| Variable | Who gets it | What it does | Default |
|---|---|---|---|
| `LIVE` | both | `1` turns live mode on | off |
| `MODEL_API_KEY` | both | the key; reaches a process only through its environment | none |
| `LIVE_READINGS` | reader | how many readings of the same papers run at once | 2 |
| `LIVE_SHARED_GLIMMER` | reader | `1` lets every reading use Muse Glimmer. Each one still starts `BILLRADAR_LLAMA_SERVER`, so sharing one Muse Glimmer on `BILLRADAR_GLIMMER_PORT` needs a stand-in for that program. Otherwise only the first reading may start one | off |
| `LIVE_SPEND_CAP_USD` | reader | daily ceiling, passed to the reading as `BILLRADAR_DAILY_SPEND_USD`; must be above 0 | 15 |
| `META_SPEND_CAP_USD` | reader | the engine's ceiling over all days | 40 |
| `LIVE_READ_TIMEOUT_S` | reader | how long one reading may run before it is stopped | 3600 |
| `BILLRADAR_LLAMA_SERVER`, `BILLRADAR_GLIMMER_DIR` | reader | where llama-server and the Muse Glimmer weights are, if not on `PATH` and in `~/Models/Muse-Glimmer-30B-GGUF` | |
| `BILLRADAR_SECOND_READ` | reader | `0` turns off the engine's second read | on |
| `BILLRADAR_WONDER_REVIEW` | reader | `0` turns off Muse Wonder's review | on |
| `CHOOSER_MAX_CALLS` | coordinator | model calls per process, then word overlap only | 50 |
| `CHOOSER_TIMEOUT_S` | coordinator | seconds for one model call, then word overlap | 15 |
| `READER_WAIT_S` | `make demo` | how long it waits for the reader's answer | 60, or 3600 with `LIVE=1` |

**What goes to a model.** A live reading starts only when the sha256 of every posted file matches a paper of one fictional pack. Any other file gets the same refusal as without `LIVE=1`, and no reading starts.

- **The reader** starts a child process for each reading (`family_room/live_read.py`) and gives it the pack's name and the sha256 list, never the bytes. The child reads only the pack's own files on that list, checks that the pack says `"fictional": true`, and refuses otherwise, so a file from outside the pack cannot get to a model. It runs the engine's `pipeline.run` with the pack's own profile and date, in the engine's private mode: a blurred copy of each photo goes to SAM 3.1, the closed sheets and PDF pages to Muse Spark, and the findings and letters to Muse Wonder. A new pack without a profile is not read.
- **The closing step** reads each sheet on this computer with Muse Glimmer. On this pack it first looks for a recorded answer to the same picture. The recordings match byte for byte only where the pictures come out exactly the same: on the Mac that made them all of them matched, and on another Mac with the same package versions they did not. A sheet with no recording needs Muse Glimmer itself: llama-server, the weights and about 32 GB of free memory. Without those the reading stops at the first such sheet and says so. What Glimmer answers is kept in `~/.cache/billradar-family-room/`, so on a new computer the first reading is the slow one, and after that only a picture it has not seen goes to Glimmer. SAM 3.1, Muse Spark and Muse Wonder are called on every reading.
- **Reading rules.** `family_room/reading_fixes.py` applies four fixed rules to a model's reading, after the reading and before the engine's checks, in live mode only. A PDF bill whose provider was read as a person ("LAST, FIRST") is the hospital's bill when its first printed lines name the hospital and no practice. On a summary statement, a line that says "total" and repeats the sheet's total charges to the cent is not a service; when such a sheet has no lines left and its balance and total charges were read, two reads differing only between no lines and at most 2 raise no doubt about its money. A line with no date on a sheet that covers one day takes that day. On an itemized bill, when one of two identical lines holds the sheet's whole balance as its patient share and no other line has a share, that amount is the balance. No rule writes a sum, but a rule can move the total, up or down, by changing what the checks count. The checks still work out every sum, and the engine's verifier, which sees the corrected reading, has the last word. A rule that fires leaves a line in the reading's `problems`. The engine's own files stay unchanged at the pinned commit, so these rules live in this repository.
- **The marked sheet** comes after the owner has the message, the cards and the handoff has gone, and only for the reading that is shown: a short separate child (`family_room/live_marks.py`) calls the engine's own `highlight.attach` on that finished case, one SAM 3.1 call per mark on the covered sheet, through the same spend ceiling. The reading that is not shown pays for no mark. A mark exists only for a finding with a sum printed on a photographed sheet; PDF pages and findings without a sum on a sheet (in the sample pack, the $1,399.71 financial assistance finding) get none. Where the device cannot find the row, there is no mark and nothing is said about it. Each mark SAM 3.1 confirmed is posted as one JPEG in the owner's own room, then one line: "BillRadar marks the exact line on the photo: code finds the row on this device and SAM 3.1 confirms it on the covered sheet." A mark only the row finder stands behind is not posted. The picture holds the sheet as photographed, so it goes nowhere else, and the handoff message is unchanged. A failure here is logged and loses nothing that was already sent.
- **The MCP server** has no key and no live switch in either mode. It only reads finished cases.
- **The coordinator** sends what the relative wrote and the numbered list of `they_say` strings, on a stdlib HTTP call to the same endpoint and model as the engine's reader. Long digit runs, media handles, phone numbers and US dates are dropped first. A name someone typed is not a shape, so it does go through.

`python -m family_room.telegram wire` and `unwire` restart the agents with the caller's environment, so run them with the same variables to stay live.

**What the coordinator's model can and cannot do.** It can name one id from the list, or none. Its reply is parsed for that one field, and anything else counts as a failed call and falls back to word overlap. An id that is not in the list counts as none. It cannot post a word: the sentence in the family room is the reader's checked answer with its quote, built by code. "Show me Nadya's papers" is handled before the model is asked, so the 403 does not depend on it. Each call adds a line to `state/model_calls.jsonl`: time, model, status, chosen id, seconds, and no message text. These calls are not in the engine's spend book; `CHOOSER_MAX_CALLS` is their limit.

**When something fails.** When no reading finishes, the room hears "Live reading stopped" with the reason, the cap and a missing Muse Glimmer included, and the family room gets nothing. The reader never falls back to the recording and never claims a live result it did not get. Papers posted while a live reading runs are refused. A timeout stops the child and everything it started, and only those. The child stops itself if the reader goes away.

**What a live run leaves.** `state/live_runs.jsonl` has one line per run with both readings' totals, the run's cost, seconds and models, and the list of problems for each finished reading, so a partial failure is visible. `state/engine-live.log` has the child's stderr, and the models' raw answers go to `state/live-recordings/`. The finished case goes to `runs/` in the engine's folder, which its git ignores, so the submodule stays clean (`tests/test_live_dryrun.py` checks `git status` on a clone of it).

**Measured on `brs1-birth-all-errors`** (`family_room/live_eval.py`, scored as the engine's `scripts/eval_run.py` scores its own path): 5 runs of two readings at once, 10 readings. They ran with `LIVE_SHARED_GLIMMER=1`, Muse Glimmer on a second Mac through a tunnel, and its answers kept from earlier readings, so a first reading on a new computer takes longer.

| | 10 live readings |
|---|---|
| Total $1,596.53 | 10 of 10 |
| Wrong sums or deadlines | 0 |
| False findings | 0 |
| Planted findings found | 7 of 7 in 2 readings, 6 of 7 in 8 |
| A prepared answer to "they say the discount is only for uninsured patients" | 10 of 10 |
| Cost per run, both readings, before the marked sheet | $0.71 to $0.85 |
| Time per run | 310 to 366 s |

In those 8 readings the seventh finding, newborn care filed on the mother's member ID, came back under "worth asking", and the score does not count it as found.

## Reproducing: what to know

- Switch is pinned to 0.31.0 (`SWITCH_VERSION` in `deploy/.env`). Other versions are untested.
- The Mattermost image is amd64 only. On Apple Silicon, Docker runs it under emulation, so the first start is slow.
- `deploy/stand.sh init-env` (run by `make up`) writes `deploy/.env` with fresh secrets, including `CONTROLLER_TOKEN_SECRET`. The file is created with mode 0600 and stays on your machine.
- `seed.py` links each Mattermost account to a Switch user before the agents start (`POST /gateway/collaborations/{bridge}/identities`). Addressing rules name people by that link, so the link has to exist first.
- A newly registered Switch agent answers only its owner (Switch docs, identity-and-access, "Precedence"). `seed.py` sets the rules for both agents: the reader answers the owner of each private room and the coordinator in `handoff`; the coordinator answers the family in `family` and the reader in `handoff`.

## Telegram

The same agents also run in Telegram, through the same Switch, with no public address (the bridge uses long polling). `make up` starts Mattermost only; Telegram is a manual step, checked live on 2026-11-05.

1. Create a bot with @BotFather and turn Group Privacy off before adding it to a group.
2. Put `{"bot_token": "...", "bot_username": "..."}` in a file and run `python3.12 -m family_room.telegram connect < that-file`. The token is not printed or logged.
3. Make groups with the bot: one for the person handling the bills, one for the family, and, if you want the call sheet on the relative's phone, one for the relative. Each person needs a Telegram username and writes one message in each of their groups, so Switch sees the rooms and the people.
4. `python3.12 -m family_room.telegram status` lists room ids and Telegram user ids. Then `python3.12 -m family_room.telegram wire PAPERS_ROOM FAMILY_ROOM OWNER_ID HELPER_ID [HELPER_ROOM]`. `python3.12 -m family_room.telegram probe` asks Switch about each private group as each agent: the coordinator must get 403 and the reader 200. `unwire` puts the agents back on Mattermost.

What is different in Telegram:

- An album arrives as separate messages, and only one of them carries the caption. The reader counts everything the same person sent in the 2 minutes before the "@reader" message as one batch, so a file sent in that window by mistake would join it.
- Telegram recompresses images picked from the gallery, and the reader then cannot recognise the sample pack. Send them as files.
- Cards appear as inline buttons in the owner's group.
- The files to send are in `vendor/billradar-meta/eval/packs/work/brs1-birth-all-errors/`.
- One Telegram bot fronts both agents, and Switch still checks room membership per agent.
- `handoff` and `make verify` stay on the Mattermost rooms. The coordinator's 403 probe asks about each person's Telegram group if they have one, and otherwise about their Mattermost room.
- With `HELPER_ROOM` the call sheet goes to the relative's Telegram group. Without it, it goes to their Mattermost room `alex-papers`.

## Known limits

- A refused card answer is visible in Switch's log, not to the person who pressed the button.
- Switch's card adds "Nobody here is linked to this agent's owner…": it tries to notify the agents' owner, the server operator; the reader mentions the room's owner itself.
- Only BillRadar's fictional packs are ever read, in both modes. Reading a family's own papers is not built here.
- Switch's setup puts the server operator's Mattermost accounts in every channel, including private rooms and `handoff`. The room boundary covers other people and agents and does not cover whoever runs the server.
- Both agents run as one OS user, and each reads its own key from `state/` by convention. A real deployment would run them under separate users.
- Cases and the reminder timer live in memory: restarting an agent forgets them. In the demo the reminder fires 45 seconds after the case arrives (`REMINDER_AFTER_S`); it does not count down to the date.

## License

This repository's own code is under the MIT License (`LICENSE`). `deploy/switch-0.31.0/` and the submodule `vendor/billradar-meta` keep their own licenses.
