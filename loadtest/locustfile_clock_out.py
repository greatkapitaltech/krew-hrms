"""
Isolated load test for POST /api/attendance/clock-out/ ONLY -- mirrors
locustfile_clock_in.py exactly, see that file's docstring for the full
reasoning (one-shot-per-employee pool, no toggling, no expected 400s).

Setup (note --clock-in-all -- clock-out's precondition is a REAL
clocked-in state, produced by the exact same clock-in code path the API
uses, not a hand-rolled Attendance row):
    python manage.py seed_load_test_employees --count 12000
    python manage.py prep_load_test_pool --clock-in-all   # writes loadtest/tokens_clocked_in.json

Run:
    locust -f loadtest/locustfile_clock_out.py --host http://localhost:8000 \
        --headless -u 100 -r 20 --run-time 2m
"""

import json
import threading

from locust import HttpUser, between, task
from locust.exception import StopUser

TEST_LATITUDE = 12.9716
TEST_LONGITUDE = 77.5946

with open("loadtest/tokens_clocked_in.json") as f:
    _pool = json.load(f)

_pool_lock = threading.Lock()
_pool_index = 0


def _claim_next():
    global _pool_index
    with _pool_lock:
        if _pool_index >= len(_pool):
            return None
        entry = _pool[_pool_index]
        _pool_index += 1
        return entry


class ClockOutOnlyUser(HttpUser):
    wait_time = between(0.2, 0.5)

    @task
    def clock_out(self):
        entry = _claim_next()
        if entry is None:
            raise StopUser()

        with self.client.post(
            "/api/attendance/clock-out/",
            json={"latitude": TEST_LATITUDE, "longitude": TEST_LONGITUDE},
            headers={"Authorization": f"Bearer {entry['token']}"},
            name="/api/attendance/clock-out/",
            catch_response=True,
        ) as response:
            if response.status_code == 200 and response.json().get("message") == "Clocked-Out":
                response.success()
            else:
                response.failure(
                    f"unexpected response for {entry['username']}: "
                    f"{response.status_code} {response.text}"
                )
