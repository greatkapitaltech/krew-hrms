from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from employee.models import BonusPoint, Employee
from leave.models import LeaveType
from krew_payroll.models.models import (
    Contract,
    LoanAccount,
    Payslip,
    Reimbursement,
    ReimbursementMultipleAttachment,
)
from krew_payroll.models.tax_models import TaxBracket


class PayslipSerializer(serializers.ModelSerializer):
    employee_first_name = serializers.CharField(
        source="employee_id.employee_first_name", read_only=True
    )
    employee_last_name = serializers.CharField(
        source="employee_id.employee_last_name", read_only=True
    )
    shift_name = serializers.CharField(source="shift_id.employee_shift", read_only=True)
    badge_id = serializers.CharField(source="employee_id.badge_id", read_only=True)
    employee_profile_url = serializers.SerializerMethodField(read_only=True)
    department_name = serializers.CharField(
        source="employee_id.employee_work_info.department_id.department", read_only=True
    )
    bank_account_check_number = serializers.CharField(
        source="employee_id.employee_bank_details.account_number", read_only=True
    )

    class Meta:
        model = Payslip
        fields = "__all__"
        # exclude = ['reference',
        #            'sent_to_employee',
        #            'installment_ids', 'created_at']

    def get_employee_profile_url(self, obj):
        try:
            employee_profile = obj.employee_id.employee_profile
            return employee_profile.url
        except:
            return None


class ContractSerializer(serializers.ModelSerializer):
    employee_first_name = serializers.CharField(
        source="employee_id.employee_first_name", read_only=True
    )
    employee_last_name = serializers.CharField(
        source="employee_id.employee_last_name", read_only=True
    )
    shift_name = serializers.CharField(source="shift_id.employee_shift", read_only=True)
    badge_id = serializers.CharField(source="employee_id.badge_id", read_only=True)
    employee_profile_url = serializers.SerializerMethodField(read_only=True)

    job_position_name = serializers.CharField(
        source="job_position_id.job_position", read_only=True
    )
    job_role_name = serializers.CharField(source="job_role_id.job_role", read_only=True)
    department_name = serializers.CharField(
        source="department_id.department", read_only=True
    )
    shift_name = serializers.CharField(source="shift_id.employee_shift", read_only=True)
    work_type_name = serializers.CharField(
        source="work_type_id.work_type", read_only=True
    )

    def get_employee_profile_url(self, obj):
        try:
            employee_profile = obj.employee_id.employee_profile
            return employee_profile.url
        except:
            return None

    class Meta:
        model = Contract
        fields = "__all__"


class LoanAccountSerializer(serializers.ModelSerializer):
    employee_profile_url = serializers.SerializerMethodField(read_only=True)
    employee_full_name = serializers.CharField(
        source="employee_id.get_full_name", read_only=True
    )
    badge_id = serializers.CharField(source="employee_id.badge_id", read_only=True)
    job_position_name = serializers.CharField(
        source="employee_id.get_job_position", read_only=True
    )

    class Meta:
        model = LoanAccount
        fields = "__all__"

    def get_employee_profile_url(self, obj):
        try:
            employee_profile = obj.employee_id.employee_profile
            return employee_profile.url
        except:
            return None


class ReimbursementSerializer(serializers.ModelSerializer):
    other_attachements = serializers.SerializerMethodField()
    leave_type_name = serializers.CharField(source="leave_type_id.name", read_only=True)
    employee_profile_url = serializers.SerializerMethodField(read_only=True)
    badge_id = serializers.CharField(source="employee_id.badge_id")
    employee_full_name = serializers.CharField(source="employee_id.get_full_name")

    def get_employee_profile_url(self, obj):
        try:
            employee_profile = obj.employee_id.employee_profile
            return employee_profile.url
        except:
            return None

    class Meta:
        model = Reimbursement
        fields = "__all__"

    def get_other_attachements(self, obj):
        attachments = []
        for attachment in obj.other_attachments.all():
            try:
                attachments.append(attachment.attachment.url)
            except:
                pass
        return attachments

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        exclude_fields = []
        # Get type from data or instance
        instance_type = getattr(self.instance, "type", None)

        if instance_type == "reimbursement":
            exclude_fields.extend(
                ["leave_type_id", "cfd_to_encash", "ad_to_encash", "bonus_to_encash"]
            )
        elif instance_type == "leave_encashment":
            exclude_fields.extend(["attachment", "amount", "bonus_to_encash"])
        elif instance_type == "bonus_encashment":
            exclude_fields.extend(
                [
                    "attachment",
                    "amount",
                    "leave_type_id",
                    "cfd_to_encash",
                    "ad_to_encash",
                ]
            )

        # Remove excluded fields from serializer fields
        for field in exclude_fields:
            self.fields.pop(field, None)

    def get_encashable_leaves(self, employee):
        leaves = LeaveType.objects.filter(
            employee_available_leave__employee_id=employee,
            employee_available_leave__total_leave_days__gte=1,
            is_encashable=True,
        )
        return leaves

    def validate(self, data):
        try:
            employee_id = self.instance.employee_id
            type = self.instance.type
            leave_type_id = self.instance.leave_type_id
        except:
            employee_id = data["employee_id"]
            type = data["type"]
            leave_type_id = (
                data["leave_type_id"] if data.get("leave_type_id", None) else None
            )

        available_points = BonusPoint.objects.filter(employee_id=employee_id).first()
        if type == "bonus_encashment":
            try:
                bonus_to_encash = self.instance.bonus_to_encash
            except:
                bonus_to_encash = data["bonus_to_encash"]

            if available_points.points < bonus_to_encash:
                raise serializers.ValidationError(
                    {"bonus_to_encash": _("Not enough bonus points to redeem")}
                )
            if bonus_to_encash <= 0:
                raise serializers.ValidationError(
                    {
                        "bonus_to_encash": _(
                            "Points must be greater than zero to redeem."
                        )
                    }
                )
        if type == "leave_encashment":
            leave_type_id = leave_type_id
            encashable_leaves = self.get_encashable_leaves(employee_id)
            if (leave_type_id is None) or (leave_type_id not in encashable_leaves):
                raise serializers.ValidationError(
                    {"leave_type_id": _("This leave type is not encashable")}
                )

        return data

    def save(self, **kwargs):
        multiple_attachment_ids = []
        request_files = self.context["request"].FILES
        attachments = request_files.getlist("attachment")
        if attachments:
            for attachment in attachments:
                file_instance = ReimbursementMultipleAttachment()
                file_instance.attachment = attachment
                file_instance.save()
                multiple_attachment_ids.append(file_instance.pk)

        instance = super().save()
        instance.other_attachments.add(*multiple_attachment_ids)

        return super().save(**kwargs)


class TaxBracketSerializer(serializers.ModelSerializer):
    class Meta:
        fields = "__all__"
        model = TaxBracket
