"""
decorators.py

Custom decorators for permission and manager checks in the application.
"""

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render

from employee.models import Employee
from horilla.config import logger
from horilla.methods import handle_no_permission
from recruitment.models import Recruitment, Stage


def decorator_with_arguments(decorator):
    """
    Decorator that allows decorators to accept arguments and keyword arguments.

    Args:
        decorator (function): The decorator function to be wrapped.

    Returns:
        function: The wrapper function.

    """

    def wrapper(*args, **kwargs):
        """
        Wrapper function that captures the arguments and keyword arguments.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            function: The inner wrapper function.

        """

        def inner_wrapper(func):
            """
            Inner wrapper function that applies the decorator to the function.

            Args:
                func (function): The function to be decorated.

            Returns:
                function: The decorated function.

            """
            return decorator(func, *args, **kwargs)

        return inner_wrapper

    return wrapper


@decorator_with_arguments
def manager_can_enter(function, perm=None, perms=None):
    """
    Decorator that checks if the user has the specified permission(s) or is a manager.

    Args:
        perm (str): A single permission string.
        perms (list): A list of permission strings.

    Returns:
        function: The decorated view.
    """

    def _function(request, *args, **kwargs):
        user = request.user
        employee = Employee.objects.filter(employee_user_id=user).first()

        is_manager = (
            Stage.objects.filter(stage_managers=employee).exists()
            or Recruitment.objects.filter(recruitment_managers=employee).exists()
        )

        # Combine perm and perms into one list to check
        all_perms = []
        if perm:
            all_perms.append(perm)
        if perms:
            all_perms.extend(perms)

        has_required_perm = any(user.has_perm(p) for p in all_perms)

        if has_required_perm or is_manager:
            return function(request, *args, **kwargs)

        return handle_no_permission(request)

    return _function


@decorator_with_arguments
def all_manager_can_enter(function, perm):
    """
    Decorator that checks if the user has the specified permission or is a manager.

    Args:
        perm (str): The permission to check.

    Returns:
        function: The decorated function.

    Raises:
        None

    """

    def _function(request, *args, **kwargs):
        """
        Inner function that performs the permission and manager check.

        Args:
            request (HttpRequest): The request object.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            HttpResponse: The response from the decorated function.

        """
        user = request.user
        employee = Employee.objects.filter(employee_user_id=user).first()
        is_manager = (
            Stage.objects.filter(stage_managers=employee).exists()
            or Recruitment.objects.filter(recruitment_managers=employee).exists()
            or request.user.employee_get.onboardingstage_set.exists()
            or request.user.employee_get.onboarding_task.exists()
        )
        if user.has_perm(perm) or is_manager:
            return function(request, *args, **kwargs)

        return handle_no_permission(request)

    return _function


@decorator_with_arguments
def recruitment_manager_can_enter(function, perm):
    """
    Decorator that checks if the user has the specified permission or is a recruitment manager.

    Args:
        perm (str): The permission to check.

    Returns:
        function: The decorated function.

    Raises:
        None

    """

    def _function(request, *args, **kwargs):
        """
        Inner function that performs the permission and manager check.

        Args:
            request (HttpRequest): The request object.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            HttpResponse: The response from the decorated function.
        """
        user = request.user
        employee = Employee.objects.filter(employee_user_id=user).first()
        is_manager = Recruitment.objects.filter(recruitment_managers=employee).exists()
        if user.has_perm(perm) or is_manager:
            return function(request, *args, **kwargs)

        return handle_no_permission(request)

    return _function


def candidate_login_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):

        allow_func = False
        if request.user.has_perm("recruitment.view_candidate"):
            allow_func = True
        if request.user:
            if request.user.is_authenticated:
                if (
                    request.user.employee_get.stage_set.exists()
                    or request.user.employee_get.recruitment_set.exists()
                ):
                    allow_func = True

        if "candidate_id" in request.session:
            allow_func = True

        if allow_func:
            try:
                func = view_func(request, *args, **kwargs)
            except KeyError:
                raise
            except Exception as e:
                logger.error(e)
                if not settings.DEBUG:
                    messages.error(request, str(e))
                    return render(request, "went_wrong.html", status=404)
                raise e
            return func
        return redirect("candidate-login/")

    return _wrapped_view


def drive_manager_required(view):
    """
    PRD: adding, editing, removing and staffing stages are drive-level actions
    (the opening's Managers / HR), not Stage Manager actions. Resolves the job
    opening from the stage in the URL (stage_id / sid / pk) or a posted
    recruitment_id, then refuses anyone who does not manage it.
    """
    from horilla.http import HorillaRedirect
    from django.utils.translation import gettext as _
    from recruitment.services.authorization import is_drive_manager

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        opening = None
        stage_pk = kwargs.get("stage_id") or kwargs.get("sid") or kwargs.get("pk")
        if stage_pk:
            stage = Stage.objects.filter(pk=stage_pk).select_related("recruitment_id").first()
            opening = stage.recruitment_id if stage else None
        if opening is None:
            rec_id = request.POST.get("recruitment_id") or request.GET.get("recruitment_id")
            if rec_id and str(rec_id).isdigit():
                opening = Recruitment.objects.filter(pk=rec_id).first()
        if opening is not None and not is_drive_manager(request.user, opening):
            messages.error(
                request,
                _("Only this job opening's managers can add, edit or remove stages."),
            )
            if request.headers.get("HX-Request") == "true":
                return HorillaRedirect(request)
            return handle_no_permission(request)
        return view(request, *args, **kwargs)

    return wrapper

