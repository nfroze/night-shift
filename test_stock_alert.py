"""Offline tests. Run with:  python3 -B -m unittest   (from this folder)

Nothing here talks to Apple or Telegram. The watcher tests drive the real HTTP
code against a local fake server on 127.0.0.1, with the watcher's clock and
randomness replaced, so no test sleeps through the intervals it checks. Only
the deadline tests use real time, because a real deadline is their subject.

The stand-in for Apple's lookup follows what live responses showed: ask about
one model, get up to three similar in-stock models (same capacity first),
listed only for the nearest store that has any, never the model asked about.

Test names carry the rule they guard, M1 to M8, as listed in the facts sheet.
"""
import contextlib
import email.utils
import http.server
import io
import json
import math
import os
import sys
import tempfile
import threading
import time
import unittest
import unicodedata
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so it also runs as  python3 -I -m unittest  or from another folder

import stock_alert as w

E, G, D, F = "MK2E4QN/A", "MK2G4QN/A", "MK2D4QN/A", "MK2F4QN/A"
DUO = {D: 256, E: 256, F: 512, G: 512}
A, B = "R341", "R423"                       # Apple Eldon Square, Apple Metrocentre
A_PC, B_PC = "NE1 7XD", "NE11 9YG"
ORDER = {A_PC: [A, B, "R328"], B_PC: [B, A, "R328"]}
EXAMPLE = {"stores": [A, B], "models": [E, G]}
E_AT_A = "IN STOCK: iPhone Duo 256GB Night Sky\nApple Eldon Square"
E_AT_B = "IN STOCK: iPhone Duo 256GB Night Sky\nApple Metrocentre"
STORES = w.load_data("stores_uk.json")["stores"]
BY_POSTCODE = {s["postcode"]: n for n, s in STORES.items()}
RECOVERED = "Stock watcher is reading everything again."


def apple_answer(shelves, ask_part, postcode, quote="Available Today"):
    ranked = sorted((p for p in DUO if p != ask_part), key=lambda p: (DUO[p] != DUO[ask_part], p))
    stores, listed = [], False
    for num in ORDER.get(postcode, [BY_POSTCODE.get(postcode)]):
        parts = {}
        if not listed:
            have = [p for p in ranked if p in shelves.get(num, ())][:3]
            parts = {p: {"pickupDisplay": "available", "pickupSearchQuote": quote,
                         "messageTypes": {"regular": {"storePickupProductTitle": "title of " + p}}}
                     for p in have}
            listed = bool(parts)
        stores.append({"storeNumber": num, "storeName": num, "partsAvailability": parts})
    return {"body": {"PickupMessage": {"stores": stores}}}


class Midpoint:
    """Stands in for the watcher's random numbers: always the middle of the range."""

    def uniform(self, a, b):
        return (a + b) / 2


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.pauses = []

    def now(self):
        return self.t

    def pause(self, seconds):
        assert math.isfinite(seconds) and seconds >= 0, seconds
        self.pauses.append(seconds)
        self.t += seconds


# ---- the fake server -------------------------------------------------------

class FakeServer:
    """Apple's pickup lookup and Telegram's Bot API, on 127.0.0.1.

    apple(postcode, product) returns a dict (sent as JSON), or a tuple
    (status, headers, body bytes), or a function taking the request handler.
    telegram is "up", "down" (HTTP 502) or "slow" (accepted, answer too late).
    apple_delay moves the fake clock on by that many seconds per Apple request.
    """

    def __init__(self):
        self.reset()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, body, headers=()):
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                if not url.path.endswith("/shop/pickup-message-recommendations"):
                    return self.reply(404, b"not here")
                fake.events.append(("apple", fake.t(), q.get("location"), q.get("product")))
                answer = fake.apple(q.get("location"), q.get("product"))
                if fake.clock and fake.apple_delay:
                    fake.clock.t += fake.apple_delay
                if callable(answer):
                    return answer(self)
                if isinstance(answer, (dict, list)):
                    return self.reply(200, json.dumps(answer).encode(), [("Content-Type", "application/json")])
                status, headers, body = answer
                return self.reply(status, body, headers)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                params = dict(urllib.parse.parse_qsl(body))
                method = self.path.rsplit("/", 1)[-1]
                fake.telegram_paths.append(self.path)
                if method == "sendMessage":
                    fake.attempts.append(params.get("text"))
                    if fake.telegram == "down":
                        return self.reply(502, b'{"ok": false, "description": "Bad Gateway"}')
                    fake.events.append(("send", fake.t(), params.get("text")))
                    if fake.telegram == "slow":
                        time.sleep(fake.slow)
                    with contextlib.suppress(OSError):
                        self.reply(200, b'{"ok": true, "result": {}}')
                    return
                if method == "getUpdates":
                    fake.update_calls.append(params)
                    result = fake.updates(params)
                    if fake.clock and "timeout" in params and not result:
                        fake.clock.t += float(params["timeout"])  # a long poll that returned empty-handed
                    return self.reply(200, json.dumps({"ok": True, "result": result}).encode())
                return self.reply(404, b'{"ok": false, "description": "Not Found"}')

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda request, address: None   # clients cut off on purpose
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.apple = lambda postcode, product: apple_answer({}, product, postcode)
        self.telegram, self.slow, self.apple_delay = "up", 0.8, 0
        self.events, self.attempts, self.telegram_paths = [], [], []
        self.updates, self.update_calls = (lambda params: []), []
        self.clock = None

    def t(self):
        return self.clock.t if self.clock else 0

    @property
    def sent(self):
        return [(e[1], e[2]) for e in self.events if e[0] == "send"]

    @property
    def apple_requests(self):
        return [e[1:] for e in self.events if e[0] == "apple"]

    def texts(self):
        return [t for _, t in self.sent]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def trickle_body(handler, total=1000, every=0.05):
    """Promise a body, then send it one byte at a time."""
    with contextlib.suppress(OSError):
        handler.send_response(200)
        handler.send_header("Content-Length", str(total))
        handler.end_headers()
        for _ in range(total):
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(every)


def trickle_headers(handler, every=0.05):
    with contextlib.suppress(OSError):
        for ch in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 1000:
            handler.wfile.write(bytes([ch]))
            handler.wfile.flush()
            time.sleep(every)
    handler.close_connection = True


def trickle_chunked_close(handler, every=0.05):
    """Connection: close with chunked encoding, the chunk-size line one byte at a time."""
    with contextlib.suppress(OSError):
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n")
        handler.wfile.flush()
        for ch in b"0" * 200 + b"2\r\n{}\r\n0\r\n\r\n":
            handler.wfile.write(bytes([ch]))
            handler.wfile.flush()
            time.sleep(every)
    handler.close_connection = True


class Toy:
    """A search with no shop behind it: behaviour() returns covered keys in stock, or raises."""

    def __init__(self, key, clock, behaviour=lambda: [], calls=None):
        self.key, self.clock, self.behaviour = key, clock, behaviour
        self.covers = {("toy-shop", key): ("Toy Shop", f"item {key}")}
        self.calls = [] if calls is None else calls

    def __str__(self):
        return f"toy {self.key}"

    def run(self):
        self.calls.append(self.clock.t)
        return [w.Hit(k, f"{k}", f"IN STOCK: {k[1]}") for k in self.behaviour()]


class ServerTest(unittest.TestCase):
    """Base: a fake server, a fake clock and the module pointed at both."""

    @classmethod
    def setUpClass(cls):
        cls.server = FakeServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.server.reset()
        self.clock = Clock()
        self.server.clock = self.clock
        self.logged = []
        patch = {"APPLE_BASE": self.server.base, "TELEGRAM_BASE": self.server.base,
                 "BOT_TOKEN": "123:test", "CHAT_ID": "42", "now": self.clock.now, "pause": self.clock.pause,
                 "rng": Midpoint(), "log": self.logged.append, "HTTP_TIMEOUT": 2}
        for name, value in patch.items():
            self.addCleanup(setattr, w, name, getattr(w, name))
            setattr(w, name, value)

    def make(self, raw=None):
        self.cfg = w.Config(dict(raw or EXAMPLE))
        self.watcher = w.Watcher(w.apple_searches(self.cfg), self.cfg.poll_seconds, self.cfg.remind_seconds,
                                 self.cfg.warn_after_seconds, self.cfg.messages_per_alert)
        self.start = self.clock.t
        return self.watcher

    def step(self, fail=None, shelves=None, telegram="up"):
        """One cycle. fail: None, "A", "B", "ALL" (HTTP 503 for those searches)
        or a function (postcode, product) -> bool. Returns the texts delivered."""
        shelves = shelves or {}

        def apple(postcode, product):
            failing = fail(postcode, product) if callable(fail) else (
                fail == "ALL" or (fail == "A" and postcode == A_PC) or (fail == "B" and postcode == B_PC))
            return (503, [], b"Service Unavailable") if failing else apple_answer(shelves, product, postcode)
        self.server.apple = apple
        self.server.telegram = telegram
        before = len(self.server.sent)
        self.clock.pause(self.watcher.cycle())
        return [t for _, t in self.server.sent[before:]]

    def run_steps(self, schedule):
        """schedule: list of (fail, shelves). Returns [(cycle number, seconds since start, text)]."""
        out = []
        for n, (fail, shelves) in enumerate(schedule, 1):
            t = self.clock.t - self.start
            out += [(n, t, text) for text in self.step(fail, shelves)]
        return out

    def run_until(self, seconds):
        """Cycles with the server as it is, until `seconds` after start. Returns [(t, text)]."""
        before = len(self.server.sent)
        while self.clock.t - self.start < seconds:
            self.clock.pause(self.watcher.cycle())
        return [(round(t - self.start, 1), text) for t, text in self.server.sent[before:]]


# ---- M1: small by construction ---------------------------------------------

class SizeAndPacingTest(ServerTest):
    def test_M1_at_most_12_searches(self):
        stores = list(STORES)
        self.assertEqual(len(w.Config({"stores": stores[:6], "models": [E, G]}).searches), 12)
        for raw, n in (({"stores": stores[:13], "models": [E]}, 13), ({"stores": stores[:7], "models": [E, G]}, 14)):
            with self.subTest(n=n), self.assertRaises(w.ConfigError) as caught:
                w.Config(raw)
            message = str(caught.exception)
            self.assertIn(f"needs {n} searches", message)
            self.assertIn("limit is 12", message)
            self.assertIn("48 requests a minute", message)
        with self.assertRaises(w.ConfigError):
            w.Watcher([Toy(n, self.clock) for n in range(13)], 30, 180, 900)

    def test_M1_interval_rule(self):
        for poll, n, expected in ((30, 4, 30), (5, 4, 5), (5, 8, 10), (5, 12, 15), (30, 12, 30), (100, 12, 100),
                                  (1, 1, 5)):
            with self.subTest(poll=poll, n=n):
                self.assertEqual(w.effective_interval(poll, n), expected)
        stores = list(STORES)
        cfg = w.Config({"stores": stores[:6], "models": [E, G]})
        self.assertEqual((cfg.interval, cfg.warn_after_seconds), (30, 900))
        cfg = w.Config({"stores": stores[:6], "models": [E, G], "poll_seconds": 5})
        self.assertEqual((cfg.interval, cfg.warn_after_seconds), (15, 900))
        cfg = w.Config({"stores": stores[:2], "models": [E, G], "poll_seconds": 5})
        self.assertEqual(cfg.interval, 5)                      # the owner's own production rate
        with self.assertRaises(w.ConfigError):                 # never stale between two checks
            w.Config({"stores": [A], "models": [E], "poll_seconds": 100, "warn_after_seconds": 150})
        self.assertEqual(w.Watcher([Toy(n, self.clock) for n in range(12)], 5, 180, 900).interval, 15)

    def test_M1_identical_requests_are_sent_once(self):
        self.make({"stores": [{"number": A, "name": "One", "postcode": A_PC},
                              {"number": "R328", "name": "Three", "postcode": A_PC}, B], "models": [E]})
        self.assertEqual(self.cfg.searches, [(D, A_PC, [A, "R328"]), (D, B_PC, [B])])
        self.step()
        self.assertEqual(sorted((pc, p) for _, pc, p in self.server.apple_requests), [(A_PC, D), (B_PC, D)])

    def test_M1_average_rate_stays_at_or_under_48_a_minute(self):
        calls = []
        flaky = {"n": 0}

        def sometimes_failing():
            flaky["n"] += 1
            if flaky["n"] % 50 < 13:   # now and then a whole check fails: backoff must not speed anything up
                raise w.CheckFailed("down")
            return []
        for n, poll in ((12, 5), (4, 5), (12, 30)):
            calls.clear()
            start = self.clock.t
            watcher = w.Watcher([Toy(i, self.clock, sometimes_failing, calls) for i in range(n)], poll, 180, 900)
            while self.clock.t < start + 3600:
                self.clock.pause(watcher.cycle())
            with self.subTest(n=n, poll=poll):
                self.assertGreater(len(calls), 3600 / 60 * min(48, n * 60 / poll) * 0.6)
                self.assertLessEqual(len(calls) / ((calls[-1] - calls[0]) / 60), 48)       # the average
                self.assertGreaterEqual(min(b - a for a, b in zip(calls, calls[1:])), w.REQUEST_GAP_SECONDS)
                # checks start at least one interval apart: a minute holds at most 60 / interval + 1 of them
                worst = max(sum(1 for c in calls if s <= c < s + 60) for s in calls)
                self.assertLessEqual(worst, (60 / watcher.interval + 1) * n)

    def test_M1_once_uses_the_same_pacing(self):
        cfg = w.Config({"stores": list(STORES)[:6], "models": [E, G]})
        self.server.apple = lambda pc, p: apple_answer({}, p, pc)
        with contextlib.redirect_stdout(io.StringIO()):
            w.cmd_once(cfg)
        self.assertEqual(len(self.server.apple_requests), 12)
        self.assertEqual(self.clock.pauses, [w.REQUEST_GAP_SECONDS] * 11)


# ---- config (M8) -----------------------------------------------------------

class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.logged = []
        self.addCleanup(setattr, w, "log", w.log)
        w.log = self.logged.append

    def bad(self, raw):
        with self.assertRaises(w.ConfigError) as caught:
            w.Config(raw)
        return str(caught.exception)

    def test_example_config_resolves_from_bundled_lists(self):
        cfg = w.load_config(os.path.join(HERE, "config.example.json"))
        self.assertEqual(cfg.endpoint, w.APPLE_BASE + "/uk/shop/pickup-message-recommendations")
        self.assertEqual(cfg.stores, {A: "Apple Eldon Square", B: "Apple Metrocentre"})
        self.assertEqual(cfg.postcodes, {A: A_PC, B: B_PC})
        self.assertEqual(cfg.models, {E: "iPhone Duo 256GB Night Sky", G: "iPhone Duo 512GB Night Sky"})
        self.assertEqual(cfg.ask_for, {E: D, G: F})           # twins: same size, other colour
        self.assertEqual(cfg.searches, [(D, A_PC, [A]), (F, A_PC, [A]), (D, B_PC, [B]), (F, B_PC, [B])])
        self.assertEqual(cfg.buy_urls[G], "https://www.apple.com/uk/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky")
        self.assertEqual((cfg.poll_seconds, cfg.remind_seconds, cfg.warn_after_seconds, cfg.interval), (30, 180, 900, 30))

    def test_custom_entries(self):
        custom = w.Config({"stores": [A, {"number": "R999", "name": "Apple Somewhere", "postcode": "ZZ1 1ZZ"}],
                           "models": [E, {"part": "AAAA", "name": "Something", "ask_about": "BBBB"}]})
        self.assertEqual(custom.postcodes["R999"], "ZZ1 1ZZ")
        self.assertEqual(custom.ask_for["AAAA"], "BBBB")
        self.assertEqual(custom.buy_urls["AAAA"], "https://www.apple.com/uk/shop")

    def test_M8_bad_configs_are_refused(self):
        for raw in ({}, [], {"stores": [A], "models": []}, {"stores": [], "models": [E]},
                    {"stores": [A], "models": [{"part": "A", "name": "a", "ask_about": "A"}]},  # twin must differ
                    {"stores": [{"number": "R1"}], "models": [E]},
                    {"stores": [7], "models": [E]}, {"stores": A, "models": [E]},
                    {"stores": [A, A], "models": [E]}, {"stores": [A], "models": [E, E]},
                    {"stores": [A], "models": [{"part": "X", "name": "x", "ask_about": "Y", "buy_url": "javascript:x"}]},
                    {"stores": [A], "models": [E], "country_path": "@evil.example"},
                    {"stores": [A], "models": [E], "country_path": ""},
                    {"stores": [A], "models": [E], "messages_per_alert": 0},
                    {"stores": [A], "models": [E], "messages_per_alert": 6},
                    {"stores": [A], "models": [E], "messages_per_alert": 1.5},
                    {"stores": [A], "models": [E], "messages_per_alert": True}):
            with self.subTest(raw=raw):
                self.bad(raw)

    def test_M8_numbers_must_be_finite_and_within_ceilings(self):
        for key, (_, _, ceiling) in w.SECONDS.items():
            for value in (math.nan, math.inf, -math.inf, -5, 0, "30", "nan", True, None, [30], 1e10, ceiling + 1):
                with self.subTest(key=key, value=value):
                    self.assertIn(key, self.bad({"stores": [A], "models": [E], key: value}))
            w.Config({"stores": [A], "models": [E], key: ceiling})

    def test_M8_unknown_identifiers_and_settings_are_named(self):
        message = self.bad({"stores": [A, "R000", "R001"], "models": [E]})
        self.assertIn('"R000"', message)
        self.assertIn('"R001"', message)
        message = self.bad({"stores": [A], "models": [E, "NOPE", "ALSO/NOPE"]})
        self.assertIn('"NOPE"', message)
        self.assertIn('"ALSO/NOPE"', message)
        self.assertIn('"pol_seconds"', self.bad({"stores": [A], "models": [E], "pol_seconds": 30}))
        w.Config({"stores": [A], "models": [E], "_comment": "keys starting with _ are notes"})

    def test_M8_floors_are_raised_and_logged(self):
        cfg = w.Config({"stores": [A], "models": [E], "poll_seconds": 2, "remind_seconds": 1})
        self.assertEqual((cfg.poll_seconds, cfg.remind_seconds, cfg.warn_after_seconds), (5, 60, 900))
        self.assertEqual(len(self.logged), 2)
        self.assertTrue(all("below the minimum" in line for line in self.logged), self.logged)

    def test_M8_warn_after_floor_covers_one_slow_check(self):
        per_search = w.HTTP_TIMEOUT + w.REQUEST_GAP_SECONDS          # 15.25: a read that takes its whole deadline
        for stores, n, floor in ((list(STORES)[:2], 4, 121), (list(STORES)[:6], 12, 243)):
            with self.subTest(searches=n):
                self.assertEqual(floor, max(120, 2 * 30 + n * per_search))
                raw = {"stores": stores, "models": [E, G]}
                for below in (10, 120, floor - 0.5):
                    message = self.bad(dict(raw, warn_after_seconds=below))
                    self.assertIn(f'"warn_after_seconds" must be at least {floor} seconds', message)
                    self.assertNotIn("\n", message)
                self.assertEqual(w.Config(dict(raw, warn_after_seconds=floor)).warn_after_seconds, floor)
        self.assertEqual(self.logged, [])                             # refused, never quietly raised
        for poll in (30, w.SECONDS["poll_seconds"][2]):                 # the default 900 starts for every allowed config
            cfg = w.Config({"stores": list(STORES)[:6], "models": [E, G], "poll_seconds": poll})
            self.assertEqual(cfg.warn_after_seconds, 900)
            self.assertLessEqual(w.min_warn_after(cfg.interval, 12), 900)

    def test_M8_unreadable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            for content in (b'{"stores": ["R341"], "models": ["MK2E4QN/A"], "remind_seconds": NaN}',
                            b'{"stores": ["R341"], "models": ["MK2E4QN/A"], "poll_seconds": Infinity}',
                            b"\xff\xfe not text", b"", b'{"stores": [', b"[" * 100000):
                with open(path, "wb") as f:
                    f.write(content)
                with self.subTest(content=content[:30]), self.assertRaises(w.ConfigError):
                    w.load_config(path)
            with self.assertRaises(w.ConfigError) as caught:
                w.load_config(tmp)                          # a directory
            self.assertIn("cannot read", str(caught.exception))
            self.assertNotIn("\n", str(caught.exception))
            with self.assertRaises(w.ConfigError) as caught:
                w.load_config(os.path.join(tmp, "missing.json"))
            self.assertIn("Copy config.example.json", str(caught.exception))


class BundledDataTest(unittest.TestCase):
    def test_bundled_data_is_self_consistent(self):
        db = w.load_data("stores_uk.json")
        self.assertEqual(len(db["stores"]), 39)
        self.assertNotIn("number_unknown", db)
        self.assertEqual((db["stores"]["R762"]["name"], db["stores"]["R329"]["name"]),
                         ("Apple Battersea", "Apple Brompton Road"))
        self.assertTrue(all(k[0] == "R" and k[1:].isdigit() for k in db["stores"]))
        self.assertTrue(all(s["name"].startswith("Apple ") and s["postcode"] for s in db["stores"].values()))
        self.assertEqual(len({s["postcode"] for s in db["stores"].values()}), 39)   # no two share a postcode
        models = w.load_data("models_uk.json")["products"]["iphone-duo"]["variants"]
        self.assertTrue(all(v["source_checked"] for v in models))
        idx = w.model_index(w.load_data("models_uk.json"))
        self.assertEqual(len(idx), 8)
        self.assertTrue(all(v["twin"] in idx and v["twin"] != k for k, v in idx.items()))


# ---- detection (no network): M6 -------------------------------------------

class DetectionTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, w, "fetch", w.fetch)
        self.cfg = w.Config(dict(EXAMPLE))

    def found(self, shelves):
        w.fetch = lambda url, ask, postcode: apple_answer(shelves, ask, postcode)
        return {hit.key for search in w.apple_searches(self.cfg) for hit in search.run()}

    def test_detection(self):
        self.assertEqual(self.found({}), set())
        self.assertEqual(self.found({"R328": [E, G]}), set())       # stock only at a store you don't watch
        self.assertEqual(self.found({A: [D, F]}), set())            # only the twins in stock: no alert
        self.assertEqual(self.found({A: [E]}), {(A, E)})
        self.assertEqual(self.found({B: [G]}), {(B, G)})
        # A nearer store with other models must not hide the store behind it.
        self.assertEqual(self.found({A: [F], B: [E]}), {(B, E)})
        self.assertEqual(self.found({B: [D], A: [G]}), {(A, G)})
        self.assertEqual(self.found({A: list(DUO), B: list(DUO)}), {(A, E), (A, G), (B, E), (B, G)})

    def test_a_search_reports_only_what_it_covers(self):
        w.fetch = lambda url, ask, postcode: apple_answer({A: [E, G]}, ask, postcode)
        for search in w.apple_searches(self.cfg):
            self.assertTrue({hit.key for hit in search.run()} <= set(search.covers), search)

    def parse_fails(self, payload):
        with self.assertRaises(w.CheckFailed) as caught:
            w.parse(payload, [A], [E])
        return str(caught.exception)

    def test_M6_missing_or_malformed_fields_fail_closed(self):
        for payload in ({}, [], "text", 7, None, {"body": "x"}, {"body": {}}, {"body": {"PickupMessage": {}}},
                        {"body": {"PickupMessage": {"stores": "no"}}},
                        {"body": {"PickupMessage": {"stores": [{"storeNumber": "R328"}]}}},   # searched store absent
                        {"body": {"PickupMessage": {"stores": [{"storeNumber": A}]}}},        # no partsAvailability
                        {"body": {"PickupMessage": {"stores": [{"storeNumber": A, "partsAvailability": None}]}}},
                        {"body": {"PickupMessage": {"stores": [{"storeNumber": A, "partsAvailability": []}]}}},
                        {"body": {"PickupMessage": {"stores": [
                            {"storeNumber": A, "partsAvailability": {E: {"pickupQuote": "x"}}}]}}},  # no status
                        {"body": {"PickupMessage": {"stores": [
                            {"storeNumber": A, "partsAvailability": {E: "available"}}]}}}):
            with self.subTest(payload=payload):
                self.parse_fails(payload)

    def test_M6_unknown_status_string_fails_the_search(self):
        for status in ("AVAILABLE", "limited", "", 1, ["available"]):
            with self.subTest(status=status):
                payload = {"body": {"PickupMessage": {"stores": [
                    {"storeNumber": A, "partsAvailability": {D: {"pickupDisplay": status}}}]}}}
                self.assertIn("unknown pickup status", self.parse_fails(payload))

    def test_M6_known_negative_statuses_and_empty_lists_are_valid(self):
        for parts in ({}, {E: {"pickupDisplay": "unavailable"}}, {E: {"pickupDisplay": "ineligible"}}):
            payload = {"body": {"content": {"pickupMessage": {"stores": [{"storeNumber": A, "partsAvailability": parts}]}}}}
            self.assertEqual(w.parse(payload, [A], [E]), {})

    def test_M6_optional_fields_never_discard_a_hit(self):
        for info in ({"pickupDisplay": "available", "messageTypes": "schema changed"},
                     {"pickupDisplay": "available", "messageTypes": {"regular": "x"}},
                     {"pickupDisplay": "available", "pickupSearchQuote": None},
                     {"pickupDisplay": "available", "pickupSearchQuote": {"a": 1}},
                     {"pickupDisplay": "available"}):
            with self.subTest(info=info):
                payload = {"body": {"PickupMessage": {"stores": [{"storeNumber": A, "partsAvailability": {E: info}}]}}}
                titles = {}
                self.assertEqual(w.parse(payload, [A], [E], titles), {(A, E): "Available"})
                self.assertEqual(titles, {(A, E): E})

    def test_titles_are_only_built_for_once(self):
        self.addCleanup(setattr, w, "_title", w._title)

        def no_titles(info, part):
            raise AssertionError("the watcher must not build display titles")
        w._title = no_titles
        payload = apple_answer({A: [E]}, D, A_PC)
        self.assertEqual(w.parse(payload, [A], [E]), {(A, E): "Available Today"})

    def test_M6_remote_text_is_sanitised(self):
        quote = ("To\ud800day⁠­؜‮​﻿\n2000-01-01 Stock watcher is reading everything again."
                 "\r\x1b[31m " + "x" * 500)
        payload = apple_answer({A: [E]}, D, A_PC, quote=quote)
        payload["body"]["PickupMessage"]["stores"][0]["partsAvailability"][E]["messageTypes"]["regular"][
            "storePickupProductTitle"] = "Evil\ntitle\x07\udfff" + "y" * 300
        titles = {}
        found = w.parse(payload, [A], [E], titles)
        for text in (found[(A, E)], titles[(A, E)]):
            self.assertFalse([c for c in text if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp")], text)
            self.assertLessEqual(len(text), 80)
        self.assertTrue(found[(A, E)].startswith("Today 2000-01-01"))
        self.assertEqual(w.alert_text(self.cfg, A, E, found[(A, E)]).count("\n"), 2)


# ---- the HTTP layer against the fake server: M3, M5, M7 --------------------

class HttpTest(ServerTest):
    def fetch(self):
        return w.fetch(w.endpoint(), D, A_PC)

    def test_good_answer(self):
        self.assertEqual(self.fetch(), apple_answer({}, D, A_PC))
        self.assertEqual(self.server.apple_requests[0][1:], (A_PC, D))

    def test_http_errors_are_failed_checks(self):
        for answer in ((503, [], b"busy"), (500, [], b""), (301, [("Location", "http://x/")], b""),
                       (200, [], b"<html>blocked</html>"), (200, [], b'{"body": {"PickupMe'), (200, [], b"[" * 100000)):
            with self.subTest(answer=answer[:2]):
                self.server.apple = lambda pc, p: answer
                self.assertRaises(w.CheckFailed, self.fetch)

    def test_M5_throttle_signals_and_retry_after_forms(self):
        date = email.utils.formatdate(time.time() + 600, usegmt=True)
        for status, header, expected in ((429, "120", 120), (403, "7", 7), (429, None, None), (429, "soon", None),
                                         (429, "2.5", None), (429, "999999999", w.MAX_RETRY_AFTER_SECONDS),
                                         (503, "30", 30), (429, date, 600)):
            with self.subTest(status=status, header=header):
                headers = [("Retry-After", header)] if header else []
                self.server.apple = lambda pc, p: (status, headers, b"slow down")
                with self.assertRaises(w.Throttled) as caught:
                    self.fetch()
                if expected is None:
                    self.assertIsNone(caught.exception.retry_after)
                else:
                    self.assertAlmostEqual(caught.exception.retry_after, expected, delta=3)
        for headers in ([], [("Retry-After", "garbage")]):   # a 503 without a usable Retry-After is an ordinary failure
            self.server.apple = lambda pc, p: (503, headers, b"busy")
            with self.assertRaises(w.CheckFailed) as caught:
                self.fetch()
            self.assertNotIsInstance(caught.exception, w.Throttled)

    def test_cut_off_or_oversized_body_is_a_failed_check(self):
        def short(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "5000")
            handler.end_headers()
            handler.wfile.write(json.dumps(apple_answer({}, D, A_PC)).encode())
            handler.close_connection = True
        self.server.apple = lambda pc, p: short
        self.assertRaises(w.CheckFailed, self.fetch)
        self.addCleanup(setattr, w, "MAX_RESPONSE_BYTES", w.MAX_RESPONSE_BYTES)
        w.MAX_RESPONSE_BYTES = 1000
        self.server.apple = lambda pc, p: (200, [], b" " * 5000)
        self.assertRaises(w.CheckFailed, self.fetch)

    def test_M7_total_deadline_holds_for_every_transfer_mode(self):
        w.HTTP_TIMEOUT = 0.5
        for slow in (trickle_body, trickle_headers, trickle_chunked_close):
            with self.subTest(slow=slow.__name__):
                self.server.apple = lambda pc, p: slow
                started = time.monotonic()
                with self.assertRaises(w.CheckFailed) as caught:
                    self.fetch()
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertIn("within 0.5s", str(caught.exception))

    def test_M3_send_says_false_only_when_the_message_definitely_did_not_arrive(self):
        self.assertTrue(w.send("hello"))
        self.assertTrue(self.server.telegram_paths[0].startswith("/bot123:test/"))
        self.server.telegram = "down"                       # HTTP error: definitely not delivered
        self.assertFalse(w.send("lost"))
        self.server.telegram = "slow"                       # accepted, but the answer came too late
        w.HTTP_TIMEOUT = 0.3
        self.assertTrue(w.send("maybe"))
        self.assertIn("it may still have arrived", self.logged[-1])
        self.assertEqual(self.server.texts(), ["hello", "maybe"])
        self.addCleanup(setattr, w, "TELEGRAM_BASE", w.TELEGRAM_BASE)
        w.TELEGRAM_BASE = "http://127.0.0.1:9"              # connection refused: definitely not delivered
        self.assertFalse(w.send("x"))

    def test_M3_ok_false_is_not_delivery_and_long_text_is_cut(self):
        self.assertTrue(w.send("x" * 5000))
        self.assertEqual(len(self.server.texts()[0]), w.TELEGRAM_TEXT_LIMIT)
        original = self.server.httpd.RequestHandlerClass.do_POST

        def ok_false(handler):
            handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
            handler.reply(200, b'{"ok": false, "description": "nope"}')
        self.server.httpd.RequestHandlerClass.do_POST = ok_false
        self.addCleanup(setattr, self.server.httpd.RequestHandlerClass, "do_POST", original)
        self.assertFalse(w.send("x"))

    def test_M6_send_encodes_lone_surrogates_with_replacement(self):
        self.assertTrue(w.send("broken \ud800 text"))
        self.assertEqual(self.server.texts(), ["broken ? text"])

    def test_messages_per_alert_sends_copies(self):
        self.assertTrue(w.send("x", count=3))
        self.assertEqual(self.server.texts(), ["x"] * 3)
        self.assertEqual(self.clock.pauses, [1.5, 1.5])


# ---- the watch loop: M2 to M6 ----------------------------------------------

class WatcherTest(ServerTest):
    def test_M2_an_alert_goes_out_as_soon_as_its_search_returns(self):
        self.make()
        self.server.apple_delay = 10                         # every read takes 10 s
        self.step(shelves={A: [E]})                           # E at A is seen by the first search
        kinds = [e[0] for e in self.server.events]
        self.assertEqual(kinds, ["apple", "send", "apple", "apple", "apple"])
        self.assertEqual(self.server.sent[0][0] - self.start, 10)

    def test_M2_stamps_use_the_time_of_delivery_when_reads_are_slow(self):
        self.make({"stores": [A, B], "models": [E, G], "remind_seconds": 60})
        for delay in (14.9, 0, 14.9, 0, 0, 14.9, 0, 0, 0):
            self.server.apple_delay = delay
            self.step(shelves={B: [G]})                       # seen by the last search of each check
        times = [t for t, text in self.server.sent if text.startswith("IN STOCK")]
        self.assertGreater(len(times), 2)
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 60, times)

    def test_M2_slow_successful_reads_are_never_stale(self):
        stores = list(STORES)[:4]
        self.make({"stores": stores, "models": [E, G], "warn_after_seconds": 200})
        self.server.apple_delay = 14.9                        # 8 searches: one check lasts about 150 s
        self.assertEqual(self.run_until(3000), [])

    def test_M2_twelve_slow_searches_at_the_default_never_warn(self):
        self.make({"stores": list(STORES)[:6], "models": [E, G]})
        self.server.apple_delay = 14                          # 12 searches: one check lasts about 171 s
        self.assertEqual(self.run_until(3600), [])
        self.assertGreater(len(self.server.apple_requests), 12 * 20)

    def test_M2_first_check_counts_from_its_own_start(self):
        w.HTTP_TIMEOUT = 15
        cfg = w.Config({"stores": list(STORES)[:4], "models": [E, G], "poll_seconds": 5,
                        "warn_after_seconds": 142})   # 8 searches: the floor, 2 x 10 + 8 x 15.25
        self.server.apple_delay = 15                          # every read takes its whole deadline and succeeds
        note, send, stop = "Stock watcher started.", w.send, self.clock.t + 3600

        def slow_note(text, *args):
            if text == note:
                self.clock.t += 60                            # a slow startup note: DNS time is outside its deadline
            return send(text, *args)

        def pause(seconds):
            self.clock.pause(seconds)
            if self.clock.t > stop:
                raise KeyboardInterrupt
        w.send, w.pause, w.rng = slow_note, pause, type("Late", (), {"uniform": lambda self, a, b: b})()
        with self.assertRaises(KeyboardInterrupt):
            w.watch(w.apple_searches(cfg), cfg.poll_seconds, cfg.remind_seconds, cfg.warn_after_seconds, note=note)
        self.assertEqual(self.server.texts(), [note])        # longest start delay, slow note, slow checks: no warning
        self.assertEqual(len({(pc, p) for _, pc, p in self.server.apple_requests}), 8)

    def test_M2_warning_is_on_time_when_every_read_times_out(self):
        self.make({"stores": list(STORES)[:6], "models": [E, G]})
        self.server.apple_delay = 15
        self.server.apple = lambda pc, p: (503, [], b"")
        out = self.run_until(1500)
        self.assertEqual(len(out), 1, out)
        self.assertLessEqual(out[0][0], 900 + 1.15 * self.watcher.interval)   # even while backing off

    def test_M3_ambiguous_send_is_stamped_and_reminded(self):
        self.make()
        w.HTTP_TIMEOUT = 0.3
        self.step(shelves={B: [E]}, telegram="slow")          # accepted, answer lost
        out = self.run_steps([(None, {B: [E]})] * 9)          # about 290 s
        self.assertEqual(len(self.server.attempts), 2, self.server.attempts)
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(out[0][1], 180)               # the reminder, not a resend

    def test_M3_definite_failure_is_retried_next_cycle(self):
        self.make()
        self.assertEqual(self.step(shelves={B: [E]}, telegram="down"), [])
        delivered = self.step(shelves={B: [E]})
        self.assertEqual(len(delivered), 1)
        self.assertTrue(delivered[0].startswith(E_AT_B))
        self.assertEqual(self.step(shelves={B: [E]}), [])
        self.assertEqual(len(self.server.attempts), 2)

    def test_M3_no_queue_a_failed_alert_for_gone_stock_is_not_sent(self):
        self.make()
        self.step(shelves={B: [E]}, telegram="down")
        self.assertEqual(self.step(shelves={}), [])
        self.assertEqual(len(self.server.attempts), 1)

    def test_M3_stamp_is_cleared_only_by_a_good_read_of_the_same_search(self):
        self.make()
        a_d = lambda pc, p: (pc, p) == (A_PC, D)
        out = self.run_steps([(None, {A: [E]}),
                              (a_d, {}),             # A's search for E fails: E at A is not known to be gone
                              (None, {A: [E]}),      # still stamped: no second alert
                              (None, {}),            # a good read shows it gone
                              (None, {A: [E]})])     # so its return alerts afresh
        self.assertEqual([n for n, _, _ in out], [1, 5])

    def test_M3_reminders_follow_remind_seconds(self):
        self.make()
        out = self.run_steps([(None, {A: [E]})] * 14)   # about 420 s
        times = [t for _, t, _ in out]
        self.assertEqual(times[0], 0)
        self.assertEqual(len(times), 3)
        self.assertTrue(all(180 <= b - a < 180 + 33 for a, b in zip(times, times[1:])), times)

    def test_M4_staggered_recovery_sends_exactly_one_recovery(self):
        for raw in (EXAMPLE, {"stores": list(STORES)[:6], "models": [E, G]}):
            with self.subTest(searches=len(w.Config(raw).searches)):
                self.server.events.clear()
                self.make(raw)
                order = [s.key for s in self.watcher.searches]
                fixed_at = {key: 1000 + 120 * i for i, key in enumerate(order)}   # one more every 120 s
                self.server.apple = lambda pc, p: (
                    (503, [], b"down") if self.clock.t - self.start < fixed_at[(p, pc)] else apple_answer({}, p, pc))
                out = self.run_until(1000 + 120 * len(order) + 600)
                self.assertEqual(len(out), 2, out)
                self.assertTrue(out[0][1].startswith("Stock watcher can't read stock"), out)
                self.assertEqual(out[1][1], RECOVERED)
                self.assertGreaterEqual(out[1][0], max(fixed_at.values()))   # only once the last search reads again

    def test_M4_warning_lists_what_is_stale_then_nothing_in_between(self):
        self.make()
        out = self.run_steps([("A", {})] * 40 + [("ALL", {})] * 20 + [(None, {})] * 40)
        texts = [text for _, _, text in out]
        self.assertEqual(texts, [
            "Stock watcher can't read stock (no good answer for 15 min; last error: HTTP 503 from Apple). "
            "NOT watching these until this clears:\n"
            "- Apple Eldon Square: iPhone Duo 256GB Night Sky, iPhone Duo 512GB Night Sky\n"
            "It keeps trying and will tell you when it reads everything again.",
            RECOVERED])
        self.assertTrue(900 <= out[0][1] < 900 + 33)

    def test_M4_one_warning_names_every_store_that_failed_together(self):
        self.make({"stores": [A, B], "models": [E, G], "poll_seconds": 5, "warn_after_seconds": 120})
        self.server.apple_delay = 3
        self.step()                                           # last good reads 3.25 s apart, Eldon Square first
        self.server.apple_delay = 0
        self.server.apple = lambda pc, p: (503, [], b"down")   # both stores, from the same check on
        out = self.run_until(300)
        self.server.apple = lambda pc, p: apple_answer({}, p, pc)
        out += self.run_until(1500)
        self.assertEqual([text for _, text in out][1:], [RECOVERED])
        self.assertIn("- Apple Eldon Square: iPhone Duo 256GB Night Sky, iPhone Duo 512GB Night Sky\n", out[0][1])
        self.assertIn("- Apple Metrocentre: iPhone Duo 256GB Night Sky, iPhone Duo 512GB Night Sky\n", out[0][1])

    def test_M4_undelivered_health_messages_are_retried(self):
        self.make({"stores": [A, B], "models": [E, G], "warn_after_seconds": 120})
        for _ in range(6):
            self.step(fail="A", telegram="down")
        self.assertEqual(self.server.sent, [])
        self.assertTrue(self.server.attempts)
        self.assertEqual(len(self.step(fail="A")), 1)
        self.assertEqual(self.step(fail="A"), [])
        self.assertEqual(self.step(telegram="down"), [])
        self.assertEqual(self.step(), [RECOVERED])
        self.assertEqual(self.step(), [])

    def test_M4_short_failures_do_not_warn(self):
        self.make()
        self.assertEqual(self.run_steps([("ALL", {}), ("A", {}), ("ALL", {}), ("B", {})] * 4), [])

    def test_M5_some_searches_failing_do_not_change_the_cadence(self):
        self.make()
        for _ in range(20):
            self.step(fail="A")
        b_times = [t for t, pc, p in self.server.apple_requests if (pc, p) == (B_PC, D)]
        self.assertEqual(len(b_times), 20)
        self.assertEqual({round(b - a, 3) for a, b in zip(b_times, b_times[1:])}, {30 * 1.075})

    def test_M5_every_search_failing_backs_off_globally_and_resets(self):
        self.make()
        self.server.apple = lambda pc, p: (503, [], b"")
        self.run_until(3000)
        self.server.apple = lambda pc, p: apple_answer({}, p, pc)
        self.run_until(3600)
        starts = [t for t, pc, p in self.server.apple_requests if (pc, p) == (A_PC, D)]
        waits = [round(b - a - 3 * w.REQUEST_GAP_SECONDS, 3) for a, b in zip(starts, starts[1:])]
        self.assertEqual(waits[:6], [52.5, 105, 210, 420, 525, 525])   # interval x 2^n x 0.875, capped at 600 x 0.875
        self.assertEqual(waits[-1], round(30 * 1.075 - 3 * w.REQUEST_GAP_SECONDS, 3))  # reset by the success

    def test_M5_throttle_ends_the_check_and_honours_retry_after(self):
        date = email.utils.formatdate(time.time() + 600, usegmt=True)
        for status, header, at_least, at_most in ((429, "3600", 3600, 3960), (429, date, 597, 663),
                                                  (503, "120", 120, 132), (429, "garbage", 52.5, 52.5),
                                                  (403, None, 52.5, 52.5)):
            with self.subTest(status=status, header=header):
                self.server.events.clear()
                self.make()
                self.server.apple = lambda pc, p: (status, [("Retry-After", header)] if header else [], b"")
                self.clock.pause(self.watcher.cycle())
                self.assertEqual(len(self.server.apple_requests), 1)     # the other three were not sent
                self.server.apple = lambda pc, p: apple_answer({}, p, pc)
                out = self.run_until(at_most + 600)
                first, second = self.server.apple_requests[0][0], self.server.apple_requests[1][0]
                self.assertTrue(at_least <= second - first <= at_most + 1, second - first)
                expected = 2 if at_least > 900 else 0     # an hour unread: told once, then once when back
                self.assertEqual(len(out), expected, out)

    def test_M5_a_day_long_retry_after_is_honoured_as_an_hour(self):
        self.assertEqual(w.retry_after_seconds("86400"), 3600)
        self.server.apple = lambda pc, p: (429, [("Retry-After", "86400")], b"")
        with self.assertRaises(w.Throttled) as caught:
            w.fetch(w.endpoint(), D, A_PC)
        self.assertEqual(caught.exception.retry_after, 3600)

    def test_M5_a_long_cooldown_still_warns_and_then_resumes(self):
        self.make()
        self.server.apple = lambda pc, p: (429, [("Retry-After", "86400")], b"")
        self.clock.pause(self.watcher.cycle())
        self.server.apple = lambda pc, p: apple_answer({}, p, pc)
        out = self.run_until(5000)
        first, second = self.server.apple_requests[0][0], self.server.apple_requests[1][0]
        self.assertTrue(3600 <= second - first <= 3960 + 1, second - first)     # resumed after the capped cooldown
        self.assertEqual(len(out), 2, out)
        self.assertTrue(out[0][1].startswith("Stock watcher can't read stock"), out)
        self.assertLessEqual(out[0][0], 900 + 1.15 * self.watcher.interval)     # told while still paused
        self.assertEqual(out[1][1], RECOVERED)

    def test_M5_a_throttled_search_cannot_starve_the_others(self):
        self.make()
        self.server.apple = lambda pc, p: (403, [], b"") if (pc, p) == (A_PC, D) else apple_answer({B: [E]}, p, pc)
        out = self.run_until(1200)
        self.assertTrue(any(text.startswith(E_AT_B) for _, text in out), out)

    def test_M6_unknown_status_is_a_failure_never_no_stock(self):
        self.make({"stores": [A, B], "models": [E, G], "warn_after_seconds": 120, "remind_seconds": 600})
        self.step(shelves={A: [E]})
        odd = apple_answer({A: [E]}, D, A_PC)
        odd["body"]["PickupMessage"]["stores"][0]["partsAvailability"][E]["pickupDisplay"] = "AVAILABLE"
        self.server.apple = lambda pc, p: odd if (pc, p) == (A_PC, D) else apple_answer({}, p, pc)
        out = self.run_until(200)
        self.assertIn(("R341", E), self.watcher.alerted)                 # the stamp survives
        self.assertTrue(any("unknown pickup status" in line for line in self.logged))
        self.assertEqual(len(out), 1)
        self.assertIn("Apple Eldon Square: iPhone Duo 256GB Night Sky", out[0][1])
        self.server.apple = lambda pc, p: apple_answer({A: [E]}, p, pc)
        self.assertEqual(self.step(shelves={A: [E]}), [RECOVERED])       # within remind_seconds: no new alert

    def test_M6_poisoned_remote_text_cannot_block_a_later_alert(self):
        self.make({"stores": [A, B], "models": [E]})
        bad = "\ud800⁠­؜‮\x1b[2J\nToday\r"

        def apple(pc, p):
            return apple_answer({A: [E]}, p, pc, quote=bad) if pc == A_PC else apple_answer({B: [E]}, p, pc)
        self.server.apple = apple
        self.clock.pause(self.watcher.cycle())
        texts = self.server.texts()
        self.assertEqual(len(texts), 2, texts)
        self.assertTrue(texts[0].startswith(E_AT_A + " - [2J Today\n"), texts[0])   # the escape itself is gone
        self.assertTrue(texts[1].startswith(E_AT_B))
        for text in texts:
            self.assertFalse([c for c in text if c != "\n" and unicodedata.category(c) in ("Cc", "Cf", "Cs")])

    def test_M6_a_broken_search_is_only_a_failed_search(self):
        def broken():
            raise ZeroDivisionError("bug")
        good = Toy("good", self.clock, lambda: [("toy-shop", "good")])
        watcher = w.Watcher([Toy("bad", self.clock, broken), good], 30, 180, 120)
        for _ in range(8):
            self.clock.pause(watcher.cycle())
        self.assertEqual(len(good.calls), 8)
        self.assertIn("unexpected ZeroDivisionError: bug", watcher.last_error)
        texts = self.server.texts()
        self.assertEqual(texts[0], "IN STOCK: good")
        self.assertIn("- Toy Shop: item bad", texts[1])


# ---- watch(), commands and main() ------------------------------------------

class LoopAndCommandTest(ServerTest):
    def toy_watch(self, behaviour, cycles, note=None):
        pauses, calls = [], []

        def pause(seconds):
            pauses.append(seconds)
            self.clock.t += seconds
            if len(pauses) > cycles:
                raise KeyboardInterrupt
        w.pause = pause
        with self.assertRaises(KeyboardInterrupt):
            w.watch([Toy("console", self.clock, behaviour, calls)], 30, 3600, 900, note=note)
        return pauses, calls

    def test_the_loop_is_not_tied_to_apple(self):
        state = {"n": 0}

        def toy():
            state["n"] += 1
            if state["n"] == 1:
                raise w.CheckFailed("shop did not answer")
            return [] if state["n"] == 2 else [("toy-shop", "console")]
        self.toy_watch(toy, 8)
        self.assertEqual(self.server.texts(), ["IN STOCK: console"])

    def test_M1_start_delay_and_M4_startup_note_sent_once_best_effort(self):
        self.server.telegram = "down"
        pauses, calls = self.toy_watch(lambda: [], 4, note="Stock watcher started.")
        self.assertEqual(pauses[0], 15)                       # rng.uniform(0, interval), the midpoint here
        self.assertGreater(calls[0], 1_000_000 + 14)          # nothing was requested before the start delay
        self.assertEqual(self.server.attempts, ["Stock watcher started."])   # tried once, never again
        self.server.reset()
        self.server.clock = self.clock
        self.toy_watch(lambda: [], 4, note="Stock watcher started.")
        self.assertEqual(self.server.texts(), ["Stock watcher started."])

    def test_M6_nothing_unexpected_ends_the_loop(self):
        original = w.Watcher.cycle
        state = {"n": 0}

        def flaky(watcher):
            state["n"] += 1
            if state["n"] in (2, 3):
                raise ValueError("bug in the loop")
            return original(watcher)
        w.Watcher.cycle = flaky
        self.addCleanup(setattr, w.Watcher, "cycle", original)
        pauses, _ = self.toy_watch(lambda: [], 6)
        self.assertEqual(state["n"], 6)
        self.assertEqual(sum(1 for line in self.logged if "check failed unexpectedly" in line), 2)
        self.assertTrue(all(math.isfinite(p) and p > 0 for p in pauses[1:]))

    def test_M6_keyboard_interrupt_and_system_exit_still_stop(self):
        for stop in (KeyboardInterrupt, SystemExit):
            def stopping():
                raise stop
            with self.subTest(stop=stop.__name__), self.assertRaises(stop):
                w.watch([Toy("x", self.clock, stopping)], 30, 180, 900)

    def test_run_forever_logs_the_interval_and_sends_the_note(self):
        captured = {}
        self.addCleanup(setattr, w, "watch", w.watch)
        self.addCleanup(__import__("signal").signal, 15, __import__("signal").getsignal(15))
        w.watch = lambda searches, poll, *args, **kw: captured.update(n=len(searches), poll=poll, **kw)
        w.run_forever(w.Config({"stores": list(STORES)[:4], "models": [E, G]}))
        self.assertEqual((captured["n"], captured["poll"]), (8, 30))
        self.assertIn("every ~30s", captured["note"])
        self.assertIn("8 requests every ~30s", self.logged[-1])

    def write_config(self, raw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        return path

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                w.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_M8_once_is_strict(self):
        path = self.write_config(EXAMPLE)
        self.server.apple = lambda pc, p: apple_answer({A: [E, F]}, p, pc)
        code, out, _ = self.main("--once", "--config", path)
        self.assertIn(code, (0, None))
        self.assertIn("IN STOCK: iPhone Duo 256GB Night Sky at Apple Eldon Square (Available Today)", out)
        self.assertIn("Apple Eldon Square: MK2F4QN/A  title of MK2F4QN/A", out)    # listed, not watched
        for answer in (lambda pc, p: (503, [], b"") if (pc, p) == (B_PC, F) else apple_answer({}, p, pc),
                       lambda pc, p: [], lambda pc, p: (429, [], b"")):
            self.server.apple = answer
            code, _, _ = self.main("--once", "--config", path)
            self.assertTrue(isinstance(code, str) and code.startswith("Check failed"), code)

    def test_M8_bad_config_exits_in_one_line_before_any_request(self):
        path = self.write_config({"stores": [A], "models": [E], "poll_seconds": 1e309})
        for config in (path, os.path.dirname(path)):
            code, _, err = self.main("--config", config)
            self.assertTrue(isinstance(code, str) and code.startswith("Config problem"), code)
            self.assertNotIn("\n", code)
            self.assertNotIn("Traceback", err)
        self.assertEqual((self.server.events, self.server.attempts), ([], []))

    def test_M6_error_text_is_sanitised(self):
        def bad_status(handler):
            handler.wfile.write(b"HTTP/1.1 2000 \x1b[2Jfake\r\x9b\r\n\r\n")
            handler.close_connection = True
        self.server.apple = lambda pc, p: bad_status
        code, _, _ = self.main("--once", "--config", self.write_config(EXAMPLE))
        self.assertTrue(code.startswith("Check failed: no answer from Apple"), code)
        self.assertFalse([c for c in code if unicodedata.category(c) in ("Cc", "Cf", "Cs")], repr(code))

    def test_keyboard_interrupt_exits_cleanly(self):
        self.addCleanup(setattr, w, "cmd_once", w.cmd_once)

        def interrupted(cfg):
            raise KeyboardInterrupt
        w.cmd_once = interrupted
        code, _, err = self.main("--once", "--config", self.write_config(EXAMPLE))
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", err)

    def test_verify_stores_keeps_going_after_a_failure(self):
        self.server.apple = lambda pc, p: (503, [], b"") if pc == "AB11 5PN" else apple_answer({}, p, pc)
        code, out, _ = self.main("--verify-stores")
        self.assertIn(code, (0, None))
        self.assertIn("FAILED    Apple Union Square (AB11 5PN): HTTP 503 from Apple", out)
        self.assertIn("OK        R762 Apple Battersea", out)
        self.assertTrue(out.endswith("\n38 confirmed, 1 to look at.\n"), out[-80:])
        self.assertEqual(self.clock.pauses, [w.REQUEST_GAP_SECONDS] * 38)

    def test_test_alert_reports_failure(self):
        path = self.write_config(EXAMPLE)
        code, out, _ = self.main("--test", "--config", path)
        self.assertIn(code, (0, None))
        self.assertTrue(self.server.texts()[0].startswith("TEST ALERT (not real stock)\nIN STOCK:"))
        self.server.telegram = "down"
        code, _, _ = self.main("--test", "--config", path)
        self.assertTrue(isinstance(code, str) and "not delivered" in code, code)

    def update(self, uid, chat):
        return {"update_id": uid, "message": {"chat": {"id": chat, "type": "private"}, "text": "hi"}}

    def test_chat_id_uses_a_message_sent_before_the_step(self):
        backlog = [self.update(17, 111), self.update(18, 222)]
        self.server.updates = lambda params: backlog if "offset" not in params else []
        code, out, err = self.main("--chat-id")
        self.assertIn(code, (0, None))
        self.assertEqual(out.strip(), "222")                  # the most recent
        self.assertIn("More than one chat", err)
        self.assertNotIn("Waiting", err)

    def test_chat_id_waits_for_a_message_sent_during_the_step(self):
        polls = {"n": 0}

        def updates(params):
            if "timeout" not in params:
                return [{"update_id": 5, "my_chat_member": {}}]   # not a message: acknowledged, not used
            polls["n"] += 1
            return [self.update(6, 333)] if polls["n"] == 2 else []
        self.server.updates = updates
        code, out, err = self.main("--chat-id")
        self.assertIn(code, (0, None))
        self.assertEqual(out.strip(), "333")
        self.assertIn("send it any message", err)
        self.assertEqual(self.server.update_calls[1].get("offset"), "6")

    def test_chat_id_with_no_message_says_what_to_do(self):
        code, out, _ = self.main("--chat-id")
        self.assertTrue(isinstance(code, str) and code.startswith("No message arrived. Open your bot"), code)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
