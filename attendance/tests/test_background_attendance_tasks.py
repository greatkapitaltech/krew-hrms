"""
Part 3 Sec 3.4.1 of the Attendance performance plan -- late-come/early-
out/flexible-shortfall flagging deferred to a Celery task
(attendance/tasks.py), driven through the real clock_in()/clock_out()
views (attendance/views/clock_in_out.py), not the lower-level helpers
other test files already exercise directly. CELERY_TASK_ALWAYS_EAGER is
True by default (no REDIS_URL in test/CI), so .delay() below runs the
task synchronously in-process -- no worker needed.
"""

from datetime import date, datetime, time
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from attendance.models import (
    Attendance,
    AttendanceLateComeEarlyOut,
    AttendanceRuleSet,
    BackgroundAttendanceTask,
    BackgroundAttendanceTaskRetryLog,
)
from attendance.tasks import (
    enqueue_background_attendance_task,
    process_background_attendance_task,
    retry_background_task,
    sweep_stuck_background_tasks,
)
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user
from horilla_auth.models import HorillaUser


class BackgroundTaskTestBase(TestCase):
    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        cache.clear()  # per-task locks (attendance/tasks.py) live here too
        self.addCleanup(self._clear_context_vars)

    @staticmethod
    def _clear_context_vars():
        _thread_locals.request = None
        clear_selected_company()


class ClockInOutViewsDeferLateComeEarlyOutTests(BackgroundTaskTestBase):
    """Drives the real HTTP views, not clock_in_attendance_and_activity()
    directly, since that's exactly what changed in this pass."""

    def setUp(self):
        super().setUp()
        self.company = make_company("Deferred Flagging Co")
        self.shift = EmployeeShift.objects.create(employee_shift="Deferred Test Shift")
        self.shift.company_id.add(self.company)
        # "monday" etc. are seeded once per install -- .create() would
        # collide with the existing global row.
        self.day = EmployeeShiftDay.objects.get_or_create(day="monday")[0]
        EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(9, 0), end_time=time(17, 0),
        )
        user = make_user("deferred_emp", password="secret123")
        make_employee(
            company=self.company, email="deferred_emp@test.horilla",
            user=user, shift=self.shift,
        )
        self.user = HorillaUser.objects.get(pk=user.pk)

    def _clock(self, path, hour, minute):
        """
        clock_in()/clock_out() (attendance/views/clock_in_out.py) only
        honor an explicit date/time when a biometric device set
        request.date/request.time as real Python attributes on the
        request object -- a plain query param wouldn't be read at all.
        For an ordinary web request they fall back to date.today()/
        datetime.now() directly, so the only reliable way to control
        "now" from an HTTP-driven test is to freeze those built-ins for
        the duration of the request -- these subclass the real date/
        datetime, so everything else (strftime, timezone.make_aware,
        isinstance checks) keeps working unchanged; only .today()/.now()
        are overridden.
        """
        self.client.force_login(self.user)
        session = self.client.session
        session["selected_company"] = str(self.company.id)
        session.save()

        fixed_date = date(2026, 9, 21)  # a Monday
        fixed_aware_dt = timezone.make_aware(datetime(2026, 9, 21, hour, minute))

        class _FrozenDate(date):
            @classmethod
            def today(cls):
                return fixed_date

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 21, hour, minute)

        # clock_in()/clock_out() also compute the *actual* punch instant
        # via timezone.localtime() (django's own aware "now"), separately
        # from the plain datetime.now() used for the "%H:%M" comparisons
        # above -- both need freezing, or clock_out_attendance_and_activity()
        # ends up summing worked hours against the real wall-clock instant.
        with mock.patch("attendance.views.clock_in_out.date", _FrozenDate), \
             mock.patch("attendance.views.clock_in_out.datetime", _FrozenDateTime), \
             mock.patch(
                 "attendance.views.clock_in_out.timezone.localtime",
                 return_value=fixed_aware_dt,
             ):
            return self.client.get(path, HTTP_HX_REQUEST="true")

    def test_late_clock_in_creates_and_processes_a_background_task(self):
        # Shift starts 09:00 -- clocking in at 09:30 is late.
        from attendance.views.clock_in_out import clock_in_attendance_and_activity

        attendance = clock_in_attendance_and_activity(
            employee=self.user.employee_get,
            date_today=date(2026, 9, 21),
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="09:30",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=32400,
            end_time=61200,
            in_datetime=timezone.make_aware(datetime(2026, 9, 21, 9, 30)),
        )

        task = BackgroundAttendanceTask.objects.get(attendance=attendance)
        self.assertEqual(task.kind, BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT)
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_DONE)
        self.assertTrue(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="late_come",
            ).exists()
        )

    def test_shift_based_clock_out_defers_early_out_flagging(self):
        from attendance.views.clock_in_out import clock_in_attendance_and_activity

        attendance = clock_in_attendance_and_activity(
            employee=self.user.employee_get,
            date_today=date(2026, 9, 21),
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="09:00",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=32400,
            end_time=61200,
            in_datetime=timezone.make_aware(datetime(2026, 9, 21, 9, 0)),
        )
        # Clear the late-come task created by clock-in so we only look at
        # the clock-out one below.
        BackgroundAttendanceTask.objects.filter(attendance=attendance).delete()

        response = self._clock("/attendance/clock-out/", 16, 0)  # left an hour early
        self.assertEqual(response.status_code, 200)

        task = BackgroundAttendanceTask.objects.get(attendance=attendance)
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_DONE)
        self.assertTrue(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="early_out",
            ).exists()
        )

    def test_flexible_mode_clock_out_defers_shortfall_flagging(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True, total_work_hours_reference="8.00",
        )
        from attendance.views.clock_in_out import clock_in_attendance_and_activity

        attendance = clock_in_attendance_and_activity(
            employee=self.user.employee_get,
            date_today=date(2026, 9, 21),
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="09:00",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=32400,
            end_time=61200,
            in_datetime=timezone.make_aware(datetime(2026, 9, 21, 9, 0)),
        )
        BackgroundAttendanceTask.objects.filter(attendance=attendance).delete()

        response = self._clock("/attendance/clock-out/", 15, 0)  # 6:00 worked, short
        self.assertEqual(response.status_code, 200)

        attendance.refresh_from_db()
        task = BackgroundAttendanceTask.objects.get(attendance=attendance)
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_DONE)
        self.assertTrue(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )


class BackgroundTaskRetryTests(BackgroundTaskTestBase):
    def setUp(self):
        super().setUp()
        self.company = make_company("Retry Co")
        self.shift = EmployeeShift.objects.create(employee_shift="Retry Shift")
        # "monday" etc. are seeded once per install -- .create() would
        # collide with the existing global row.
        self.day = EmployeeShiftDay.objects.get_or_create(day="monday")[0]
        user = make_user("retry_emp", password="secret123")
        self.employee = make_employee(
            company=self.company, email="retry_emp@test.horilla",
            user=user, shift=self.shift,
        )
        self.attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 21),
            attendance_day=self.day,
            shift_id=self.shift,
            attendance_clock_in=time(9, 0),
            attendance_clock_in_date=date(2026, 9, 21),
        )

    def test_success_marks_the_task_done(self):
        task = enqueue_background_attendance_task(
            self.attendance, BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
        )
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_DONE)
        self.assertEqual(task.attempts, 0)
        self.assertIsNotNone(task.processed_at)

    def test_a_locked_task_is_skipped_not_double_processed(self):
        from attendance.tasks import _task_lock_key

        task = BackgroundAttendanceTask.objects.create(
            attendance=self.attendance,
            kind=BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
        )
        # Simulates a concurrent run already holding the lock (e.g. a
        # broker redelivering the same message, or the sweep re-enqueuing
        # a task a worker is still partway through).
        cache.add(_task_lock_key(task.pk), "1", 60)

        process_background_attendance_task(task.pk)

        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_PENDING)
        self.assertEqual(task.attempts, 0)

    def test_the_lock_is_released_after_a_successful_run(self):
        from attendance.tasks import _task_lock_key

        task = enqueue_background_attendance_task(
            self.attendance, BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
        )
        self.assertIsNone(cache.get(_task_lock_key(task.pk)))

    def test_the_lock_is_released_after_a_failed_run(self):
        from attendance.tasks import _task_lock_key

        task = BackgroundAttendanceTask.objects.create(
            attendance=self.attendance,
            kind=BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
        )
        with mock.patch.dict(
            "attendance.tasks._TASK_HANDLERS",
            {BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT: mock.Mock(
                side_effect=ValueError("boom")
            )},
        ):
            process_background_attendance_task(task.pk)

        self.assertIsNone(cache.get(_task_lock_key(task.pk)))

    def test_an_unknown_kind_fails_without_raising(self):
        task = BackgroundAttendanceTask.objects.create(
            attendance=self.attendance, kind="NOT_A_REAL_KIND",
        )
        process_background_attendance_task(task.pk)
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_FAILED)

    def test_a_handler_exception_increments_attempts_and_marks_failed(self):
        task = BackgroundAttendanceTask.objects.create(
            attendance=self.attendance,
            kind=BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
        )
        # _TASK_HANDLERS captures a direct function reference at import
        # time, so patching the module-level name _run_late_come_early_out
        # wouldn't affect what's already stored in the dict -- patch the
        # dict entry itself instead.
        with mock.patch.dict(
            "attendance.tasks._TASK_HANDLERS",
            {BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT: mock.Mock(
                side_effect=ValueError("boom")
            )},
        ):
            process_background_attendance_task(task.pk)
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_FAILED)
        self.assertEqual(task.attempts, 1)
        self.assertTrue(task.last_error)

    def test_sweep_reenqueues_failed_under_the_attempt_cap(self):
        task = BackgroundAttendanceTask.objects.create(
            attendance=self.attendance,
            kind=BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
            status=BackgroundAttendanceTask.STATUS_FAILED,
            attempts=2,
        )
        swept = sweep_stuck_background_tasks()
        self.assertEqual(swept, 1)
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_DONE)

    def test_sweep_does_not_reenqueue_a_task_at_the_attempt_cap(self):
        BackgroundAttendanceTask.objects.create(
            attendance=self.attendance,
            kind=BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
            status=BackgroundAttendanceTask.STATUS_FAILED,
            attempts=BackgroundAttendanceTask.MAX_ATTEMPTS,
        )
        swept = sweep_stuck_background_tasks()
        self.assertEqual(swept, 0)

    def test_manual_retry_resets_state_and_writes_exactly_one_log_row(self):
        task = BackgroundAttendanceTask.objects.create(
            attendance=self.attendance,
            kind=BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
            status=BackgroundAttendanceTask.STATUS_FAILED,
            attempts=BackgroundAttendanceTask.MAX_ATTEMPTS,
            last_error="boom",
        )
        retry_background_task(task, reset_by=self.employee, note="fixed the cause")

        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundAttendanceTask.STATUS_DONE)  # eager-processed
        self.assertEqual(
            BackgroundAttendanceTaskRetryLog.objects.filter(task=task).count(), 1
        )
        log = BackgroundAttendanceTaskRetryLog.objects.get(task=task)
        self.assertEqual(log.previous_status, BackgroundAttendanceTask.STATUS_FAILED)
        self.assertEqual(log.previous_attempts, BackgroundAttendanceTask.MAX_ATTEMPTS)
        self.assertEqual(log.reset_by, self.employee)
