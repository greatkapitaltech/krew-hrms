"""
Generic payroll helpers kept from the original calculator: gross / taxable
gross pay, the "if" condition check and the rate-based amount helpers.

Payslip figures now come from earnings and deductions
(``krew_payroll.methods.pay_components``); the old Allowance / Deduction
calculation that lived here was removed with those models.
"""

import contextlib
import operator

from django.apps import apps

from horilla.methods import get_horilla_model_class
from krew_payroll.methods.limits import compute_limit
from krew_payroll.models import models
from krew_payroll.models.models import Contract




def return_none(a, b):
    return None



operator_mapping = {
    "equal": operator.eq,
    "notequal": operator.ne,
    "lt": operator.lt,
    "gt": operator.gt,
    "le": operator.le,
    "ge": operator.ge,
    "icontains": operator.contains,
    "range": return_none,
}

def dynamic_attr(obj, attribute_path):
    """
    Retrieves the value of a nested attribute from a related object dynamically.

    Args:
        obj: The base object from which to start accessing attributes.
        attribute_path (str): The path of the nested attribute to retrieve, using
        double underscores ('__') to indicate relationship traversal.

    Returns:
        The value of the nested attribute if it exists, or None if it doesn't exist.
    """
    attributes = attribute_path.split("__")

    for attr in attributes:
        with contextlib.suppress(Exception):
            if isinstance(obj.first(), Contract):
                obj = obj.filter(is_active=True).first()

        obj = getattr(obj, attr, None)
        if obj is None:
            break
    return obj


def calculate_based_on_basic_pay(*_args, **kwargs):
    """
    Calculate the amount of an allowance or deduction based on the employee's
    basic pay with rate provided in the allowance or deduction object

    Args:
        employee (Employee): The employee object for whom to calculate the amount.
        start_date (datetime.date): The start date of the period for which to calculate the amount.
        end_date (datetime.date): The end date of the period for which to calculate the amount.
        component (Component): The allowance or deduction object that defines the rate or percentage
        to apply.

    Returns:
        The calculated allowance or deduction amount based on the employee's basic pay.

    """
    component = kwargs["component"]
    basic_pay = kwargs["basic_pay"]
    day_dict = kwargs["day_dict"]
    rate = component.rate
    amount = basic_pay * rate / 100
    amount = compute_limit(component, amount, day_dict)

    return amount


def calculate_based_on_gross_pay(*_args, **kwargs):
    """
    Calculate the amount of an allowance or deduction based on the employee's gross pay with rate
    provided in the allowance or deduction object

    Args:
        employee (Employee): The employee object for whom to calculate the amount.
        start_date (datetime.date): The start date of the period for which to calculate the amount.
        end_date (datetime.date): The end date of the period for which to calculate the amount.
        component (Component): The allowance or deduction object that defines the rate or percentage
        to apply.

    Returns:+-
        The calculated allowance or deduction amount based on the employee's gross pay.

    """

    component = kwargs["component"]
    day_dict = kwargs["day_dict"]
    gross_pay = calculate_gross_pay(**kwargs)
    rate = component.rate
    amount = gross_pay["gross_pay"] * rate / 100
    amount = compute_limit(component, amount, day_dict)
    return amount


def calculate_based_on_taxable_gross_pay(*_args, **kwargs):
    """
    Calculate the amount of an allowance or deduction based on the employee's taxable gross pay with
    rate provided in the allowance or deduction object

    Args:
        employee (Employee): The employee object for whom to calculate the amount.
        start_date (datetime.date): The start date of the period for which to calculate the amount.
        end_date (datetime.date): The end date of the period for which to calculate the amount.
        component (Component): The allowance or deduction object that defines the rate or percentage
        to apply.

    Returns:
        The calculated component amount based on the employee's taxable gross pay.

    """
    component = kwargs["component"]
    day_dict = kwargs["day_dict"]
    taxable_gross_pay = calculate_taxable_gross_pay(**kwargs)
    taxable_gross_pay = taxable_gross_pay["taxable_gross_pay"]
    rate = component.rate
    amount = taxable_gross_pay * rate / 100
    amount = compute_limit(component, amount, day_dict)
    return amount


def calculate_based_on_net_pay(component, net_pay, day_dict):
    """
    Calculates the amount of an allowance or deduction based on the net pay of an employee.

    Args:
        component (Allowance or Deduction): The allowance or deduction object.
        net_pay (float): The net pay of the employee.
        day_dict (dict): Dictionary containing working day details.

    Returns:
        float: The calculated amount of the component based on the net pay.
    """
    rate = float(component.rate)
    amount = net_pay * rate / 100
    amount = compute_limit(component, amount, day_dict)
    return amount


def calculate_based_on_attendance(*_args, **kwargs):
    """
    Calculates the amount of an allowance or deduction based on the attendance of an employee.

    Args:
        employee (Employee): The employee for whom the attendance is being calculated.
        start_date (date): The start date of the attendance period.
        end_date (date): The end date of the attendance period.
        component (Allowance or Deduction): The allowance or deduction object.
        day_dict (dict): Dictionary containing working day details.

    Returns:
        float: The calculated amount of the component based on the attendance.
    """

    if not apps.is_installed("attendance"):
        return 0

    Attendance = get_horilla_model_class(app_label="attendance", model="attendance")
    employee = kwargs["employee"]
    start_date = kwargs["start_date"]
    end_date = kwargs["end_date"]
    component = kwargs["component"]
    day_dict = kwargs["day_dict"]

    count = Attendance.objects.filter(
        employee_id=employee,
        attendance_date__range=(start_date, end_date),
        attendance_validated=True,
    ).count()
    amount = count * component.per_attendance_fixed_amount
    amount = compute_limit(component, amount, day_dict)
    return amount


def calculate_based_on_shift(*_args, **kwargs):
    """
    Calculates the amount of an allowance or deduction based on the employee's shift attendance.

    Args:
        employee (Employee): The employee for whom the shift attendance is being calculated.
        start_date (date): The start date of the attendance period.
        end_date (date): The end date of the attendance period.
        component (Allowance or Deduction): The allowance or deduction object.
        day_dict (dict): Dictionary containing working day details.

    Returns:
        float: The calculated amount of the component based on the shift attendance.
    """
    if not apps.is_installed("attendance"):
        return 0

    Attendance = get_horilla_model_class(app_label="attendance", model="attendance")
    employee = kwargs["employee"]
    start_date = kwargs["start_date"]
    end_date = kwargs["end_date"]
    component = kwargs["component"]
    day_dict = kwargs["day_dict"]

    shift_id = component.shift_id.id
    count = Attendance.objects.filter(
        employee_id=employee,
        shift_id=shift_id,
        attendance_date__range=(start_date, end_date),
        attendance_validated=True,
    ).count()
    amount = count * component.shift_per_attendance_amount

    amount = compute_limit(component, amount, day_dict)
    return amount


def calculate_based_on_overtime(*_args, **kwargs):
    """
    Calculates the amount of an allowance or deduction based on employee's overtime hours.

    Args:
        employee (Employee): The employee for whom the overtime is being calculated.
        start_date (date): The start date of the overtime period.
        end_date (date): The end date of the overtime period.
        component (Allowance or Deduction): The allowance or deduction object.
        day_dict (dict): Dictionary containing working day details.

    Returns:
        float: The calculated amount of the allowance or deduction based on the overtime hours.
    """
    if not apps.is_installed("attendance"):
        return 0

    Attendance = get_horilla_model_class(app_label="attendance", model="attendance")
    employee = kwargs["employee"]
    start_date = kwargs["start_date"]
    end_date = kwargs["end_date"]
    component = kwargs["component"]
    day_dict = kwargs["day_dict"]

    attendances = Attendance.objects.filter(
        employee_id=employee,
        attendance_date__range=(start_date, end_date),
        attendance_overtime_approve=True,
    )
    overtime = sum(attendance.overtime_second for attendance in attendances)
    amount_per_hour = component.amount_per_one_hr
    amount_per_second = amount_per_hour / (60 * 60)
    amount = overtime * amount_per_second
    amount = round(amount, 2)

    amount = compute_limit(component, amount, day_dict)

    return amount


def calculate_based_on_work_type(*_args, **kwargs):
    """
    Calculates the amount of an allowance or deduction based on the employee's
    attendance with a specific work type.

    Args:
        employee (Employee): The employee for whom the attendance is being considered.
        start_date (date): The start date of the attendance period.
        end_date (date): The end date of the attendance period.
        component (Allowance or Deduction): The allowance or deduction object.
        day_dict (dict): Dictionary containing working day details.

    Returns:
        float: The calculated amount of the allowance or deduction based on the
               attendance with the specified work type.
    """
    if not apps.is_installed("attendance"):
        return 0

    Attendance = get_horilla_model_class(app_label="attendance", model="attendance")
    employee = kwargs["employee"]
    start_date = kwargs["start_date"]
    end_date = kwargs["end_date"]
    component = kwargs["component"]
    day_dict = kwargs["day_dict"]

    work_type_id = component.work_type_id.id
    count = Attendance.objects.filter(
        employee_id=employee,
        work_type_id=work_type_id,
        attendance_date__range=(start_date, end_date),
        attendance_validated=True,
    ).count()
    amount = count * component.work_type_per_attendance_amount

    amount = compute_limit(component, amount, day_dict)

    return amount


def calculate_based_on_children(*_args, **kwargs):
    """
    Calculates the amount of an allowance or deduction based on the attendance of an employee.

    Args:
        employee (Employee): The employee for whom the attendance is being calculated.
        start_date (date): The start date of the attendance period.
        end_date (date): The end date of the attendance period.
        component (Allowance or Deduction): The allowance or deduction object.
        day_dict (dict): Dictionary containing working day details.

    Returns:
        float: The calculated amount of the component based on the attendance.
    """
    employee = kwargs["employee"]
    component = kwargs["component"]
    day_dict = kwargs["day_dict"]
    count = employee.children
    amount = count * component.per_children_fixed_amount
    amount = compute_limit(component, amount, day_dict)
    return amount

def calculate_gross_pay(*_args, **kwargs):
    """Gross pay = basic pay + total of the earnings lines."""
    basic_pay = kwargs["basic_pay"]
    total_allowance = kwargs["total_allowance"]
    return {"gross_pay": total_allowance + basic_pay, "basic_pay": basic_pay, "deductions": []}


def calculate_taxable_gross_pay(*_args, **kwargs):
    """Gross pay minus tax-free earnings and pre-tax deductions."""
    allowances = kwargs["allowances"]
    gross_pay = calculate_gross_pay(**kwargs)["gross_pay"]
    pretax = kwargs.get("pretax_deductions") or {"pretax_deductions": []}
    non_taxable_allowance_total = sum(
        allowance["amount"] for allowance in allowances["allowances"] if not allowance["is_taxable"]
    )
    pretax_deduction_total = sum(
        deduction["amount"] for deduction in pretax["pretax_deductions"] if deduction.get("is_pretax")
    )
    return {"taxable_gross_pay": gross_pay - non_taxable_allowance_total - pretax_deduction_total}


def if_condition_on(*_args, **kwargs):
    """
    Zero the amount unless the component's "if" condition holds. The condition
    compares basic pay (``if_choice == "basic_pay"``) or gross pay with the
    component's range / operator.
    """
    component = kwargs["component"]
    basic_pay = kwargs["basic_pay"]
    amount = float(kwargs["amount"])
    gross_pay = 0
    if component.if_choice != "basic_pay":
        gross_pay = calculate_gross_pay(**kwargs)["gross_pay"]
    condition_value = basic_pay if component.if_choice == "basic_pay" else gross_pay
    if component.if_condition == "range":
        if not component.start_range <= condition_value <= component.end_range:
            amount = 0
    else:
        operator_func = operator_mapping.get(component.if_condition)
        if not operator_func(condition_value, component.if_amount):
            amount = 0
    return amount
