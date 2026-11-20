"""offer_match_quality.annotate on hand-made match rows.  python3 python/collectors/tests/test_offer_match_quality.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import offer_match_quality as Q  # noqa: E402


def row(txid, msg, sat, t=100, outcome="completed", first=50):
    return {"txid": txid, "msg_id": msg, "lock_sat": sat, "lock_time": t, "outcome": outcome, "first_seen": first}


class Annotate(unittest.TestCase):
    def test_unique_ambiguous_round(self):
        rows = Q.annotate([row("a", "m1", 123456), row("b", "m2", 500000), row("c", "m3", 777777), row("c", "m4", 777777), row("d", "m5", 100000), row("d", "m6", 100000)])
        q = {(r["txid"], r["msg_id"]): (r["n_candidates"], r["round_amount"], r["match_quality"]) for r in rows}
        self.assertEqual(q[("a", "m1")], (1, 0, "unique"))
        self.assertEqual(q[("b", "m2")], (1, 1, "round"))
        self.assertEqual(q[("c", "m3")], (2, 0, "ambiguous"))
        self.assertEqual(q[("d", "m6")], (2, 1, "ambiguous"))
        self.assertEqual(Q.summary(rows), {"unique": 1, "round": 1, "ambiguous": 2})

    def test_csv_strings_and_order(self):
        rows = Q.annotate([row("z", "m2", "1500.0", t="200", first="9"), row("z", "m1", "1500", t="200", first="9"), row("y", "m9", "42", t="100")])
        self.assertEqual([r["msg_id"] for r in rows], ["m9", "m1", "m2"])      # by lock_time, txid, first_seen, msg_id
        self.assertEqual(rows[1]["match_quality"], "ambiguous")

    def test_same_txid_other_outcome_is_separate(self):
        rows = Q.annotate([row("x", "m1", 1234), row("x", "m2", 1234, outcome="failed")])
        self.assertTrue(all(r["n_candidates"] == 1 for r in rows))


class LockSideAndUsd(unittest.TestCase):
    def test_lock_direction_follows_is_reverse_ads_bid(self):
        self.assertEqual(Q.lock_direction("BTC", "XMR", "BTC"), "reverse")     # XMR is scriptless: lock on coin_to
        self.assertEqual(Q.lock_direction("BTC", "BTC", "XMR"), "normal")
        self.assertEqual(Q.lock_direction("LTC", "BTC", "LTC"), None)          # BTC->LTC locks on BTC, never on LTC
        self.assertEqual(Q.lock_direction("BTC", "BTC", "LTC"), "normal")
        self.assertEqual(Q.lock_direction("LTC", "DOGE", "LTC"), "reverse")    # DOGE has no segwit
        self.assertEqual(Q.lock_direction("BTC", "XMR", "DOGE"), None)

    def test_usd_value_uses_the_locked_leg(self):
        px = {"BTC": 100000.0, "XMR": 400.0}.get
        self.assertEqual(Q.usd_value("BTC", 2_000_000, px, 5.0, "XMR", 0.02, "BTC"), 2000.0)
        self.assertEqual(Q.usd_value("PART", 10**8, px, 1.0, "XMR", 50.0, "PART"), 400.0)    # no PART price: the XMR leg
        self.assertIsNone(Q.usd_value("PART", 10**8, px, 1.0, "WOW", 50.0, "PART"))


if __name__ == "__main__":
    unittest.main()
