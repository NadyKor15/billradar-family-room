# BillRadar Family Room on Switch 0.31.0 (bundled Mattermost, no API keys)
#   make up       Switch + Mattermost (pinned 0.31.0), engine venv, policy sources, seed, both agents
#   make demo     the family story, played through Mattermost's API, waiting for the agents' answers
#   make verify   8 checks, each negative next to a control that must succeed
#   make template-check   Switch checks the room template on the running stand, next to a broken copy that must fail
#   make judge    up, demo, verify and template-check on a new stand, no keys, ending in one line
#   make down     stop agents and the stack (make wipe also drops the volumes)
SHELL := /bin/bash
PY ?= python3.12
export FAMILY_ROOM_STATE ?= $(CURDIR)/state
export REMINDER_AFTER_S ?= 45

.PHONY: up stack engine seed agents demo verify template-check judge test down wipe status

up: stack engine seed agents

stack:
	@test -f deploy/.env || deploy/stand.sh init-env
	deploy/stand.sh up
	@echo "waiting for Switch setup..."; for i in $$(seq 1 80); do \
	  docker logs nadflip-setup-1 2>&1 | grep -q "Setup complete" && break; sleep 3; done
	@curl -fsS -o /dev/null http://127.0.0.1:38000/health && echo "switch: healthy"

engine:
	git submodule update --init vendor/billradar-meta
	@test -x .venv-engine/bin/python || $(PY) -m venv .venv-engine
	.venv-engine/bin/pip install -q -e vendor/billradar-meta
	cd vendor/billradar-meta && ../../.venv-engine/bin/python scripts/fetch_policies.py

seed:
	.venv-engine/bin/python -m family_room.seed

agents:
	deploy/agents.sh start

demo:
	$(PY) -m family_room.demo

verify:
	$(PY) -m family_room.verify

template-check:
	$(PY) -m family_room.template_check

judge:
	PY=$(PY) $(PY) -m family_room.judge

test:
	@test -x .venv-engine/bin/python || $(PY) -m venv .venv-engine
	@.venv-engine/bin/python -m pytest --version >/dev/null 2>&1 || .venv-engine/bin/pip install -q pytest
	.venv-engine/bin/python -m pytest -q tests

status:
	deploy/stand.sh ps; deploy/agents.sh status

down:
	deploy/agents.sh stop; deploy/stand.sh down

wipe:
	deploy/agents.sh stop; deploy/stand.sh wipe; rm -rf state
