"""
attendance/activity_log.py

log_attendance_activity() -- the single write path for
AttendanceActivityLog (attendance/models.py). Called explicitly at each
real action site (clock-in/out, Auto Punch-out, Validation auto-pass,
Overtime auto-approve, Regularization decisions, and -- once built --
Create Attendance/Bulk Import), never via a signal: see the model's own
docstring for why this is deliberately not django-auditlog/horilla_audit
-based.
"""


def log_attendance_activity(actor, action_type, affected_employees, what_changed, source):
    """
    Write one AttendanceActivityLog row.

    args:
        actor              : Employee instance, or None for a System/
                              automated action (Auto Punch-out, a
                              threshold-based auto-pass/auto-approve).
        action_type        : one of AttendanceActivityLog.ACTION_CHOICES.
        affected_employees : a single Employee instance, or an iterable
                              of Employee instances/pks -- normalized
                              here to a plain list of pks so a
                              single-employee action and a batch action
                              (Bulk Import, Batch Entry Create) both
                              write the same shape, just a
                              one-element vs. many-element list.
        what_changed       : short, human-readable description, e.g.
                              "Check-out changed from 6:05 PM to 6:45 PM".
        source              : which feature generated this entry, e.g.
                              "Clock In/Out", "Auto Punch-out",
                              "Regularization", "Create Attendance",
                              "Bulk Import".
    """
    from attendance.models import AttendanceActivityLog
    from employee.models import Employee

    if affected_employees is None:
        affected_ids = []
    elif isinstance(affected_employees, Employee):
        affected_ids = [affected_employees.pk]
    else:
        affected_ids = [
            emp.pk if isinstance(emp, Employee) else emp for emp in affected_employees
        ]

    return AttendanceActivityLog.objects.create(
        actor=actor,
        action_type=action_type,
        affected_employee_ids=affected_ids,
        what_changed=what_changed,
        source=source,
    )
