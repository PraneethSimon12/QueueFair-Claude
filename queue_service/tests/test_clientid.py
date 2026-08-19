"""client_id_from in isolation. No request object, no Django. The X-Forwarded-For trust rule is a
security boundary — get it wrong and the join rate limit is bypassable by spoofing a header — so it
is tested against hand-written header strings, each naming what breaks.
"""

import unittest

from core.clientid import client_id_from


class NoTrustedProxyTests(unittest.TestCase):
    def test_uses_remote_addr(self) -> None:
        self.assertEqual(client_id_from("203.0.113.7", None, trust_proxy=False), "203.0.113.7")

    def test_ignores_forwarded_for_entirely(self) -> None:
        """With no trusted proxy in front, every byte of X-Forwarded-For is attacker-controlled.
        Reading it here would let a directly-exposed service be fooled by a spoofed header."""
        self.assertEqual(
            client_id_from("203.0.113.7", "1.2.3.4", trust_proxy=False), "203.0.113.7"
        )


class TrustedProxyTests(unittest.TestCase):
    def test_takes_the_rightmost_hop(self) -> None:
        """One proxy (Caddy) appends the real peer, so the last entry is the one it vouches for."""
        self.assertEqual(
            client_id_from("10.0.0.2", "198.51.100.9", trust_proxy=True), "198.51.100.9"
        )

    def test_spoofed_leftmost_is_ignored(self) -> None:
        """The attack: the client sends `X-Forwarded-For: <victim>`; Caddy appends the real client.
        Trusting the leftmost would count the request against the spoofed value and bypass the
        limit. The rightmost is the real client and the only one that must be used."""
        xff = "9.9.9.9, 198.51.100.9"  # "9.9.9.9" is client-supplied; the proxy appended the real ip
        self.assertEqual(client_id_from("10.0.0.2", xff, trust_proxy=True), "198.51.100.9")

    def test_missing_forwarded_for_falls_back_to_remote_addr(self) -> None:
        """A trusted proxy that somehow sent no XFF must not crash the join path."""
        self.assertEqual(client_id_from("10.0.0.2", None, trust_proxy=True), "10.0.0.2")

    def test_whitespace_and_empty_hops_are_tolerated(self) -> None:
        self.assertEqual(
            client_id_from("10.0.0.2", " 9.9.9.9 ,  , 198.51.100.9 ", trust_proxy=True),
            "198.51.100.9",
        )

    def test_forwarded_for_of_only_commas_falls_back(self) -> None:
        self.assertEqual(client_id_from("10.0.0.2", " , ,", trust_proxy=True), "10.0.0.2")


class FallbackTests(unittest.TestCase):
    def test_nothing_to_key_on_becomes_unknown(self) -> None:
        """A missing address groups such requests as one 'unknown' bucket rather than crashing —
        those requests still get rate-limited together, which is the safe direction."""
        self.assertEqual(client_id_from("", None, trust_proxy=False), "unknown")
        self.assertEqual(client_id_from("", None, trust_proxy=True), "unknown")

    def test_ipv6_passes_through(self) -> None:
        self.assertEqual(client_id_from("2001:db8::1", None, trust_proxy=False), "2001:db8::1")


if __name__ == "__main__":
    unittest.main()
