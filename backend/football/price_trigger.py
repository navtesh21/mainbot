"""PriceTrigger — real-time Polymarket CLOB WebSocket price spike detector.

Ported verbatim from fball_bot/ws.py. WebSocket-first with REST-polling
fallback if the `websockets` package or connection is unavailable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from typing import Any, Callable, Optional

from backend.football.classifier import SignalClassifier, ParityInfo

logger = logging.getLogger("trading_bot")


def _parse_level(raw, idx: int = 0):
    try:
        if isinstance(raw, dict):
            return float(raw.get("price", 0)), float(raw.get("size", 0))
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            return float(raw[0]), float(raw[1])
    except (ValueError, TypeError, IndexError):
        pass
    return 0.0, 0.0


def _parse_book_bid_ask(raw: dict):
    bids_raw = raw.get("bids") or []
    asks_raw = raw.get("asks") or []

    best_bid = 0.0
    best_ask = 0.0
    bid_vol = 0.0
    ask_vol = 0.0

    for i, b in enumerate(bids_raw[:3]):
        p, s = _parse_level(b)
        if p > 0:
            if i == 0:
                best_bid = p
            bid_vol += p * s

    for i, a in enumerate(asks_raw[:3]):
        p, s = _parse_level(a)
        if p > 0:
            if i == 0:
                best_ask = p
            ask_vol += p * s

    return best_bid, best_ask, bid_vol, ask_vol


class PriceSpike:
    """A detected price spike with rich context for the classifier."""

    def __init__(
        self,
        token_id: str,
        fixture_id: int,
        price_before: float,
        price_now: float,
        change_pct: float,
        direction: str,
        timestamp: float,
        imbalance: float = 1.0,
        trajectory: str = "noise",
        event_type: str = "uncertain",
        confidence: float = 0.0,
        expected_reversion: float = 0.0,
        parity: Optional[ParityInfo] = None,
        reasons: Optional[list] = None,
    ) -> None:
        self.token_id = token_id
        self.fixture_id = fixture_id
        self.price_before = price_before
        self.price_now = price_now
        self.change_pct = change_pct
        self.direction = direction
        self.timestamp = timestamp
        self.imbalance = imbalance
        self.trajectory = trajectory
        self.event_type = event_type
        self.confidence = confidence
        self.expected_reversion = expected_reversion
        self.parity = parity
        self.reasons = reasons or []
        self.detected_at = time.time()


class PriceTrigger:
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    REST_URL = "https://clob.polymarket.com/book"

    MIN_SPIKE_THRESHOLD_PCT = 2.0
    SPIKE_WINDOW_SECONDS = 10.0
    ALERT_COOLDOWN = 30.0
    MID_PRICE_WINDOW = 30

    def __init__(self) -> None:
        self._subscriptions: dict[str, dict[str, Any]] = {}
        self._price_history: dict[str, deque] = {}
        self._callbacks: list[Callable] = []
        self._running = False
        self._reconnect_delay = 1.0
        self._last_activity: float = time.time()
        self._use_websocket = True
        self._rest_client: Any = None
        self._ws_subscribed: set[str] = set()
        self._ws_pending_subscribe: set[str] = set()

        self._signal_classifier = SignalClassifier()
        self._last_book: dict[str, dict[str, Any]] = {}
        self._no_token_map: dict[str, str] = {}
        self._no_token_reverse: dict[str, str] = {}  # no_token_id -> primary token_id
        self._last_no_mid: dict[int, float] = {}

    def on_spike(self, callback: Callable) -> None:
        self._callbacks.append(callback)

    async def subscribe(self, token_id: str, fixture_id: int, baseline_price: float, no_token_id: str = "") -> None:
        self._subscriptions[token_id] = {
            "fixture_id": fixture_id,
            "baseline_price": baseline_price,
            "last_alert": 0.0,
            "best_bid": baseline_price - 0.005,
            "best_ask": baseline_price + 0.005,
            "bid_vol": 100.0,
            "ask_vol": 100.0,
        }
        self._price_history[token_id] = deque(maxlen=self.MID_PRICE_WINDOW)
        self._price_history[token_id].append((time.time(), baseline_price))
        if token_id in self._ws_subscribed:
            pass
        elif token_id not in self._ws_pending_subscribe:
            self._ws_pending_subscribe.add(token_id)

        if no_token_id:
            self._no_token_map[token_id] = no_token_id
            self._no_token_reverse[no_token_id] = token_id
            # Book-only WS subscription for the NO token: feeds parity analysis
            # (_check_spike's analyze_parity) via self._last_book, but never runs
            # spike-detection on the NO token's own price series — nothing
            # downstream is set up to trade off a spike keyed by the NO token.
            if no_token_id not in self._ws_subscribed and no_token_id not in self._ws_pending_subscribe:
                self._ws_pending_subscribe.add(no_token_id)

        logger.info(f"PriceTrigger subscribed to {token_id[:16]} (fixture={fixture_id})")

    def unsubscribe(self, token_id: str) -> None:
        self._subscriptions.pop(token_id, None)
        self._price_history.pop(token_id, None)
        self._last_book.pop(token_id, None)
        self._ws_subscribed.discard(token_id)
        self._ws_pending_subscribe.discard(token_id)
        no_tok = self._no_token_map.pop(token_id, None)
        if no_tok:
            self._no_token_reverse.pop(no_tok, None)
            self._last_book.pop(no_tok, None)
            self._ws_subscribed.discard(no_tok)
            self._ws_pending_subscribe.discard(no_tok)

    async def start(self) -> None:
        self._running = True
        import httpx
        self._rest_client = httpx.AsyncClient(timeout=10, http2=False)
        try:
            while self._running:
                try:
                    if self._use_websocket:
                        await self._run_websocket()
                    else:
                        await self._run_rest_polling()
                except Exception as e:
                    delay = self._reconnect_delay
                    logger.warning(f"PriceTrigger disconnected ({type(e).__name__}), retry in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    self._reconnect_delay = min(self._reconnect_delay * 1.5, 10.0)
        finally:
            if self._rest_client:
                await self._rest_client.aclose()

    async def stop(self) -> None:
        self._running = False

    async def _run_websocket(self) -> None:
        try:
            import websockets
        except ImportError:
            self._use_websocket = False
            await self._run_rest_polling()
            return

        async with websockets.connect(
            self.WS_URL, ping_interval=20, ping_timeout=10, max_size=2**20
        ) as ws:
            self._ws_subscribed.clear()
            all_token_ids = list(self._subscriptions.keys()) + list(self._no_token_reverse.keys())
            for token_id in all_token_ids:
                await ws.send(json.dumps({"type": "subscribe", "channel": "l2", "id": token_id}))
                self._ws_subscribed.add(token_id)
                self._ws_pending_subscribe.discard(token_id)

            self._reconnect_delay = 1.0
            self._last_activity = time.time()
            logger.info(f"PriceTrigger WebSocket LIVE — {len(self._ws_subscribed)} subscriptions")

            async for raw_msg in ws:
                if not self._running:
                    break
                self._last_activity = time.time()
                try:
                    self._handle_ws_message(raw_msg)
                except Exception as e:
                    logger.warning(f"WS msg handler: {e}")

                if self._ws_pending_subscribe:
                    pending = list(self._ws_pending_subscribe)
                    for token_id in pending:
                        try:
                            await ws.send(json.dumps({"type": "subscribe", "channel": "l2", "id": token_id}))
                            self._ws_subscribed.add(token_id)
                            self._ws_pending_subscribe.discard(token_id)
                        except Exception as e:
                            logger.warning(f"WS subscribe {token_id[:12]}: {e}")

    def _handle_ws_message(self, raw) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode()
        data = json.loads(raw)
        if not isinstance(data, list):
            data = [data]

        for msg in data:
            market = msg.get("market", "") or msg.get("id", "")

            if market in self._no_token_reverse:
                # Book-only tracking for parity analysis — store the raw book
                # so analyze_parity's self._last_book.get(no_tok) lookup works,
                # but never run spike-detection on the NO token's own series.
                self._last_book[market] = msg
                continue

            if market not in self._subscriptions:
                continue

            self._last_book[market] = msg

            bid, ask, bid_vol, ask_vol = _parse_book_bid_ask(msg)
            if bid <= 0 or ask <= 0 or bid >= ask:
                continue

            info = self._subscriptions[market]
            info["best_bid"] = bid
            info["best_ask"] = ask
            info["bid_vol"] = bid_vol
            info["ask_vol"] = ask_vol

            mid = (bid + ask) / 2
            self._price_history[market].append((time.time(), mid))
            self._check_spike(market, mid, info)

    async def _run_rest_polling(self) -> None:
        while self._running:
            if self._subscriptions:
                tasks = [self._poll_one_token(tid, info) for tid, info in list(self._subscriptions.items())]
                await asyncio.gather(*tasks, return_exceptions=True)
                self._last_activity = time.time()
            await asyncio.sleep(1.0)

    async def _poll_one_token(self, token_id: str, info: dict) -> None:
        try:
            r = await self._rest_client.get(self.REST_URL, params={"token_id": token_id})
            if r.status_code != 200:
                return
            data = r.json()
            self._last_book[token_id] = data

            bid, ask, bid_vol, ask_vol = _parse_book_bid_ask(data)
            if bid <= 0 or ask <= 0 or bid >= ask:
                return

            info["best_bid"] = bid
            info["best_ask"] = ask
            info["bid_vol"] = bid_vol
            info["ask_vol"] = ask_vol

            mid = (bid + ask) / 2

            no_tok = self._no_token_map.get(token_id)
            if no_tok:
                try:
                    nr = await self._rest_client.get(self.REST_URL, params={"token_id": no_tok})
                    if nr.status_code == 200:
                        self._last_book[no_tok] = nr.json()
                        n_bid, n_ask, _, _ = _parse_book_bid_ask(nr.json())
                        if n_bid > 0 and n_ask > 0:
                            self._last_no_mid[info["fixture_id"]] = (n_bid + n_ask) / 2
                except Exception:
                    pass

            self._price_history[token_id].append((time.time(), mid))
            self._check_spike(token_id, mid, info)
        except Exception as e:
            logger.debug(f"REST poll {token_id[:12]}: {e}")

    def _compute_rolling_volatility(self, history: deque) -> float:
        if len(history) < 5:
            return self.MIN_SPIKE_THRESHOLD_PCT

        changes = []
        for i in range(1, len(history)):
            prev = history[i - 1][1]
            curr = history[i][1]
            if prev > 0:
                changes.append(abs(curr - prev) / prev * 100)

        if not changes:
            return self.MIN_SPIKE_THRESHOLD_PCT

        n = len(changes)
        mean = sum(changes) / n
        variance = sum((c - mean) ** 2 for c in changes) / n
        per_step_std = math.sqrt(variance)

        window_steps = max(1, n)
        scaled_vol = per_step_std * math.sqrt(window_steps)

        return max(self.MIN_SPIKE_THRESHOLD_PCT, scaled_vol * 1.5)

    def _check_spike(self, token_id: str, current_mid: float, info: dict) -> None:
        history = self._price_history.get(token_id)
        if not history or len(history) < 2:
            return

        now = time.time()
        first_price = None
        for ts, price in history:
            if now - ts <= self.SPIKE_WINDOW_SECONDS:
                if first_price is None:
                    first_price = price
                break

        if first_price is None or first_price <= 0:
            return

        change_pct = ((current_mid - first_price) / first_price) * 100

        adaptive_threshold = self._compute_rolling_volatility(history)
        if abs(change_pct) < adaptive_threshold:
            return

        if now - info.get("last_alert", 0) < self.ALERT_COOLDOWN:
            return

        direction = "up" if change_pct > 0 else "down"
        info["last_alert"] = now

        bid_v = info.get("bid_vol", 1.0)
        ask_v = info.get("ask_vol", 1.0)
        imbalance = bid_v / max(ask_v, 1.0)

        raw_book = self._last_book.get(token_id) or {}
        best_ask = raw_book.get("asks")
        best_bid = raw_book.get("bids")
        if isinstance(best_ask, (list, tuple)) and len(best_ask) > 0 and isinstance(best_bid, (list, tuple)) and len(best_bid) > 0:
            _, ask_p = _parse_level(best_ask[0])
            bid_p, _ = _parse_level(best_bid[0])
            raw_spread = (ask_p - bid_p) / max(bid_p, 0.001) if bid_p > 0 else 0.001
        else:
            raw_spread = 0.001

        trajectory = self._signal_classifier.classify_trajectory(
            history, current_mid, raw_spread,
        )

        micro = self._signal_classifier.compute_micro_structure(raw_book)

        parity: Optional[ParityInfo] = None
        no_tok = self._no_token_map.get(token_id)
        no_book = self._last_book.get(no_tok) if no_tok else None
        if no_book:
            n_bid, n_ask, _, _ = _parse_book_bid_ask(no_book)
            if n_bid > 0 and n_ask > 0:
                no_mid = (n_bid + n_ask) / 2
                parity = self._signal_classifier.analyze_parity(current_mid, no_mid)

        classified = self._signal_classifier.analyze(
            trajectory=trajectory,
            imbalance=micro.get("imbalance", imbalance),
            spread=micro.get("spread", 0.0),
            cancel_rate=micro.get("cancel_rate", 0.0),
            parity=parity,
            price_change_pct=change_pct,
            direction=direction,
            token_id=token_id,
        )

        spike = PriceSpike(
            token_id=token_id,
            fixture_id=info["fixture_id"],
            price_before=round(first_price, 4),
            price_now=round(current_mid, 4),
            change_pct=round(change_pct, 1),
            direction=direction,
            timestamp=now,
            imbalance=micro.get("imbalance", imbalance),
            trajectory=classified.trajectory_type,
            event_type=classified.event_type,
            confidence=classified.confidence,
            expected_reversion=classified.expected_reversion,
            parity=parity,
            reasons=classified.reasons,
        )

        logger.info(
            f"SPIKE: fix={info['fixture_id']} {direction} {change_pct:.1f}% (th={adaptive_threshold:.1f}%) "
            f"traj={classified.trajectory_type} event={classified.event_type} conf={classified.confidence:.2f} "
            f"imb={micro.get('imbalance', imbalance):.1f} ({first_price:.3f}->{current_mid:.3f}) "
            f"{' '.join(classified.reasons[:3])}"
        )

        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    _safe_fire_coro(cb, spike)
                else:
                    cb(spike)
            except Exception as e:
                logger.error(f"Spike cb err: {e}")

    @property
    def is_connected(self) -> bool:
        return (time.time() - self._last_activity) < 60

    @property
    def connected_markets(self) -> int:
        return len(self._subscriptions)


def _safe_fire_coro(cb: Callable, spike: PriceSpike) -> None:
    task = asyncio.create_task(cb(spike))
    task.add_done_callback(_log_coro_error)


def _log_coro_error(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
        if exc:
            logger.error(f"Spike callback error: {exc}")
    except asyncio.CancelledError:
        pass
