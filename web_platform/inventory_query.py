"""Bounded anonymous inventory races, isolated from booking and captcha clients."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logging
import os
import re
import threading
import time

import httpx

from booking_api import _times_from_ok_area, seats_from_ok_area
from candidate_pool import find_matching_slot
from http_capture import capture_context, record_request, submit_with_capture_context
from site_config import get_channel


class InventoryError(RuntimeError):
    pass


def query_budget():
    try:
        return max(0.1, min(5.0, float(os.getenv("BOOKING_QUERY_BUDGET_MS", "800")) / 1000))
    except ValueError:
        return 0.8


def make_client(channel):
    return httpx.Client(
        trust_env=False, follow_redirects=False, timeout=0.8,
        headers={"User-Agent": get_channel(channel).user_agent,
                 "X-Requested-With": "XMLHttpRequest"},
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
    )


class ChannelClientPool:
    def __init__(self, channel, capacity=6, factory=make_client):
        self.channel = channel
        self.capacity = capacity
        self.factory = factory
        self.available = []
        self.created = 0
        self.closed = False
        self.condition = threading.Condition()

    def acquire(self, deadline):
        with self.condition:
            while True:
                if self.closed:
                    raise InventoryError("query pool closed")
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise InventoryError("query client deadline")
                if self.available:
                    return self.available.pop()
                if self.created < self.capacity:
                    self.created += 1
                    break
                self.condition.wait(remaining)
        try:
            return self.factory(self.channel)
        except Exception:
            with self.condition:
                self.created -= 1
                self.condition.notify()
            raise

    def release(self, client, broken=False):
        with self.condition:
            discard = broken or self.closed or client.is_closed
            if discard:
                self.created -= 1
            else:
                self.available.append(client)
            self.condition.notify()
        if discard:
            client.close()

    def warm(self, count=3):
        clients = []
        try:
            for unused in range(min(count, self.capacity)):
                clients.append(self.acquire(time.perf_counter() + 10))
        finally:
            for client in clients:
                self.release(client)

    def close(self):
        with self.condition:
            self.closed = True
            clients, self.available = self.available, []
            self.created -= len(clients)
            self.condition.notify_all()
        for client in clients:
            client.close()


def check_deadline(deadline, cancelled):
    remaining = deadline - time.perf_counter()
    if cancelled.is_set() or remaining <= 0:
        raise InventoryError("query cancelled or deadline exceeded")
    return remaining


def get_inventory_response(client, channel, path, params, deadline, cancelled):
    remaining = check_deadline(deadline, cancelled)
    url = httpx.URL(get_channel(channel).api_url(path),
                    params={**params, "_": time.time_ns() // 1_000_000})
    response = record_request(client, "GET", str(url), timeout=remaining,
                              capture_meta={"attempt": 1, "phase": "inventory"})
    check_deadline(deadline, cancelled)
    if response.status_code != 200 or not response.content:
        raise InventoryError(f"HTTP {response.status_code}, bytes={len(response.content)}")
    return response


def object_rows(response):
    try:
        data = response.json()
    except ValueError as error:
        raise InventoryError("invalid inventory JSON") from error
    if (not isinstance(data, dict) or str(data.get("result")) != "1"
            or not isinstance(data.get("object"), list)):
        raise InventoryError("invalid inventory result")
    return data["object"]


def fetch_inventory(client, channel, venue_id, slot_times, date, deadline, cancelled):
    began = time.perf_counter()
    if channel == "8080":
        response = get_inventory_response(client, channel, "/product/findOkArea.html",
                                          {"s_date": date, "serviceid": venue_id}, deadline, cancelled)
        rows = object_rows(response)
        for row in rows:
            if (not isinstance(row, dict) or not row.get("stockid") or not row.get("id")
                    or not isinstance(row.get("stock"), dict)
                    or not row["stock"].get("time_no") or not isinstance(row.get("status"), int)):
                raise InventoryError("invalid inventory row")
            try:
                int(row.get("name") or row.get("num"))
            except (TypeError, ValueError) as error:
                raise InventoryError("invalid court number") from error
        times = _times_from_ok_area(rows)
        stage_finished = time.perf_counter()
        seats_by_stock = {
            str(slot["ID"]): seats_from_ok_area(rows, slot["ID"])
            for slot in times if any(find_matching_slot([slot], selected) for selected in slot_times)
        }
    else:
        response = get_inventory_response(client, channel, "/product/findtime.html",
                                          {"type": "day", "s_dates": date, "serviceid": venue_id},
                                          deadline, cancelled)
        times = object_rows(response)
        if any(not isinstance(slot, dict) or not slot.get("ID")
               or not isinstance(slot.get("TIME_NO"), str) or not slot["TIME_NO"] for slot in times):
            raise InventoryError("invalid times row")
        stage_finished = time.perf_counter()
        seats_by_stock = {}
        for selected in slot_times:
            slot = find_matching_slot(times, selected)
            if slot is None or str(slot["ID"]) in seats_by_stock:
                continue
            response = get_inventory_response(client, channel, "/seat/seat.html",
                                              {"id": venue_id, "type": 2, "stockid": slot["ID"],
                                               "json": "html"}, deadline, cancelled)
            match = re.search(r'value="([^"]*)"\s+id="txt_seatid"', response.text)
            if not match or not match[1]:
                raise InventoryError("missing seat inventory")
            seats = []
            for item in filter(None, match[1].split(",")):
                parts = item.split("_")
                if len(parts) < 3 or not parts[0].isdigit() or not parts[1] or parts[-1] not in {"0", "1", "2"}:
                    raise InventoryError("invalid seat inventory")
                seats.append({"courtNumber": int(parts[0]), "seatId": parts[1],
                              "available": parts[-1] == "1"})
            if not seats:
                raise InventoryError("empty seat inventory")
            seats_by_stock[str(slot["ID"])] = seats
    check_deadline(deadline, cancelled)
    finished = time.perf_counter()
    return {"ok": True, "times": times, "seats_by_stock": seats_by_stock, "channel": channel,
            "times_ms": (stage_finished - began) * 1000,
            "seats_ms": (finished - stage_finished) * 1000}


class InventoryService:
    def __init__(self, capacity=6, factory=make_client, fetcher=fetch_inventory, backlog=None):
        self.pools = {channel: ChannelClientPool(channel, capacity, factory) for channel in ("8080", "80")}
        self.executors = {channel: ThreadPoolExecutor(max_workers=capacity, thread_name_prefix=f"inventory-{channel}")
                          for channel in self.pools}
        pending_limit = capacity + (capacity * 2 if backlog is None else backlog)
        self.permits = {channel: threading.BoundedSemaphore(pending_limit) for channel in self.pools}
        self.fetcher = fetcher

    def warm(self):
        for pool in self.pools.values():
            try:
                pool.warm()
            except Exception as error:
                logging.getLogger(__name__).warning("Query client warmup failed: %s", type(error).__name__)

    def close(self):
        for pool in self.pools.values():
            pool.close()
        for executor in self.executors.values():
            executor.shutdown(wait=True, cancel_futures=True)

    def _fetch(self, channel, venue_id, slots, date, deadline, cancelled):
        pool = self.pools[channel]
        check_deadline(deadline, cancelled)
        client = pool.acquire(deadline)
        broken = False
        try:
            check_deadline(deadline, cancelled)
            with capture_context(inventory_channel=channel, venue_id=venue_id):
                return self.fetcher(client, channel, venue_id, slots, date, deadline, cancelled)
        except httpx.TransportError:
            broken = True
            raise
        finally:
            pool.release(client, broken)

    def _submit(self, channel, venue_id, slots, date, deadline, cancelled):
        permit = self.permits[channel]
        if not permit.acquire(blocking=False):
            raise InventoryError("query channel capacity reached")
        try:
            future = submit_with_capture_context(self.executors[channel], self._fetch,
                                                  channel, venue_id, slots, date, deadline, cancelled)
        except Exception:
            permit.release()
            raise
        future.add_done_callback(lambda completed: permit.release())
        return future

    def fetch(self, venues, slots, date, log, on_venue=None, budget=None):
        began = time.perf_counter()
        deadline = began + (query_budget() if budget is None else budget)
        pending = {}
        results = {}
        stops = {venue["id"]: threading.Event() for venue in venues}
        errors = {venue["id"]: [] for venue in venues}
        names = {venue["id"]: venue["name"] for venue in venues}
        try:
            for venue_id in stops:
                for channel in ("8080", "80"):
                    try:
                        future = self._submit(channel, venue_id, slots, date, deadline, stops[venue_id])
                        pending[future] = (venue_id, channel)
                    except Exception as error:
                        errors[venue_id].append(f"{channel}:{type(error).__name__}:{error}")
            while pending and len(results) < len(stops):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                done, unused = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
                if not done:
                    break
                for future in sorted(done, key=lambda item: pending[item][1] != "8080"):
                    venue_id, channel = pending.pop(future)
                    if venue_id in results:
                        continue
                    try:
                        result = future.result()
                        if not result.get("ok"):
                            raise InventoryError("invalid venue inventory")
                    except Exception as error:
                        errors[venue_id].append(f"{channel}:{type(error).__name__}:{error}")
                        continue
                    if time.perf_counter() >= deadline:
                        break
                    result["winner_ms"] = (time.perf_counter() - began) * 1000
                    results[venue_id] = result
                    stops[venue_id].set()
                    for other, (other_venue, other_channel) in pending.items():
                        if other_venue == venue_id:
                            other.cancel()
                    if on_venue is not None:
                        on_venue(venue_id, result)
            for venue_id in stops:
                if venue_id not in results:
                    results[venue_id] = {"ok": False}
                    errors[venue_id].extend(f"{channel}:deadline exceeded" for other_venue, channel in pending.values()
                                            if other_venue == venue_id)
                    reason = "; ".join(errors[venue_id]) or "deadline exceeded"
                    log(f"[Query] {names[venue_id]} dual-channel failed: {reason[:240]}")
        finally:
            for stop in stops.values():
                stop.set()
            for future in pending:
                future.cancel()
        successful = [result for result in results.values() if result["ok"]]
        return results, {"ok": len(successful), "failed": len(results) - len(successful),
                         "times_ms": max((result["times_ms"] for result in successful), default=0),
                         "seats_ms": max((result["seats_ms"] for result in successful), default=0),
                         "total_ms": (time.perf_counter() - began) * 1000,
                         "winners": {venue_id: result["channel"] for venue_id, result in results.items() if result["ok"]}}
