"""
Load test for the clock-in/clock-out JWT API
(/api/attendance/clock-in/, /api/attendance/clock-out/).

Why one Locust "user" == one real Employee, not one shared account:
check_online() (employee/models.py) makes clock-in a toggle per employee
-- a second concurrent clock-in for someone already clocked in just falls
through with no attendance write at all, which would silently turn this
into a load test of the cheap early-return path instead of the real
write path. Each simulated user claims one fixture employee (seeded by
`python manage.py seed_load_test_employees --count N`, see that file's
docstring) and toggles in/out on its own, which is what actually exercises
clock_in_attendance_and_activity()/clock_out() under concurrent load.

Setup:
    python manage.py seed_load_test_employees --count 200   # >= peak concurrent users
    pip install locust                                       # not in requirements.txt -- dev-only

Run (against whichever server you want to measure -- dev runserver,
gunicorn, the full docker-compose stack):
    locust -f loadtest/locustfile.py --host http://localhost:8000

Then open http://localhost:8089 to set concurrent user count / spawn
rate, or run headless:
    locust -f loadtest/locustfile.py --host http://localhost:8000 \
        --headless -u 100 -r 10 --run-time 5m

FIXTURE_EMPLOYEE_COUNT below must match (or be <=) whatever --count you
seeded -- set via env var if you seeded a different number:
    LOAD_TEST_EMPLOYEE_COUNT=200 locust -f loadtest/locustfile.py ...
"""

import itertools
import os
import threading

from locust import HttpUser, between, task

FIXTURE_EMPLOYEE_COUNT = int(os.environ.get("LOAD_TEST_EMPLOYEE_COUNT", "50"))
USERNAME_PREFIX = "loadtest_emp_"
FIXTURE_PASSWORD = "LoadTest@1234"

# A fixed point well away from any real GeoFencing boundary this dev
# install might have configured -- check_geo_fence() only rejects a punch
# that's outside an *enforced* (start=True) rule; against the "Load Test
# Co" company seed_load_test_employees.py creates (which never gets a
# GeoFencing row), this coordinate is accepted unconditionally. Only
# latitude/longitude being present at all is actually required (Geo-tag
# is unconditional -- see check_geo_fence's own docstring).
TEST_LATITUDE = 12.9716
TEST_LONGITUDE = 77.5946

# Round-robins fixture usernames across every simulated user so two
# Locust users never fight over the same employee's clock-in/out state.
# Thread-safe: Locust runs each User's tasks on its own greenlet, but
# on_start for every user still races over this same iterator.
_username_pool = itertools.cycle(
    f"{USERNAME_PREFIX}{i:04d}" for i in range(1, FIXTURE_EMPLOYEE_COUNT + 1)
)
_username_pool_lock = threading.Lock()


def _claim_username():
    with _username_pool_lock:
        return next(_username_pool)


class ClockInOutUser(HttpUser):
    # Real employees don't punch back-to-back -- this paces each
    # simulated user's own in/out cycle. Set wait_time = constant(0) (add
    # `from locust import constant`) instead for a pure max-throughput
    # stress run rather than a realistic-usage simulation.
    wait_time = between(1, 3)

    def on_start(self):
        self.username = _claim_username()
        response = self.client.post(
            "/api/auth/login/",
            json={"username": self.username, "password": FIXTURE_PASSWORD},
            name="/api/auth/login/",
        )
        response.raise_for_status()
        token = response.json()["access"]
        self.client.headers.update({"Authorization": f"Bearer {token}"})
        # Local tracking only -- the server's own check_online() is the
        # real source of truth, but starting every fixture employee
        # clocked-out (true after seeding/a flush) lets this drive the
        # toggle without an extra lookup call per iteration.
        self.is_clocked_in = False

    @task
    def toggle_clock(self):
        payload = {"latitude": TEST_LATITUDE, "longitude": TEST_LONGITUDE}
        if not self.is_clocked_in:
            endpoint, expected = "/api/attendance/clock-in/", "Clocked-In"
        else:
            endpoint, expected = "/api/attendance/clock-out/", "Clocked-Out"

        with self.client.post(
            endpoint, json=payload, name=endpoint, catch_response=True
        ) as response:
            if response.status_code == 200 and response.json().get("message") == expected:
                self.is_clocked_in = not self.is_clocked_in
                response.success()
            else:
                # A real anomaly, not expected business-logic state (this
                # user's own local toggle should always match what the
                # server expects next) -- e.g. two Locust users landed on
                # the same fixture employee because FIXTURE_EMPLOYEE_COUNT
                # is smaller than the actual concurrent user count.
                response.failure(
                    f"unexpected response for {self.username}: "
                    f"{response.status_code} {response.text}"
                )
