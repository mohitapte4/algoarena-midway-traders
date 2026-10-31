#!/usr/bin/env python3
"""clock_page.py — logical vs real clock for Friday Oct 23 (Week 4), from the tape alone.

    python clock_page.py [--week 4] [--commit 9485c5f] [--tape <file>]

Live fills, wall-clock length of a tick, ticks to react to each event, and the same on_tick
replayed at the commit that traded (the last one before the block opened). The tape is read
from this folder; the bot's code at that commit comes from this repository's git history.

P&L here = cash flow of the seat's fills (fees and rebates included) plus the inventory they
left, marked at the block's last mid, for live and replay alike.
"""
from __future__ import annotations

import argparse
import bisect
import io
import json
import pathlib
import subprocess
import sys
import tarfile

import duckdb
import numpy as np

from backtest_hw4 import RULES

HERE = pathlib.Path(__file__).resolve().parent
SEATS = {"midway_traders_trader_1": "momentum", "midway_traders_trader_2": "meanrev"}
TAPE = HERE / "session_week04_20261023_204025_market.jsonl.gz"


def repo_root() -> str:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE, capture_output=True, text=True)
    if out.returncode:
        raise SystemExit("clock_page.py needs this repository's git history for the bot's code")
    return out.stdout.strip()


def export_commit(commit: str, dest: pathlib.Path) -> pathlib.Path:
    code = dest / f"code_{commit}"
    if not code.exists():
        code.mkdir(parents=True)
        blob = subprocess.run(["git", "archive", commit, "team", "engine", "scripts", "arena"],
                              cwd=repo_root(), check=True, capture_output=True).stdout
        tarfile.open(fileobj=io.BytesIO(blob)).extractall(code, filter="data")
    return code


def summary(fills: list[dict], last_mid: dict) -> dict:
    """fills: dicts with side, symbol, qty, price, cost (fee paid minus rebate earned)."""
    shares = sum(f["qty"] for f in fills)
    pnl = sum((1 if f["side"] == "buy" else -1) * f["qty"] * (last_mid[f["symbol"]] - f["price"])
              - f["cost"] for f in fills)
    return {"fills": len(fills), "avg_price": sum(f["qty"] * f["price"] for f in fills) / shares,
            "pnl": pnl}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week", type=int, default=4)
    ap.add_argument("--commit", default="9485c5f", help="the commit that traded this block")
    ap.add_argument("--tape", default=str(TAPE))
    ap.add_argument("--warehouse", default=str(HERE / "build" / "tape.parquet"))
    a = ap.parse_args()
    W, week = a.warehouse, a.week
    dk = duckdb.connect()
    dk.execute(f"""
      CREATE VIEW book AS SELECT * FROM read_parquet('{W}/book_snapshot/**/*.parquet', hive_partitioning=true) WHERE week={week};
      CREATE VIEW trades AS SELECT * FROM read_parquet('{W}/trade_execution/**/*.parquet', hive_partitioning=true) WHERE week={week};
      CREATE VIEW events AS SELECT * FROM read_parquet('{W}/session_event/**/*.parquet', hive_partitioning=true) WHERE week={week};
      CREATE VIEW lines AS SELECT t, ts FROM book UNION ALL SELECT t, ts FROM trades UNION ALL SELECT t, ts FROM events;""")

    last_mid = dict(dk.execute("SELECT symbol, arg_max(mid, seq) FROM book GROUP BY symbol").fetchall())
    fee, rebate = dk.execute("SELECT sum(fee) / sum(notional) FILTER (fee > 0), "
                             "sum(maker_rebate) / sum(notional) FILTER (maker_rebate > 0) "
                             "FROM trades").fetchone()
    fee, rebate = round(fee, 6), round(rebate, 6)

    # Wall-clock length of a tick: gaps between each tick's first line, open to close.
    per_tick = dk.execute("SELECT t, min(epoch(ts)) FROM lines WHERE t >= 0 GROUP BY t ORDER BY t").fetchall()
    ticks, stamps = [r[0] for r in per_tick], [r[1] for r in per_tick]
    gaps = np.diff(stamps) / np.maximum(np.diff(ticks), 1)
    wall = {"ticks": ticks[-1] - ticks[0], "seconds": stamps[-1] - stamps[0], "mean": float(gaps.mean()),
            "p1": float(np.percentile(gaps, 1)), "median": float(np.median(gaps)),
            "p99": float(np.percentile(gaps, 99)), "max": float(gaps.max())}

    def tick_of(stamp: float) -> int:
        return ticks[max(bisect.bisect_right(stamps, stamp) - 1, 0)]

    code = export_commit(a.commit, HERE / "build")
    shorts = ["--shorts"] if RULES[week]["shorts"] else []
    tape = str(pathlib.Path(a.tape).resolve())
    live, replay, live_fills, replay_fills = {}, {}, {}, {}
    for seat, bot in SEATS.items():
        rows = dk.execute("""
            SELECT t, seq, symbol, side, quantity, price, cost FROM (
              SELECT t, seq, symbol, 'buy' AS side, quantity, price,
                     CASE WHEN taker_id = ? THEN fee ELSE -maker_rebate END AS cost
              FROM trades WHERE buyer_id = ?
              UNION ALL
              SELECT t, seq, symbol, 'sell', quantity, price,
                     CASE WHEN taker_id = ? THEN fee ELSE -maker_rebate END
              FROM trades WHERE seller_id = ?) ORDER BY seq""", [seat] * 4).fetchall()
        live_fills[seat] = [dict(zip(("t", "seq", "symbol", "side", "qty", "price", "cost"), r)) for r in rows]
        live[seat] = summary(live_fills[seat], last_mid)

        # The same on_tick replayed, at the commit that traded.
        out = subprocess.run(
            [sys.executable, str(HERE / "replay_live_bot.py"), "--code", str(code), "--tape", tape,
             "--bot", bot, "--seat", seat, "--fee", str(fee), "--rebate", str(rebate),
             "--limit", str(RULES[week]["position_limit"]), *shorts],
            capture_output=True, text=True, cwd=code)
        if out.returncode:
            raise SystemExit(f"replay of {bot} failed:\n{out.stderr[-2000:]}")
        data = json.loads(out.stdout[out.stdout.rindex('{"fills"'):])
        replay_fills[seat] = [{"t": tick_of(stamp), "ts": stamp, "symbol": sym, "side": side, "qty": qty,
                               "price": px, "cost": -qty * px * rebate if maker else qty * px * fee}
                              for stamp, side, sym, qty, px, maker in data["fills"]]
        replay[seat] = summary(replay_fills[seat], last_mid)

    # Ticks to react: the first fill in the event's direction after the event's own line (a fill
    # earlier in the same tick came before the message). The fade's first order usually rests at
    # the plan's floor, so maker fills count. This tape has no order sends or acks.
    reactions = []
    for ev_t, ev_seq, ev_ts, pct, message, data in dk.execute(
            "SELECT t, seq, epoch(ts), pct, message, data FROM events "
            "WHERE event = 'CALENDAR_EVENT' ORDER BY seq").fetchall():
        names = [s for s in json.loads(data).get("affected", []) if s != "ARENA10"]
        side = "buy" if pct > 0 else "sell"
        for seat in SEATS:
            reactions.append({
                "event_t": ev_t, "event": message, "seat": seat,
                "live_ticks": min((f["t"] - ev_t for f in live_fills[seat] if f["symbol"] in names
                                   and f["side"] == side and f["seq"] > ev_seq), default=None),
                "replay_ticks": min((f["t"] - ev_t for f in replay_fills[seat] if f["symbol"] in names
                                     and f["side"] == side and f["ts"] >= ev_ts), default=None)})

    report = {"week": week, "commit": a.commit, "fee": fee, "rebate": rebate, "wall_clock": wall,
              "live": live, "replay": replay, "reactions": reactions}
    path = HERE / "results" / f"clock_week{week:02d}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("fee", "rebate", "wall_clock", "live", "replay")}, indent=1))
    for r in reactions:
        print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
