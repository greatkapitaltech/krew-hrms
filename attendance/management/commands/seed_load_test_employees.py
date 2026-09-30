"""
Management command: seed_load_test_employees
----------------------------------------------
Creates N employees purely for load-testing the clock-in/clock-out API
(see loadtest/locustfile.py) -- each one needs its own real, distinct
Employee/HorillaUser, since check_online() means a single employee can't
be meaningfully clocked in twice in a row the way a load test hammers an
endpoint; only many independent employees cycling in/out produces
realistic concurrent write load.

All fixtures share one company ("Load Test Co") and one shift ("Load
Test Shift", schedule 00:00-23:59 every day) so a clock-in/out succeeds
at any time of day the load test happens to run, and are tagged via the
FIXTURE_EMAIL_DOMAIN the same way create_summary_fixtures.py tags its
own -- --flush deletes by that tag, never a blind employee wipe.

Run:
    python manage.py seed_load_test_employees              # 50 employees
    python manage.py seed_load_test_employees --count 200
    python manage.py seed_load_test_employees --flush       # delete fixtures first
"""

from django.core.management.base import BaseCommand
from django.db import transaction

FIXTURE_EMAIL_DOMAIN = "fixture.loadtest.test"
USERNAME_PREFIX = "loadtest_emp_"
FIXTURE_PASSWORD = "LoadTest@1234"
COMPANY_NAME = "Load Test Co"
SHIFT_NAME = "Load Test Shift"


class Command(BaseCommand):
    help = "Seed (or flush) employees dedicated to clock-in/out API load testing."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=50)
        parser.add_argument(
            "--flush", action="store_true", help="Delete existing load-test fixtures first."
        )

    def handle(self, *args, **options):
        from attendance.models import (
            Attendance,
            AttendanceActivity,
            AttendanceLateComeEarlyOut,
        )
        from base.models import Company, EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
        from employee.models import Employee, EmployeeWorkInformation
        from horilla_auth.models import HorillaUser
        from payroll.models.models import Contract

        count = options["count"]

        if options["flush"]:
            # A load-test run's whole point is generating Attendance/
            # AttendanceActivity rows -- all PROTECT their employee (or
            # attendance) FK, same as Contract (auto-created by
            # Employee.save()'s own signal), so a plain
            # Employee.objects.filter(...).delete() fails once any
            # fixture has ever actually clocked in/out. Deleted in FK
            # dependency order: leaves first, Employee/User last.
            fixture_employees = Employee.objects.filter(
                email__iendswith=f"@{FIXTURE_EMAIL_DOMAIN}"
            )
            fixture_users = HorillaUser.objects.filter(
                username__istartswith=USERNAME_PREFIX
            )
            fixture_attendance = Attendance.objects.filter(
                employee_id__in=fixture_employees
            )
            deleted_lce, _d = AttendanceLateComeEarlyOut.objects.filter(
                attendance_id__in=fixture_attendance
            ).delete()
            deleted_activities, _d = AttendanceActivity.objects.filter(
                employee_id__in=fixture_employees
            ).delete()
            deleted_attendance, _d = fixture_attendance.delete()
            deleted_contracts, _d = Contract.objects.filter(
                employee_id__in=fixture_employees
            ).delete()
            deleted_employees, _d = fixture_employees.delete()
            deleted_users, _d = fixture_users.delete()
            self.stdout.write(
                self.style.WARNING(
                    # deleted_employees is Employee.delete()'s *total*
                    # cascaded row count (profile/bank-detail one-to-ones
                    # etc.), not literally the employee count -- shown
                    # as-is since Django's delete() doesn't split it out.
                    f"Deleted {deleted_employees} row(s) total for fixture "
                    f"employees, {deleted_users} user(s), {deleted_attendance} "
                    f"attendance row(s), {deleted_activities} activity row(s), "
                    f"{deleted_lce} late-come/early-out row(s), "
                    f"{deleted_contracts} contract(s)."
                )
            )
            if not options["count"]:
                return

        with transaction.atomic():
            company, _ = Company.objects.get_or_create(
                company=COMPANY_NAME,
                defaults={
                    "hq": True,
                    "address": "1 Load Test St",
                    "country": "US",
                    "state": "CA",
                    "city": "LA",
                    "zip": "90001",
                },
            )

            shift, _ = EmployeeShift.objects.get_or_create(employee_shift=SHIFT_NAME)
            shift.company_id.add(company)
            for day_name, _label in EmployeeShiftDay._meta.get_field("day").choices:
                day, _ = EmployeeShiftDay.objects.get_or_create(day=day_name)
                day.company_id.add(company)
                EmployeeShiftSchedule.objects.update_or_create(
                    shift_id=shift,
                    day=day,
                    defaults={
                        "start_time": "00:00",
                        "end_time": "23:59",
                        "minimum_working_hour": "00:00",
                    },
                )

            created = 0
            for i in range(1, count + 1):
                username = f"{USERNAME_PREFIX}{i:04d}"
                email = f"{username}@{FIXTURE_EMAIL_DOMAIN}"
                if Employee.objects.filter(email=email).exists():
                    continue
                user = HorillaUser.objects.create_user(
                    username=username, email=email, password=FIXTURE_PASSWORD
                )
                employee = Employee(
                    employee_first_name="LoadTest",
                    employee_last_name=f"Emp{i:04d}",
                    email=email,
                    phone=f"9{i:09d}"[:10],
                    employee_user_id=user,
                )
                employee.save()
                updated = EmployeeWorkInformation.objects.filter(employee_id=employee).update(
                    company_id=company, shift_id=shift
                )
                if updated == 0:
                    EmployeeWorkInformation.objects.create(
                        employee_id=employee, company_id=company, shift_id=shift
                    )
                created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Ready: {count} fixture employee(s) under company {COMPANY_NAME!r}, "
                f"shift {SHIFT_NAME!r} ({created} newly created). "
                f"Usernames {USERNAME_PREFIX}0001.._{count:04d}, password {FIXTURE_PASSWORD!r}."
            )
        )
