# Week 2: asyncio

Run `python pipeline.py` and `python resilient_client.py`. Numbers are from Python 3.12 on macOS.

## Backpressure (10,000 ticks, 3 consumers)

| maxsize | work per tick | ticks/s | peak depth |
| ------: | ------------: | ------: | ---------: |
|       1 |          none |  35,235 |          1 |
|  10,000 |          none | 884,678 |     10,000 |
|       1 |          1 ms |   2,575 |          1 |
|  10,000 |          1 ms |   2,498 |     10,000 |

## Blocking-call stall (10 ms heartbeat, 2 s of work)

| variant                                  | max heartbeat gap (ms) | missed beats |
| ---------------------------------------- | ---------------------: | -----------: |
| `await asyncio.sleep(2)`                 |                   13.4 |            0 |
| `time.sleep(2)` in a coroutine           |                  2,007 |          200 |
| `await asyncio.to_thread(time.sleep, 2)` |                   12.1 |            0 |

The 1,000-tick pipeline took 0.41 s on its own and 2.40 s with a `time.sleep(2)` task running alongside.

## Why blocking work goes on a thread and socket I/O stays on the event loop

The event loop runs on one thread, and it only switches tasks at an `await`. A blocking HTTP call like `requests.get` or `yf.Ticker(...).fast_info` never awaits. While it waits on the network, it holds the thread, and every other task freezes. Our measurement shows this: one `time.sleep(2)` stalled a 10 ms heartbeat for 2,007 ms and missed 200 beats, and the pipeline sat idle for those 2 seconds. In a trading bot, that means ticks go unread, quotes go stale, orders aren't sent, and websocket keepalive pings get no reply. If the stall lasts long enough, the exchange drops the connection and the bot's next read raises `ConnectionClosed`.

The reference broker (`engine/broker/broker.py:594-628`) avoids this. It polls yfinance on a separate daemon thread, which can block as long as it needs to. Socket I/O stays on the loop because it is non-blocking, so one thread can serve many connections. `asyncio.to_thread` gives the same result: in our test the heartbeat gap stayed at 12 ms.

To detect it, run with `asyncio.run(main(), debug=True)`. Any step slower than `slow_callback_duration` (100 ms by default) logs a warning such as `Executing <Task ...> took 2.006 seconds`.
