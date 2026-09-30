"""
Isolated load test for POST /api/attendance/clock-in/ ONLY -- no
clock-out calls at all, so no "Already clocked-in"/"Already clocked-out"
business-logic response ever shows up in the results.

Why this needs its own pool, not the toggle-style locustfile.py: an
employee can only clock in once per day (check_online() correctly 400s
a repeat) -- sustaining N requests/sec against clock-in alone, with zero
of those expected 400s, means every single request must land on a
distinct employee nobody has clocked in yet today. Pool size must
therefore cover the whole run: roughly (target_rps * planned duration in
seconds) employees, never reused.

Setup:
    python manage.py seed_load_test_employees --count 12000   # >= target_rps * duration_seconds
    python manage.py prep_load_test_pool                      # writes loadtest/tokens.json
                                                                # (mints JWTs directly, no HTTP --
                                                                #  keeps this fast even for a large pool)

Run:
    locust -f loadtest/locustfile_clock_in.py --host http://localhost:8000 \
        --headless -u 100 -r 20 --run-time 2m

Tune -u (concurrent users) and wait_time below together to approach a
target total RPS -- Locust has no single "--rps" flag; total throughput
is roughly users / (wait_time + response_time). Watch the live RPS in
the web UI (omit --headless) or the CSV/console summary to converge on
100 rps. The pool empties as the run progresses (each entry used once);
if it runs out before --run-time elapses, users stop early (via
StopUser) rather than reuse an employee and produce a fake "Already
clocked-in" -- reseed a bigger pool rather than re-running against the
same one.
"""

import json
import threading

from locust import HttpUser, between, task
from locust.exception import StopUser

TEST_LATITUDE = 12.9716
TEST_LONGITUDE = 77.5946

with open("loadtest/tokens.json") as f:
    _pool = json.load(f)

_pool_lock = threading.Lock()
_pool_index = 0


def _claim_next():
    """Pops the next never-used (username, token) pair, or None once the pool is exhausted."""
    global _pool_index
    with _pool_lock:
        if _pool_index >= len(_pool):
            return None
        entry = _pool[_pool_index]
        _pool_index += 1
        return entry


class ClockInOnlyUser(HttpUser):
    # Paces each request; total throughput is driven by -u concurrent
    # users divided by (this wait plus real response time) -- lower this
    # or raise -u to push RPS up toward a target.
    wait_time = between(0.2, 0.5)

    @task
    def clock_in(self):
        entry = _claim_next()
        if entry is None:
            raise StopUser()

        with self.client.post(
            "/api/attendance/clock-in/",
            json={"latitude": TEST_LATITUDE, "longitude": TEST_LONGITUDE},
            headers={"Authorization": f"Bearer {entry['token']}"},
            name="/api/attendance/clock-in/",
            catch_response=True,
        ) as response:
            if response.status_code == 200 and response.json().get("message") == "Clocked-In":
                response.success()
            else:
                # A real anomaly -- every entry in the pool is used
                # exactly once, so this should never legitimately be an
                # "Already clocked-in" -- if it is, the pool has fewer
                # truly-fresh employees than expected (e.g. reused from
                # an earlier run without reseeding).
                response.failure(
                    f"unexpected response for {entry['username']}: "
                    f"{response.status_code} {response.text}"
                )
