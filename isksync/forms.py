from django import forms
from django.contrib.auth import get_user_model

from .models import SystemOwnership

User = get_user_model()


class UserModelChoiceField(forms.ModelChoiceField):
    """Custom ModelChoiceField that displays main character names in dropdowns"""

    def label_from_instance(self, obj):
        try:
            if hasattr(obj, 'profile') and obj.profile.main_character:
                main_char = obj.profile.main_character
                return f"{main_char.character_name} ({obj.username})"
            else:
                return f"{obj.username} (no main character)"
        except Exception:
            return obj.username


class SystemOwnershipForm(forms.ModelForm):
    """Form for editing SystemOwnership from the front-end."""

    primary_user = UserModelChoiceField(
        queryset=User.objects.all(),
        required=False,
        help_text="The primary user responsible for this system. Must be a member of the auth group.",
    )

    class Meta:
        model = SystemOwnership
        fields = [
            "ownership_type",
            "auth_group",
            "primary_user",
            "tax_active",
            "default_tax_amount_isk",
            "discord_channel",
            "notes",
        ]
        widgets = {
            "ownership_type": forms.Select(attrs={"class": "form-select"}),
            "auth_group": forms.Select(attrs={"class": "form-select"}),
            "tax_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "default_tax_amount_isk": forms.NumberInput(attrs={"class": "form-control"}),
            "discord_channel": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Bootstrap class for the custom field
        self.fields['primary_user'].widget.attrs.update({"class": "form-select"})

        # Order auth_group alphabetically by group name
        if 'auth_group' in self.fields:
            from allianceauth.groupmanagement.models import AuthGroup
            self.fields['auth_group'].queryset = (
                AuthGroup.objects.select_related('group').order_by('group__name')
            )

        # Default ordering for primary_user
        self.fields['primary_user'].queryset = User.objects.select_related(
            'profile', 'profile__main_character'
        ).order_by('profile__main_character__character_name', 'username')

        # If instance has auth_group, filter primary_user to group members
        if self.instance and self.instance.pk and self.instance.auth_group_id:
            try:
                group_users = self.instance.auth_group.group.user_set.all()
                if group_users.exists():
                    self.fields['primary_user'].queryset = group_users.select_related(
                        'profile', 'profile__main_character'
                    ).order_by('profile__main_character__character_name', 'username')
            except Exception:
                pass
