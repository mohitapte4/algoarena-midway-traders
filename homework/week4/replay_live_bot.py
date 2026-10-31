#!/usr/bin/env python3
"""replay_live_bot.py — run one seat's real on_tick, at the commit that traded, over a tape.

Called by clock_page.py with the commit exported to a folder. Runs that commit's own
scripts/event_backtest.py (tape-time clock, acks, fills through on_fill) and records each
fill, so live and replay can be compared on the same terms. Prints JSON.

That script predates Week 4's rules, so two things are changed for this run only, leaving
on_tick alone: with --shorts a sell may go short down to the position limit, as the venue
allows (the seat enforces its own smaller limit), and ORCA gets no granted shares or wait,
since it lists mid-session and the seat's on_tick must run from the open. There is no IPO
allocation in the replay.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--code", required=True, help="folder holding the exported commit")
    ap.add_argument("--tape", required=True)
    ap.add_argument("--bot", required=True, choices=["momentum", "meanrev"])
    ap.add_argument("--seat", required=True)
    ap.add_argument("--fee", type=float, required=True)
    ap.add_argument("--rebate", type=float, required=True)
    ap.add_argument("--cash", type=float, default=300_000.0)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--shorts", action="store_true")
    a = ap.parse_args()

    os.environ["TEAM_ID"] = a.seat
    code = pathlib.Path(a.code).resolve()
    sys.path[:0] = [str(code / "engine"), str(code)]
    path = code / "scripts" / "event_backtest.py"
    spec = importlib.util.spec_from_file_location("event_backtest", path)
    eb = importlib.util.module_from_spec(spec)
    sys.modules["event_backtest"] = eb
    source = path.read_text()
    if a.shorts:
        locked = 'if side == "sell" and qty > held:'
        if locked not in source:
            raise SystemExit("event_backtest.py changed: the short check was not found")
        source = source.replace(locked, 'if side == "sell" and held - qty < -POSITION_LIMIT:')
    exec(compile(source, str(path), "exec"), eb.__dict__)
    eb.POSITION_LIMIT = a.limit

    fills = []
    original = eb.Venue._fill

    def record(self, side, symbol, qty, price, maker):
        fills.append([self.now, side, symbol, qty, price, maker])
        original(self, side, symbol, qty, price, maker)

    eb.Venue._fill = record
    args = argparse.Namespace(bot=a.bot, no_events=False, label=a.bot, tick=0.1, latency=0.05,
                              fill="sweep", fee=a.fee, rebate=a.rebate, maker_haircut=0.5,
                              cash=a.cash, seed_shares=20)
    tape = eb.load_tape(pathlib.Path(a.tape))
    tape.symbols = [s for s in tape.symbols if s != "ORCA"]
    eb.run(tape, args)
    json.dump({"fills": fills}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
