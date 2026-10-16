# AlgoArena — student team Makefile
# The arena address and your token live in .env (created by make register).

-include .env
export

ARENA_URL ?= https://arena.example.edu
# The engine lives under engine/ — every target needs it importable.
export PYTHONPATH := engine:.
# The season's week scenarios (the rules and event calendar of each Friday
# block) ship under season/ so the season simulator runs offline.
export SEASON_DIR := season
export SEASON_PERSIST := false

install:
	pip install -r requirements.txt

register:
	python scripts/create_team.py --remote $(ARENA_URL)

# Prove your connection end-to-end (registration code needed: CODE=...)
test-remote:
	python scripts/test_remote.py --arena $(ARENA_URL) --code $(CODE)

# Run a bot seat: make trader BOT=<id>  — TEAM_ID=<id> works too.
# Add LOG=1 to also write the bot's log to logs/<id>_<timestamp>.log.
TEAM_ID ?= $(BOT)

trader:
	TEAM_ID=$(TEAM_ID) LOG=$(LOG) python -m team.trader

broker:
	TEAM_ID=$(TEAM_ID) LOG=$(LOG) python -m team.broker

# Strategy seats (each writes its own log under logs/):
#   make momentum   → trader_1, moving-average crossover + event ride/fade
#   make meanrev    → trader_2, buy dips / sell the rebound + event fade
# They run a 0.1 s loop: on a scheduled event the first order decides who
# gets the cheap shares (override with TICK_INTERVAL=0.5).
SEAT_TICK ?= 0.1

momentum:
	TICK_INTERVAL=$(or $(TICK_INTERVAL),$(SEAT_TICK)) TEAM_ID=$(or $(TEAM_ID),midway_traders_trader_1) python -m team.momentum_trader

meanrev:
	TICK_INTERVAL=$(or $(TICK_INTERVAL),$(SEAT_TICK)) TEAM_ID=$(or $(TEAM_ID),midway_traders_trader_2) python -m team.mean_reversion_trader

exchange:
	python -m exchange.server

sim:
	python tests/sim_session.py

# Simulate a graded week offline against the house bots: make season WEEK=3
WEEK ?= 1
season:
	python scripts/season_sim.py --weeks $(WEEK)

# Replay a recorded block into a local dashboard: make replay FILE=sessions/<file>
replay:
	python scripts/replay_session.py $(FILE) --speed 5

# Replay a recorded block through a strategy seat, events and fills included:
#   make backtest FILE=sessions/<file> STRATEGY=momentum
STRATEGY ?= momentum
backtest:
	python scripts/event_backtest.py $(FILE) --bot $(STRATEGY)

test:
	pytest tests/ -q

.PHONY: install register trader broker momentum meanrev exchange sim season replay backtest test
