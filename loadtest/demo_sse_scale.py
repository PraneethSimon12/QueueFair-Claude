"""Cross-replica SSE fan-out demo (v2 horizontal scaling).

Opens N SSE streams through Caddy (:8080), which round-robins them across the scaled queue replicas.
A single admitter process (on one replica) then admits the batch, PUBLISHing once to Redis. If every
stream — on whichever replica accepted it — receives an `admitted` frame, the fan-out has crossed
replica boundaries: each replica's own subscriber received the one broadcast and delivered it to its
local connections. That is the property that makes the stateless design scale (design.md §8, §9).

    python demo_sse_scale.py            # 6 streams, hold 12s
"""

import asyncio

HOST, PORT = "127.0.0.1", 8080  # Caddy
EVENT = "coldplay-mumbai-2026"
TOKENS = [f"{i:032x}" for i in range(1, 7)]  # 6 valid 32-hex queue tokens, seeded into Redis
HOLD = 12.0


async def one(token: str) -> tuple[str, bool, int]:
    reader, writer = await asyncio.open_connection(HOST, PORT)
    writer.write(
        (
            f"GET /api/queue/{EVENT}/stream?t={token} HTTP/1.1\r\n"
            f"Host: {HOST}\r\nAccept: text/event-stream\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    loop = asyncio.get_event_loop()
    end = loop.time() + HOLD
    data = b""
    while loop.time() < end:
        try:
            chunk = await asyncio.wait_for(reader.read(1024), timeout=max(0.1, end - loop.time()))
        except (asyncio.TimeoutError, ValueError):
            break
        if not chunk:
            break
        data += chunk
        if b"admitted" in data:
            break
    writer.close()
    return token, (b"admitted" in data), data.count(b"position")


async def main() -> None:
    results = await asyncio.gather(*[one(t) for t in TOKENS])
    admitted = sum(1 for _, a, _ in results if a)
    print(f"opened {len(results)} SSE streams through Caddy (:8080), distributed across replicas")
    for token, got, n_pos in results:
        print(f"  ...{token[-4:]}: admitted_frame={got}  position_frames={n_pos}")
    print(f"=> {admitted}/{len(results)} streams received an 'admitted' frame after one admission")


if __name__ == "__main__":
    asyncio.run(main())
