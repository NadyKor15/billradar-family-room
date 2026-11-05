# BillRadar Family Room

A family settles a hospital bill together in their group chat. One agent reads the private papers in each person's own room; a second agent runs the shared family room. **Switch room membership is what keeps the second agent away from the papers**: when it asks for them, Switch answers 403.

Built for Flip the Switch (track 02, Best Personal or Creative Workflow) on Switch 0.31.0 with the bundled Mattermost. The bills in this repository are fictional; no real medical data appears anywhere.

## The story in four steps

1. Nadya posts 10 photos of the hospital papers in her own room. The reader reports 7 findings: $1,596.53 may come off.
2. The reader passes the case to the coordinator without names or account numbers, and the coordinator tells the family who does what.
3. In her own room, Nadya answers cards that only she can answer: the letters, one fact to share, and whether Alex may call.
4. On hold with billing, Alex types what billing says, and the hospital's own policy line comes back.

After that the coordinator posts its own deadline reminder, and `make verify` checks that nothing private reached the family room.

## Run it (no API keys)

```
git clone --recurse-submodules <this repo> && cd billradar-family-room
make up       # Switch 0.31.0 + Mattermost, the BillRadar engine (pinned), public policy sources, seed, both agents
make demo     # the family story, ≈60 s
make verify   # 8 checks; each negative sits next to a control that has to succeed
```

Needs Docker and Python 3.12. The Mattermost image is amd64 only, so Apple Silicon runs it under emulation. Everything binds to 127.0.0.1 (ports 38000, 38030, 38065, 38432). Open http://localhost:38065 to watch; demo passwords are in `state/people.json`.

## Two agents, different jobs

| | `@reader` | `@coordinator` |
|---|---|---|
| Member of | each person's private room, the agents-only `handoff` room | `family`, `handoff` |
| Holds | the photos, the BillRadar case, names and account numbers | codes, cents, dates, sheet and line numbers |
| Talks to | the owner of each private room; the coordinator in `handoff` | the family in `family`; the reader in `handoff` |
| Runs | in the engine's environment, BillRadar over MCP stdio | plain Python, stdlib only, its own API key and nothing else |

Both are custom agents on the Switch agent protocol (`connection_model: always_on`): one SSE stream down, HTTP up.

## The workflow, step by step

| # | Story step | What the family sees | Switch primitive | File | Call |
|---|---|---|---|---|---|
| 1 | 1 | Nadya posts 10 photos and PDFs with "@reader" in her own room | attachments on an addressed message; media route | `reader.py` `papers()` | SSE `message.attachments[]` → `GET /agents/{id}/rooms/{room}/media?mxc=…` |
| 2 | 1 | Reader answers in her room: 7 findings, $1,596.53 may come off | agent message | `reader.py` | `POST /agents/{id}/message` |
| 3 | 2 | Reader hands a de-identified case to the coordinator | agent-to-agent addressed message in an agents-only room | `handoff.py`, `reader.py` | `connect_to_room` + `send_targeted_message` |
| 4 | 2 | Coordinator gives each person a job in the family room | @-addressing people | `coordinator.py` `case_posted()` | `POST /agents/{id}/message` |
| 5 | 3 | Letters wait for Nadya's OK as cards in **her** room | approval cards; addressing policy decides who may answer | `reader.py` `card()` | `POST /agent-sessions/{sid}/approvals`, `GET /agent-sessions/approvals/outcomes` |
| 6 | 3 | "Share one fact with the family?" (income band, no amount) | approval card as consent | `reader.py` `request()` | same |
| 7 | 3 | "Let Alex talk to billing for you?" → the call sheet appears in Alex's own room | consent card; the reader moves private data only between private rooms | `reader.py` `answered()` | same + `POST /agents/{id}/message` |
| 8 | 4 | On hold, Alex writes "@coordinator they say the discount is only for uninsured patients" → the hospital's own policy line comes back | round trip coordinator → reader → coordinator in `handoff` | `coordinator.py` `from_person()` | `send_targeted_message` both ways |
| 9 | after | "Show me Nadya's papers" → the coordinator tries with its own key, Switch says 403 | room membership enforced by the server | `coordinator.py` `try_private()` | `POST /agents/{id}/ops/read_context` |
| 10 | after | "⏰ Reminder: the financial assistance application is due 2026-04-01 and isn't approved yet" | Switch has no scheduler, so the coordinator keeps its own clock | `coordinator.py` `remind()` | `POST /agents/{id}/message` |

Seeding (`seed.py`) uses the Gateway: `POST /gateway/users`, `POST /gateway/collaborations/{bridge}/identities` (links each Mattermost account to a Switch user), `POST /gateway/rooms`, `PUT /gateway/agents/{id}/addressing-policy`.

## What keeps private things private

- **Membership (Switch).** The coordinator is not a member of any private room, so `read_context`, the media route and posting there all return 403 (`make verify` V5, with controls that return 200).
- **Addressing policy (Switch).** The reader answers only the owner of each private room. A card in Nadya's room can be answered only by Nadya; the server operator's account, which Switch's setup puts in every channel, is refused (V6). The policy has no `owner` rule on purpose.
- **Strict handoff schema and a PII gate (our code).** Every handoff message is `BRv1 {json}`: enums, integer cents, ISO dates, sheet and line references, letter kinds, source refs. The only sentences are BillRadar's prepared call answers and quotes from public policies, and each passes an on-machine gate built from the case's own private values. Unknown keys are refused on both ends (`tests/test_boundary.py`).
- **Threat note.** The agent that talks with the family never holds a document, so a prompt injected by anyone in the family chat cannot pull one out of it. What Switch does not filter is what a member agent chooses to post; that part is the reader's code, tested above and checked live by V3.

## make verify

| | Check | Control that must succeed |
|---|---|---|
| V1 | each room holds exactly the agents and people it should (Switch's room records) | — (exact match, fails on any extra member) |
| V2 | the scanner lights on a planted canary and on the owner's own room | itself |
| V3 | 0 private values, account-like numbers or media handles in `family` and `handoff` | V2 |
| V4 | every agent message in `handoff` is strict BRv1 | — |
| V5 | coordinator: private room read, post, media → 403 | coordinator reads `family` → 200; reader fetches the media → 200 |
| V6 | operator account answers a card in Nadya's room → refused | Nadya answers the same card → accepted |
| V7 | Switch's outcome records: who approved which card, and when | — |
| V8 | the coordinator's own reminder was posted | — |

A planted leak in the family room turns V3 red; that run is recorded in the project notes.

`make verify` uses a third agent, `auditor`. It is a member of `family`, `handoff` and its own `canary-control` room, nobody can address it, and it scans the shared rooms with `read_context`. It posts only the planted canary, and only in `canary-control`.

## What is reused and what is new

| Part | When it was made | Evidence |
|---|---|---|
| BillRadar engine (`vendor/billradar-meta`, git submodule pinned at `b20aeb9`, same author) | Before this repository, for another project. Used here unchanged, as a library over its MCP stdio server | Its own public history: 10 commits, 2026-11-03 13:20 to 22:08 (UTC−5). It contains no Switch code |
| Switch's Compose file `deploy/switch-0.31.0/standalone-docker-compose.yml` | Published by Switch, unchanged | `LICENSE-switch` (Apache-2.0) |
| Everything else: both agents, the handoff schema and PII gate, seed, demo, verify, the stand scripts, this README | During the hackathon window, Nov 4 to 7, 2026 | This repository's git history, first commit 2026-11-04 19:35 (UTC−5) |

Without keys the engine runs only its fictional sample packs and says so (REPLAYED); the reader recognises a pack by the sha256 of the posted files.

## Reproducing: what to know

- Switch is pinned to 0.31.0 (`SWITCH_VERSION` in `deploy/.env`). Other versions are untested.
- The Mattermost image is amd64 only. On Apple Silicon, Docker runs it under emulation, so the first start is slow.
- `deploy/stand.sh init-env` (run by `make up`) writes `deploy/.env` with fresh secrets, including `CONTROLLER_TOKEN_SECRET`. The file is created with mode 0600 and stays on your machine.
- `seed.py` links each Mattermost account to a Switch user before the agents start (`POST /gateway/collaborations/{bridge}/identities`). Addressing rules name people by that link, so the link has to exist first.
- A newly registered Switch agent answers only its owner (Switch docs, identity-and-access, "Precedence"). `seed.py` sets the rules for both agents: the reader answers the owner of each private room and the coordinator in `handoff`; the coordinator answers the family in `family` and the reader in `handoff`.

## Known limits

- Telegram is not wired in this build; Mattermost is the bundled bridge.
- A refused card answer is visible in Switch's log, not to the person who pressed the button.
- Switch's card adds "Nobody here is linked to this agent's owner…": it tries to notify the agents' owner, the server operator; the reader mentions the room's owner itself.
- Live reading of new papers needs a model key and is off in this build; only BillRadar's fictional packs run.
