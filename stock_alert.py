#!/usr/bin/env python3
"""
Night Shift: an always-on stock watcher that messages you on Telegram.

It checks a shop's stock on a timer and messages you when the thing you want
appears, then again every remind_seconds while it stays. The watch loop
(watch) knows nothing about any shop: a shop is a list of "searches", objects
that each make one request and say what is in stock right now. One shop is
included, Apple Store pickup in the UK (apple_searches).

Standard library only. Not affiliated with Apple.

What to watch lives in config.json: a list of store numbers and a list of part
numbers. Your Telegram details come from two environment variables:

  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     your chat id (run with --chat-id to find it)

Usage:
  python3 stock_alert.py --stores POSTCODE   list the stores nearest a postcode
  python3 stock_alert.py --models            list the models this tool knows
  python3 stock_alert.py --once              check now, print what Apple lists, exit
  python3 stock_alert.py --chat-id           print the chat id of whoever messaged your bot
  python3 stock_alert.py --test              send a test alert to your phone
  python3 stock_alert.py                     run forever
  python3 stock_alert.py --verify-stores     (maintainers) re-check the bundled store list
  (add --config PATH to use a config file other than ./config.json)
"""

import sys

MIN_PYTHON = (3, 9)  # the one place the minimum supported Python version is declared
if sys.version_info < MIN_PYTHON:
    sys.exit("Night Shift needs Python %d.%d or newer." % MIN_PYTHON)

import argparse
import http.client
import json
import math
import os
import random
import re
import signal
import socket
import ssl
import threading
import time
import unicodedata
import urllib.parse
from email.utils import parsedate_to_datetime
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config.json")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# Base URLs. Overridable so the tests can point them at a local fake server.
APPLE_BASE = os.environ.get("NIGHT_SHIFT_APPLE_URL", "https://www.apple.com").rstrip("/")
TELEGRAM_BASE = os.environ.get("NIGHT_SHIFT_TELEGRAM_URL", "https://api.telegram.org").rstrip("/")

COUNTRY_PATH = "/uk"
SECONDS = {  # setting: (default, floor, ceiling). Below the floor is raised to it; above the ceiling is an error.
    "poll_seconds": (30, 5, 300),           # 30 is the recommended default, 5 the floor; 300 keeps 900 valid below
    "remind_seconds": (180, 60, 86400),
    "warn_after_seconds": (900, 120, 86400),  # below min_warn_after() is an error, not raised (see there)
}
MAX_MESSAGES_PER_ALERT = 5
MAX_SEARCHES = 12             # distinct requests per check
MAX_REQUESTS_PER_MINUTE = 48  # the rate the author ran for 12 hours (4 searches every 5 s) without being blocked
REQUEST_GAP_SECONDS = 0.25    # pause between two requests inside a check
MAX_BACKOFF_SECONDS = 600
HTTP_TIMEOUT = 15            # total seconds for one request, first byte to last
MAX_RESPONSE_BYTES = 2_000_000
MAX_RETRY_AFTER_SECONDS = 3600   # a longer Retry-After is honoured as this
TELEGRAM_TEXT_LIMIT = 4000   # Telegram's own limit is 4096 characters
PICKUP_STATUSES = ("available", "unavailable", "ineligible")  # anything else fails the search
USER_AGENT = "Mozilla/5.0 (compatible; night-shift; personal pickup checker)"


class CheckFailed(Exception):
    """A search could not get a usable answer. Never treated as "no stock"."""


class Throttled(CheckFailed):
    """The shop said slow down (HTTP 429, 403, or 503 with Retry-After). Ends the check."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class RequestFailed(Exception):
    """No complete HTTP answer. sent is True when the request itself went out."""

    def __init__(self, message, sent=False):
        super().__init__(message)
        self.sent = sent


class TelegramError(RuntimeError):
    """Telegram did not accept a request. maybe_delivered: it went out, the answer did not come back."""

    def __init__(self, message, maybe_delivered=False):
        super().__init__(message)
        self.maybe_delivered = maybe_delivered


class ConfigError(Exception):
    """config.json is missing something or is inconsistent."""


class Hit(NamedTuple):
    """One thing in stock at one place, as reported by a search."""
    key: tuple     # identifies the item and place
    summary: str   # one line for the log
    message: str   # the text sent to Telegram


def clean(value, limit=100):
    """Make outside text safe for one log line or message: control characters
    and line breaks become spaces, invisible format characters and lone
    surrogates are dropped, whitespace is collapsed, at most `limit` characters."""
    out = []
    for ch in str(value)[:limit * 4]:
        kind = unicodedata.category(ch)
        out.append(" " if kind in ("Cc", "Zl", "Zp") else "" if kind in ("Cf", "Cs") else ch)
    text = " ".join("".join(out).split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), str(msg).encode("utf-8", "replace").decode("utf-8"), flush=True)


# The watcher's clock and randomness. Tests replace these three.
def now():
    return time.monotonic()


def pause(seconds):
    time.sleep(seconds)


rng = random.Random()


# ---- Bundled data ----------------------------------------------------------

def load_data(filename):
    with open(os.path.join(HERE, filename), encoding="utf-8") as f:
        return json.load(f)


def model_index(models_db):
    """Flatten the model catalogue: part -> {name, buy_url, twin}."""
    index = {}
    for product in models_db["products"].values():
        variants = product["variants"]
        for v in variants:
            twins = [o["part"] for o in variants if o["capacity"] == v["capacity"] and o["colour"] != v["colour"]]
            index[v["part"]] = {
                "name": f'{product["name"]} {v["capacity"]} {v["colour"]}',
                "buy_url": product["buy_url"].format(
                    capacity=v["capacity"].lower(), colour=v["colour"].lower().replace(" ", "-")),
                "twin": twins[0] if twins else None,
            }
    return index


# ---- Config ----------------------------------------------------------------

CONFIG_KEYS = ("country_path", "poll_seconds", "remind_seconds", "warn_after_seconds",
               "messages_per_alert", "stores", "models")


def effective_interval(poll_seconds, searches):
    """Seconds from the start of one check to the next. The only request-rate rule: at most
    MAX_SEARCHES requests per check, and checks far enough apart that the average stays at or
    under MAX_REQUESTS_PER_MINUTE (60 / 48 = 1.25 s per search)."""
    if searches > MAX_SEARCHES:
        raise ConfigError(f"this watch list needs {searches} searches per check (stores x distinct twins) and the "
                          f"limit is {MAX_SEARCHES}. Each search is one request to Apple; the limit keeps checks "
                          f"frequent while the watcher stays at {MAX_REQUESTS_PER_MINUTE} requests a minute or fewer. "
                          f"Watch fewer stores or models.")
    return max(poll_seconds, SECONDS["poll_seconds"][1], 60 / MAX_REQUESTS_PER_MINUTE * searches)


def min_warn_after(interval, searches):
    """The lowest warn_after_seconds that a healthy watcher never trips: two intervals plus one
    check in which every search takes its whole deadline. 12 searches at the 300 s poll ceiling
    need 783 s, so the default 900 always starts."""
    return max(SECONDS["warn_after_seconds"][1], 2 * interval + searches * (HTTP_TIMEOUT + REQUEST_GAP_SECONDS))


def endpoint(country_path=COUNTRY_PATH):
    return f"{APPLE_BASE}{country_path}/shop/pickup-message-recommendations"


def _seconds(raw, key, floor=None, refuse_below=""):
    default, minimum, ceiling = SECONDS[key]
    floor = minimum if floor is None else floor
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f'"{key}" must be a number of seconds above 0, not {clean(json.dumps(value), 40)}')
    if value > ceiling:
        raise ConfigError(f'"{key}" must be at most {ceiling} seconds, not {value:g}')
    if value < floor and refuse_below:
        raise ConfigError(f'"{key}" must be at least {floor:g} seconds {refuse_below}, not {value:g}')
    if value < floor:
        log(f"{key} {value:g} is below the minimum; using {floor:g}s")
        return float(floor)
    return float(value)


def _text(entry, field, what):
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{what} needs a non-empty "{field}"')
    return value.strip()


class Config:
    """Settings for one watcher.

    "stores" entries are store numbers from the bundled UK list ("R341"), or
    objects {"number", "name", "postcode"} for anything not in it.
    "models" entries are part numbers from the bundled catalogue ("MK2E4QN/A"),
    or objects {"part", "name", "ask_about", "buy_url"} for anything not in it.
    """

    def __init__(self, raw, stores_db=None, models_db=None):
        if not isinstance(raw, dict):
            raise ConfigError("config must be a JSON object")
        unknown = [k for k in raw if k not in CONFIG_KEYS and not str(k).startswith("_")]
        if unknown:
            raise ConfigError(f'unknown setting {", ".join(clean(json.dumps(k), 40) for k in unknown)}. '
                              f'Known settings: {", ".join(CONFIG_KEYS)}')
        known_stores = (stores_db or load_data("stores_uk.json"))["stores"]
        known_models = model_index(models_db or load_data("models_uk.json"))

        country_path = raw.get("country_path", COUNTRY_PATH)
        if not isinstance(country_path, str) or not re.fullmatch(r"/[a-z]{2}(-[a-z]{2,4})?/?", country_path):
            raise ConfigError(f'"country_path" must look like "/uk", not {clean(json.dumps(country_path), 40)}')
        self.country_path = country_path.rstrip("/")
        self.endpoint = endpoint(self.country_path)

        self.poll_seconds = _seconds(raw, "poll_seconds")
        self.remind_seconds = _seconds(raw, "remind_seconds")
        mpa = raw.get("messages_per_alert", 1)
        if isinstance(mpa, bool) or not isinstance(mpa, int) or not 1 <= mpa <= MAX_MESSAGES_PER_ALERT:
            raise ConfigError(f'"messages_per_alert" must be a whole number from 1 to {MAX_MESSAGES_PER_ALERT}')
        self.messages_per_alert = mpa

        stores_raw, models_raw = raw.get("stores") or [], raw.get("models") or []
        if not isinstance(stores_raw, list) or not isinstance(models_raw, list):
            raise ConfigError('"stores" and "models" must be lists')

        self.stores = {}     # number -> name
        self.postcodes = {}  # number -> postcode to search from
        bad_stores = []
        for s in stores_raw:
            if isinstance(s, str):
                if s not in known_stores:
                    bad_stores.append(s)
                    continue
                number, name, postcode = s, known_stores[s]["name"], known_stores[s]["postcode"]
            elif isinstance(s, dict):
                number, name, postcode = (_text(s, f, "a custom store") for f in ("number", "name", "postcode"))
            else:
                raise ConfigError('a store is a store number like "R341", or an object with "number", '
                                  '"name" and "postcode"')
            if number in self.stores:
                raise ConfigError(f"store {clean(number, 30)} is listed twice")
            self.stores[number] = clean(name, 60)
            self.postcodes[number] = postcode
        if bad_stores:
            raise ConfigError(f'unknown store {", ".join(clean(json.dumps(s), 30) for s in bad_stores)}. '
                              f'Use a number from the README table, or find it with:  '
                              f'python3 stock_alert.py --stores "YOUR POSTCODE"')
        if not self.stores:
            raise ConfigError('add at least one store number under "stores"')

        self.models = {}    # part -> name
        self.buy_urls = {}  # part -> url
        self.ask_for = {}   # part -> the part to ask Apple about (its twin)
        bad_models = []
        for m in models_raw:
            if isinstance(m, str):
                if m not in known_models:
                    bad_models.append(m)
                    continue
                part, info = m, known_models[m]
                name, buy_url, ask = info["name"], info["buy_url"], info["twin"]
                if not ask:
                    raise ConfigError(f"{part} has no twin in the catalogue; add it as a custom model")
            elif isinstance(m, dict):
                part, name, ask = (_text(m, f, "a custom model") for f in ("part", "name", "ask_about"))
                if ask == part:
                    raise ConfigError(f'{clean(part, 30)}: "ask_about" must be a different model '
                                      f'(README, "How it works")')
                buy_url = m.get("buy_url") or f"https://www.apple.com{self.country_path}/shop"
                if not isinstance(buy_url, str) or not buy_url.startswith("https://"):
                    raise ConfigError(f'{clean(part, 30)}: "buy_url" must start with https://')
            else:
                raise ConfigError('a model is a part number like "MK2E4QN/A", or an object with "part", '
                                  '"name" and "ask_about"')
            if part in self.models:
                raise ConfigError(f"model {clean(part, 30)} is listed twice")
            self.models[part] = clean(name, 60)
            self.buy_urls[part] = clean(buy_url, 300)
            self.ask_for[part] = ask
        if bad_models:
            raise ConfigError(f'unknown model {", ".join(clean(json.dumps(m), 30) for m in bad_models)}. '
                              f'Use a part number from the README table (or python3 stock_alert.py --models), '
                              f'or add it as a custom model.')
        if not self.models:
            raise ConfigError('add at least one part number under "models"')

        # One search per distinct request: stores that share a postcode share it.
        grouped = {}
        for store in self.stores:
            for ask in dict.fromkeys(self.ask_for.values()):
                grouped.setdefault((ask, self.postcodes[store]), []).append(store)
        self.searches = [(ask, postcode, stores) for (ask, postcode), stores in grouped.items()]
        self.interval = effective_interval(self.poll_seconds, len(self.searches))
        n = len(self.searches)
        self.warn_after_seconds = _seconds(
            raw, "warn_after_seconds", min_warn_after(self.interval, n),
            f"for {n} searches every {self.interval:g}s (two intervals plus one check of slow good reads, "
            f"{n} x {HTTP_TIMEOUT + REQUEST_GAP_SECONDS:g}s; never below 120)")


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found. Copy config.example.json to config.json and edit it.") from None
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror or e}") from None
    except (ValueError, RecursionError) as e:
        raise ConfigError(f"{path} is not valid JSON: {clean(e)}") from None
    return Config(raw)


# ---- HTTP ------------------------------------------------------------------

def http_request(url, data=None, headers=None, deadline=None):
    """One HTTP request with a hard total deadline. Returns (status, headers, body).

    A timer shuts the socket when the deadline passes, so a server that sends
    one byte at a time cannot hold the request past it. Raises RequestFailed
    for anything short of a complete answer.
    """
    deadline = HTTP_TIMEOUT if deadline is None else deadline
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=deadline,
                                           context=ssl.create_default_context())
    elif parts.scheme == "http":
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=deadline)
    else:
        raise RequestFailed(f"unsupported URL scheme {clean(parts.scheme, 10)!r}")
    expired, socks = threading.Event(), []

    def cut_off():
        expired.set()
        # conn.sock covers the connect and TLS handshake; socks keeps the socket after
        # a Connection: close response takes it over and conn.sock becomes None.
        for sock in [conn.sock] + socks:
            if sock is not None:
                try:  # the plain socket method, so a TLS socket is cut without touching its TLS state
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)
                except OSError:
                    pass

    timer = threading.Timer(deadline, cut_off)
    timer.daemon = True
    timer.start()
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    late, sent = f"no complete answer within {deadline:g}s", False
    try:
        conn.connect()
        socks.append(conn.sock)
        conn.request("GET" if data is None else "POST", target, body=data, headers=headers or {})
        sent = True
        resp = conn.getresponse()
        body = bytearray()
        while not expired.is_set():
            chunk = resp.read1(65536)
            if not chunk:
                break
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise RequestFailed(f"answer larger than {MAX_RESPONSE_BYTES} bytes", sent)
        if expired.is_set():
            raise RequestFailed(late, sent)
        if resp.length:  # Content-Length promised more than arrived
            raise RequestFailed("connection closed before the full answer arrived", sent)
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, bytes(body)
    except RequestFailed:
        raise
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise RequestFailed(late if expired.is_set() else clean(f"{type(e).__name__}: {e}", 160), sent) from None
    finally:
        timer.cancel()
        conn.close()


def retry_after_seconds(value):
    """Seconds from a Retry-After header (whole seconds or an HTTP date), or None."""
    value = (value or "").strip()
    try:
        seconds = float(value) if value.isdigit() else parsedate_to_datetime(value).timestamp() - time.time()
    except Exception:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS) if math.isfinite(seconds) else None


def paced(items):
    """Yield items REQUEST_GAP_SECONDS apart: the one pacing rule for every command."""
    for i, item in enumerate(items):
        if i:
            pause(REQUEST_GAP_SECONDS)
        yield item


# ---- Apple -----------------------------------------------------------------
#
# The lookup is the one Apple's own product pages use for "check availability
# near you". Two things about it shape this code:
#
# 1. You ask about one model and it lists up to three *similar* models that are
#    in stock, closest match first, and never the model you asked about. So to
#    watch a model you ask about its "twin" (same product and size, another colour).
# 2. It lists stock for only one store per search: the nearest one that has
#    any. So each store is searched from its own postcode.

def fetch(url, ask_part, postcode):
    query = urllib.parse.urlencode(
        {"fae": "true", "mts.0": "regular", "location": postcode, "product": ask_part}, safe="/")
    try:
        status, headers, body = http_request(
            f"{url}?{query}", headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    except RequestFailed as e:
        raise CheckFailed(f"no answer from Apple: {e}") from None
    wait = retry_after_seconds(headers.get("retry-after"))
    if status in (403, 429) or (status == 503 and wait is not None):
        raise Throttled(f"HTTP {status} from Apple" + (f", Retry-After {wait:.0f}s" if wait is not None else ""),
                        retry_after=wait)
    if status != 200:
        raise CheckFailed(f"HTTP {status} from Apple")
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        raise CheckFailed("Apple returned a non-JSON page (likely blocked)") from None


def store_list(payload):
    body = payload.get("body") if isinstance(payload, dict) else None
    if isinstance(body, dict):
        content = body.get("content")
        for pickup in (body.get("PickupMessage"), content.get("pickupMessage") if isinstance(content, dict) else None):
            if isinstance(pickup, dict) and isinstance(pickup.get("stores"), list):
                return pickup["stores"]
    raise CheckFailed("response has no store list")


def _title(info, part):
    """Display title for --once. Optional: any shape problem falls back to the part number."""
    types = info.get("messageTypes")
    regular = types.get("regular") if isinstance(types, dict) else None
    title = regular.get("storePickupProductTitle") if isinstance(regular, dict) else None
    return clean(title, 80) if isinstance(title, str) and title.strip() else clean(part, 30)


def parse(payload, stores, parts, titles=None):
    """{(store, part): pickup quote} for `parts` in stock at `stores`, the stores
    this search was centred on. Fails closed: a missing store, a missing field
    or a pickup status this code does not know raises CheckFailed, so a changed
    response is never mistaken for "no stock". Optional fields (the quote, the
    title) never discard a hit. titles, if given, collects every available
    part's title for --once."""
    found, seen = {}, set()
    for store in store_list(payload):
        number = store.get("storeNumber") if isinstance(store, dict) else None
        if not isinstance(number, str) or number not in stores:
            continue
        seen.add(number)
        availability = store.get("partsAvailability")
        if not isinstance(availability, dict):
            raise CheckFailed(f"store {number} has no availability list")
        for part, info in availability.items():
            status = info.get("pickupDisplay") if isinstance(info, dict) else None
            if not isinstance(status, str) or status not in PICKUP_STATUSES:
                raise CheckFailed(f"store {number}: unknown pickup status "
                                  f"{clean(json.dumps(status), 30)} for {clean(part, 20)}")
            if status != "available":
                continue
            if titles is not None:
                titles[(number, part)] = _title(info, part)
            if part in parts:
                quote = info.get("pickupSearchQuote")
                found[(number, part)] = clean(quote, 60) if isinstance(quote, str) and quote.strip() else "Available"
    missing = sorted(set(stores) - seen)
    if missing:
        raise CheckFailed(f"store {', '.join(missing)} missing from response (check its number and postcode)")
    return found


def alert_text(cfg, store, part, quote):
    return (f"IN STOCK: {cfg.models[part]}\n"
            f"{cfg.stores[store]} - {quote}\n"
            f"Reserve for pickup now: {cfg.buy_urls[part]}")


class AppleSearch:
    """One request to Apple: one twin asked about from one postcode.

    covers maps each (store, part) this search is the authority for to its
    (store name, model name). It reports only those.
    """

    def __init__(self, cfg, ask_part, postcode, stores):
        self.cfg, self.ask_part, self.postcode, self.stores = cfg, ask_part, postcode, list(stores)
        self.key = (ask_part, postcode)
        self.parts = [p for p in cfg.models if cfg.ask_for[p] == ask_part]
        self.covers = {(s, p): (cfg.stores[s], cfg.models[p]) for s in self.stores for p in self.parts}

    def __str__(self):
        return f'{" / ".join(self.cfg.stores[s] for s in self.stores)} (asking about {self.ask_part})'

    def run(self, titles=None):
        cfg = self.cfg
        found = parse(fetch(cfg.endpoint, self.ask_part, self.postcode), self.stores, self.parts, titles)
        return [Hit((store, part), f"{cfg.models[part]} at {cfg.stores[store]} ({quote})",
                    alert_text(cfg, store, part, quote))
                for (store, part), quote in found.items()]


def apple_searches(cfg):
    """The Apple Store pickup source: one AppleSearch per distinct request."""
    return [AppleSearch(cfg, ask, postcode, stores) for ask, postcode, stores in cfg.searches]


# ---- Telegram --------------------------------------------------------------

def telegram(method, **params):
    if not BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    try:
        status, _, body = http_request(
            f"{TELEGRAM_BASE}/bot{BOT_TOKEN}/{method}",
            data=urllib.parse.urlencode(params, errors="replace").encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
            deadline=float(params.get("timeout", 0)) + HTTP_TIMEOUT)
    except RequestFailed as e:
        raise TelegramError(f"could not reach Telegram: {e}", maybe_delivered=e.sent) from None
    try:
        reply = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        reply = None
    if not (200 <= status < 300 and isinstance(reply, dict) and reply.get("ok") is True):
        detail = clean(reply.get("description", ""), 120) if isinstance(reply, dict) else ""
        raise TelegramError(f"Telegram said {status} {detail}".strip())
    return reply


def send(text, count=1):
    """Deliver one message to the owner, `count` times. This is the only place a
    message leaves the watcher: a second channel (email, ntfy, ...) goes here.

    Returns False only when the message definitely did not arrive (Telegram
    refused it or could not be reached). It returns True when Telegram accepted
    it, and also when the request went out but no answer came back: the caller
    then treats it as sent, so a lost answer costs at most one duplicate per
    reminder interval, never one per check.
    """
    if len(text) > TELEGRAM_TEXT_LIMIT:
        text = text[:TELEGRAM_TEXT_LIMIT - 3] + "..."
    for i in range(count):
        if i:
            pause(1.5)
        try:
            telegram("sendMessage", chat_id=CHAT_ID, text=text, disable_web_page_preview="true")
        except Exception as e:  # a failed send is reported to the caller, never raised
            maybe = getattr(e, "maybe_delivered", False)
            log(f"Telegram send failed{' (it may still have arrived)' if maybe else ''}: {clean(e, 200)}")
            return i > 0 or maybe
    return True


def wait_for_chat_id(max_wait=90):
    """The chat that most recently messaged the bot, before or during the wait.

    Returns (chat id or None, how many other chats have messaged it). Telegram
    keeps unread messages for about a day, so a message sent before this ran
    counts; otherwise it waits up to max_wait seconds for one.
    """
    deadline, offset, latest, asked = now() + max_wait, None, {}, False
    while True:
        params = {} if offset is None else {"offset": offset}
        if asked:
            params["timeout"] = int(max(1, min(20, deadline - now())))
        result = telegram("getUpdates", **params).get("result")
        for update in result if isinstance(result, list) else []:
            uid = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(uid, int):
                continue
            offset = max(offset or 0, uid + 1)
            message = update.get("message")
            chat = message.get("chat") if isinstance(message, dict) else None
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                latest[chat["id"]] = uid
        if latest:
            return max(latest, key=latest.get), len(latest) - 1
        if asked and now() >= deadline:
            return None, 0
        if not asked:
            print("No message to the bot yet. Open your bot in Telegram and send it any message "
                  f"(for example: hi). Waiting up to {max_wait} seconds...", file=sys.stderr, flush=True)
            asked = True


# ---- The watch loop --------------------------------------------------------

class Watcher:
    """The state of the watch loop, advanced one check at a time by cycle().

    A check runs the searches in order and acts on each answer at once.
    - Alerts: an item in stock with no stamp, or whose reminder is due, is sent
      right then. The stamp is set when send() reports it sent (or maybe sent),
      and cleared only by a good read of the same search showing it gone.
    - Health: a search with no good read for warn_after_seconds is stale. One
      message when the stale set goes from empty to non-empty, one when it is
      empty again, each retried until sent.
    - Pacing: every search failing backs off the whole watcher; a throttle ends
      the check and pauses it. Some searches failing changes nothing.
    """

    def __init__(self, searches, poll_seconds, remind_seconds, warn_after_seconds, messages_per_alert=1):
        keys = [s.key for s in searches]
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("searches must be a non-empty list with distinct keys")
        self.searches = list(searches)
        self.interval = effective_interval(poll_seconds, len(keys))
        self.remind_seconds, self.warn_after_seconds = remind_seconds, warn_after_seconds
        self.messages_per_alert = messages_per_alert
        self.last_ok = {}                        # search key -> time of its last good read, set by the first cycle
        self.failing = set()                     # search keys whose latest attempt failed
        self.alerted = {}                        # hit key -> time its last alert was sent
        self.warned = False                      # the owner was told some searches are stale
        self.failed_checks = 0                   # checks in a row in which every search failed
        self.resume_at = -math.inf               # no requests before this (backoff or throttle)
        self.last_error = ""

    def cycle(self):
        """Run one check if one is due; return seconds until the next call."""
        start = now()
        if not self.last_ok:  # health counts from the first check, not from creation: start delay and note excluded
            self.last_ok = {s.key: start for s in self.searches}
        due = self.resume_at  # while paused: wake for health only, and exactly when the pause ends
        if start >= self.resume_at:
            self._check()
            due = max(start + self.interval * rng.uniform(1.0, 1.15), self.resume_at)
        self._health()
        wait = due - now()
        if wait > self.interval * 1.15:  # a long pause: wake every interval so health stays current
            wait = self.interval
        return max(wait, REQUEST_GAP_SECONDS)

    def _check(self):
        ok, throttle, stock = False, None, False
        for search in paced(list(self.searches)):
            try:
                hits = search.run()
            except Exception as e:  # CheckFailed, Throttled or a bug: a failed search, nothing more
                self.last_error = clean(e if isinstance(e, CheckFailed) else f"unexpected {type(e).__name__}: {e}", 160)
                log(f"search {search} failed: {self.last_error}")
                self.failing.add(search.key)
                if isinstance(e, Throttled):
                    throttle = e  # the next check starts with the searches this one did not reach
                    self.searches.append(self.searches.pop(self.searches.index(search)))
            else:
                ok, stock = True, stock or bool(hits)
                self.last_ok[search.key] = now()
                self.failing.discard(search.key)
                self._seen(search, hits)
            self._health()
            if throttle is not None:
                break
        if ok and throttle is None:
            self.failed_checks = 0
            if not stock:
                log("no stock")
            return
        self.failed_checks = 0 if ok else self.failed_checks + 1
        if throttle is not None and throttle.retry_after is not None:
            wait = throttle.retry_after * rng.uniform(1.0, 1.1)
        else:
            wait = min(MAX_BACKOFF_SECONDS, self.interval * 2 ** min(max(self.failed_checks, 1), 10))
            wait *= rng.uniform(0.75, 1.0)
        self.resume_at = now() + wait
        log(f"{'throttled' if throttle is not None else 'every search failed'}: pausing all searches for {wait:.0f}s")

    def _seen(self, search, hits):
        """A good read: forget what it shows is gone, alert what is in stock."""
        current = {hit.key for hit in hits}
        for key in search.covers:
            if key not in current:
                self.alerted.pop(key, None)
        for hit in hits:
            if now() - self.alerted.get(hit.key, -math.inf) >= self.remind_seconds:
                log(f"IN STOCK: {hit.summary}")
                if send(hit.message, self.messages_per_alert):
                    self.alerted[hit.key] = now()

    def _health(self):
        t = now()
        stale = [s for s in self.searches if t - self.last_ok[s.key] >= self.warn_after_seconds]
        if bool(stale) == self.warned:
            return
        if stale:  # one message: every search stale or failing now, not only the first to cross the line
            places = {}
            for search in [s for s in self.searches if s in stale or s.key in self.failing]:
                for place, item in search.covers.values():
                    items = places.setdefault(place, [])
                    if item not in items:
                        items.append(item)
            text = (f"Stock watcher can't read stock (no good answer for {self.warn_after_seconds / 60:.0f} min; "
                    f"last error: {self.last_error or 'none'}). NOT watching these until this clears:\n"
                    + "".join(f"- {place}: {', '.join(items)}\n" for place, items in places.items())
                    + "It keeps trying and will tell you when it reads everything again.")
        else:
            text = "Stock watcher is reading everything again."
        if send(text):
            self.warned = bool(stale)


def watch(searches, poll_seconds, remind_seconds, warn_after_seconds=SECONDS["warn_after_seconds"][0],
          messages_per_alert=1, note=None):
    """Run searches forever and message the owner about what they find.

    searches is a list of at most MAX_SEARCHES objects, each with .key
    (hashable, distinct), .covers ({hit key: (place name, item name)} for the
    items this search is the authority on) and .run() (returns a list of Hits
    for covered items in stock now, empty when none). run() must raise
    CheckFailed when it cannot tell, so a broken check is never mistaken for an
    empty shelf, and Throttled when the shop says slow down.
    """
    watcher = Watcher(searches, poll_seconds, remind_seconds, warn_after_seconds, messages_per_alert)
    if note:
        send(note)  # best effort, sent once
    delay = rng.uniform(0, watcher.interval)
    while True:
        pause(delay)
        try:
            delay = watcher.cycle()
        except Exception as e:  # never let anything end the loop; KeyboardInterrupt and SystemExit still do
            log(f"check failed unexpectedly: {clean(repr(e), 200)}")
            delay = watcher.interval


def run_forever(cfg):
    if not (BOT_TOKEN and CHAT_ID):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first (see README).")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemctl stop: exit cleanly
    watching = f"{', '.join(cfg.models.values())} at {', '.join(cfg.stores.values())}"
    log(f"Watching {watching}: {len(cfg.searches)} requests every ~{cfg.interval:g}s; "
        f"warning after {cfg.warn_after_seconds:g}s without a good answer")
    watch(apple_searches(cfg), cfg.poll_seconds, cfg.remind_seconds, cfg.warn_after_seconds, cfg.messages_per_alert,
          note=f"Stock watcher started. Checking for {watching} every ~{cfg.interval:g}s.")


# ---- Commands --------------------------------------------------------------

def any_known_part():
    return next(iter(model_index(load_data("models_uk.json"))))


def store_row(s):
    address = s.get("address") if isinstance(s.get("address"), dict) else {}
    return (f'{clean(s.get("storeNumber", "?"), 6):6} {clean(s.get("storeName", "?"), 24):24} '
            f'{clean(s.get("city", ""), 22):22} {clean(address.get("postalCode", ""), 9):9} '
            f'{clean(s.get("storeDistanceWithUnit", ""), 12)}')


def cmd_stores(postcode):
    stores = [s for s in store_list(fetch(endpoint(), any_known_part(), postcode)) if isinstance(s, dict)]
    if not stores:
        print("Apple returned no stores near that postcode.")
    for s in stores:
        print(store_row(s))


def cmd_models():
    for part, info in model_index(load_data("models_uk.json")).items():
        print(f'{part:11} {info["name"]}')


def cmd_verify_stores():
    """Maintainers: check every bundled store number against the live lookup."""
    db = load_data("stores_uk.json")
    entries = [(num, s["name"], s["postcode"]) for num, s in db["stores"].items()]
    problems = 0
    for number, name, postcode in paced(entries):
        try:
            stores = [s for s in store_list(fetch(endpoint(), any_known_part(), postcode)) if isinstance(s, dict)]
        except CheckFailed as e:
            problems += 1
            print(f"FAILED    {name} ({postcode}): {clean(e)}")
            continue
        dist = lambda s: s["storedistance"] if isinstance(s.get("storedistance"), (int, float)) else 1e9
        got = (min(stores, key=dist) if stores else {}).get("storeNumber")
        if got == number:
            print(f"OK        {number} {name}")
        else:
            problems += 1
            print(f"CHECK     {name} ({postcode}): list says {number}, nearest store Apple returned is "
                  f"{clean(got or 'none', 10)}")
    print(f"{len(entries) - problems} confirmed, {problems} to look at.")


def cmd_once(cfg):
    """One strict pass over every search, paced like the watcher. Any failure raises."""
    found, titles = [], {}
    for search in paced(apple_searches(cfg)):
        found += search.run(titles)
    print(f"Check worked ({len(cfg.searches)} requests).")
    for hit in sorted(found):
        print(f"  IN STOCK: {hit.summary}")
    if not found:
        print("  None of your models is in stock at your stores right now.")
    others = sorted(k for k in titles if k not in {hit.key for hit in found})
    if others:
        print("Other models Apple listed at your stores (not watched, shown so you can check your config):")
        for store, part in others:
            print(f"  {cfg.stores[store]}: {clean(part, 30)}  {titles[(store, part)]}")


def cmd_chat_id():
    chat, others = wait_for_chat_id()
    if chat is None:
        sys.exit("No message arrived. Open your bot in Telegram, send it any message (for example: hi), "
                 "then run this again.")
    if others:
        print("More than one chat has messaged this bot; using the most recent. If the test alert does not "
              "reach you, send your bot a new message and run this again.", file=sys.stderr)
    print(f"Found your chat: {chat}", file=sys.stderr)
    print(chat)


def cmd_test(cfg):
    if not (BOT_TOKEN and CHAT_ID):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
    store, part = next(iter(cfg.stores)), next(iter(cfg.models))
    if not send("TEST ALERT (not real stock)\n" + alert_text(cfg, store, part, "Available Today")):
        sys.exit("The test alert was not delivered (see the error above).")
    print("Sent. Check your phone.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Night Shift: Apple Store pickup stock alerts to Telegram (UK)")
    ap.add_argument("--config", default=os.environ.get("STOCK_ALERT_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--stores", metavar="POSTCODE", help="list the stores nearest a postcode")
    ap.add_argument("--models", action="store_true", help="list the models this tool knows")
    ap.add_argument("--once", action="store_true", help="check now and exit (non-zero if any search fails)")
    ap.add_argument("--chat-id", action="store_true", help="print the chat id of whoever messaged your bot")
    ap.add_argument("--test", action="store_true", help="send a test alert")
    ap.add_argument("--verify-stores", action="store_true", help="re-check the bundled store list against Apple")
    args = ap.parse_args(argv)

    try:
        if args.chat_id:
            cmd_chat_id()
        elif args.stores:
            cmd_stores(args.stores)
        elif args.models:
            cmd_models()
        elif args.verify_stores:
            cmd_verify_stores()
        else:
            cfg = load_config(args.config)
            if args.once:
                cmd_once(cfg)
            elif args.test:
                cmd_test(cfg)
            else:
                run_forever(cfg)
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        sys.exit(130)
    except ConfigError as e:
        sys.exit(f"Config problem: {e}")
    except CheckFailed as e:
        sys.exit(f"Check failed: {e}")
    except RuntimeError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
