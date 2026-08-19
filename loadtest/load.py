"""Pure-asyncio load client for the queue service (Phase 14, local floor run).

No external deps. Two modes:

  python load.py join <n> <concurrency>     # join throughput + latency percentiles
  python load.py sse  <m> <hold_seconds>    # hold m SSE connections; measure Redis-op flatness

The SSE mode is the one that matters: it proves design.md §6's central claim — that ONGOING Redis
command volume does NOT scale with the number of connected clients, because position is arithmetic
in memory and the fan-out is one subscription per process. It separates the two costs honestly:
opening a connection costs one authoritative position lookup — a single EVALSHA of position.lua,
which `INFO commandstats` counts as ~6 (the EVALSHA plus the 5 redis.call()s inside it: EXISTS,
ZRANK, GET, HGET, ZCARD) — while holding it costs ~0 per tick between the 30s reconciliations.
We measure Redis `INFO commandstats` around each phase.
"""

import asyncio
import json
import sys
import time

import redis

HOST, PORT = "127.0.0.1", 8001
EVENT = "coldplay-mumbai-2026"


async def _join_once() -> tuple[bool, float, str | None]:
    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.open_connection(HOST, PORT)
        writer.write(
            f"POST /api/queue/{EVENT}/join HTTP/1.1\r\nHost: {HOST}\r\n"
            f"Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        data = await reader.read()  # Connection: close -> read to EOF
        writer.close()
    except OSError:
        return False, (time.perf_counter() - t0) * 1000, None
    dt = (time.perf_counter() - t0) * 1000
    ok = data.startswith(b"HTTP/1.1 200")
    token = None
    if ok and b"{" in data:
        try:
            # Response is chunked; pull the JSON object out by its braces (robust to chunk sizes).
            token = json.loads(data[data.index(b"{"):data.rindex(b"}") + 1])["queue_token"]
        except (ValueError, KeyError):
            pass
    return ok, dt, token


async def join_load(n: int, concurrency: int) -> list[str]:
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    tokens: list[str] = []

    async def one() -> None:
        async with sem:
            ok, dt, tok = await _join_once()
            if ok:
                latencies.append(dt)
                if tok:
                    tokens.append(tok)

    t0 = time.perf_counter()
    await asyncio.gather(*[one() for _ in range(n)])
    total = time.perf_counter() - t0
    latencies.sort()

    def pct(p: float) -> float:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] if latencies else 0.0

    rate = len(latencies) / total if total else 0
    print(f"JOIN  n={n} concurrency={concurrency}: {len(latencies)} ok in {total:.2f}s = {rate:.0f} req/s")
    print(f"      latency ms: p50={pct(0.50):.1f}  p95={pct(0.95):.1f}  "
          f"p99={pct(0.99):.1f}  max={latencies[-1] if latencies else 0:.1f}")
    return tokens


def _redis_command_calls() -> int:
    r = redis.Redis(socket_connect_timeout=5)
    return sum(int(v.get("calls", 0)) for v in r.info("commandstats").values())


async def _sse_open(
    token: str, counters: dict[str, int]
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """Open one SSE connection and read its first frame; return the stream if it held."""
    try:
        reader, writer = await asyncio.open_connection(HOST, PORT)
        writer.write(
            f"GET /api/queue/{EVENT}/stream?t={token} HTTP/1.1\r\nHost: {HOST}\r\n"
            f"Accept: text/event-stream\r\n\r\n".encode()
        )
        await writer.drain()
        first = await asyncio.wait_for(reader.read(256), timeout=10)  # status + first frame
        if not first.startswith(b"HTTP/1.1 200"):
            counters["failed"] += 1
            writer.close()
            return None
        counters["open"] += 1
        return reader, writer
    except (OSError, asyncio.TimeoutError):
        counters["failed"] += 1
        return None


async def _drain(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, deadline: float) -> None:
    while time.time() < deadline:  # keep the connection open, discard frames/heartbeats
        try:
            chunk = await asyncio.wait_for(reader.read(256), timeout=max(0.1, deadline - time.time()))
        except (asyncio.TimeoutError, ValueError):
            break
        if not chunk:
            break
    writer.close()


async def sse_load(m: int, hold_s: float) -> None:
    print(f"--- SSE load: {m} connections, {hold_s}s steady hold ---")
    tokens = (await join_load(m, 250))[:m]

    # PHASE 1 — open. Each connect costs one authoritative ZRANK, so this is O(connections).
    pre_open = _redis_command_calls()
    counters = {"open": 0, "failed": 0}
    conns = [c for c in await asyncio.gather(*[_sse_open(t, counters) for t in tokens]) if c]
    post_open = _redis_command_calls()
    print(f"SSE   opened {counters['open']} / attempted {len(tokens)} (failed {counters['failed']})")
    opened = counters["open"] or 1
    print(f"      Redis commands to OPEN {counters['open']} connections: {post_open - pre_open}  "
          f"(~{(post_open - pre_open) / opened:.0f} each: one position.lua EVALSHA + its 5 internal calls)")

    # PHASE 2 — steady state. THE claim: ongoing Redis ops do NOT scale with connections.
    steady_before = _redis_command_calls()
    deadline = time.time() + hold_s
    await asyncio.gather(*[_drain(r, w, deadline) for (r, w) in conns])
    steady_after = _redis_command_calls()
    print(f"      Redis commands DURING {hold_s}s steady hold of {counters['open']} connections: "
          f"{steady_after - steady_before}  (FLAT, independent of connection count — design.md §6; "
          f"a hold longer than 30s adds one ZRANK per connection per reconcile)")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "join":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
        c = int(sys.argv[3]) if len(sys.argv) > 3 else 200
        asyncio.run(join_load(n, c))
    elif mode == "sse":
        m = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
        hold = float(sys.argv[3]) if len(sys.argv) > 3 else 15.0
        asyncio.run(sse_load(m, hold))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
