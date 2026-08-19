"""Deriving the client identity the join limiter counts against. Pure string logic — no request
object, no Django, no IO — so the X-Forwarded-For trust rule can be tested against hand-written
header values.

THE TRAP THIS EXISTS TO AVOID (why this is not just `request.META['REMOTE_ADDR']`)
    Behind a reverse proxy, REMOTE_ADDR is the *proxy's* address — every client looks identical, so
    a per-client limit keyed on it throttles everyone as one. The real client IP is in
    X-Forwarded-For. But XFF is a plain request header the CLIENT can set: if we trusted the value
    the client sent, an abuser would spoof a fresh X-Forwarded-For per request and never hit the
    limit. So the rule is not "read XFF" — it is "read only the part of XFF a trusted proxy added".
"""


def client_id_from(remote_addr: str, forwarded_for: str | None, *, trust_proxy: bool) -> str:
    """The identity the join rate limit is counted against.

    - trust_proxy=False (no proxy in front, e.g. local dev / direct exposure): use REMOTE_ADDR. XFF
      is ignored entirely, because with no trusted proxy every byte of it is attacker-controlled.
    - trust_proxy=True (exactly one trusted proxy in front, e.g. Caddy): the real peer is the
      RIGHTMOST X-Forwarded-For entry. A proxy APPENDS the address it received the connection from,
      so with one hop `XFF = "<whatever the client sent>, <real client ip>"` and the last element is
      the only one the proxy vouches for. Taking the leftmost — the "original client" the header
      claims — is the spoofable value and the bug. (N trusted proxies would mean the Nth from the
      right; we run one.)

    Falls back to "unknown" only if there is genuinely nothing to key on, so a missing address
    groups those requests together rather than crashing the join path.
    """
    if trust_proxy and forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return remote_addr or "unknown"
