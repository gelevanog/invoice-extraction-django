from __future__ import annotations

from pathlib import PurePath
from typing import Any

from django import forms
from django.core.files.uploadedfile import UploadedFile

from extraction.parse import SUPPORTED_EXTENSIONS


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data: Any, initial: Any = None) -> list[UploadedFile]:
        files = data if isinstance(data, list | tuple) else [data]
        cleaned = [super(MultipleFileField, self).clean(item, initial) for item in files]
        for upload in cleaned:
            suffix = PurePath(upload.name or "").suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
                raise forms.ValidationError(f"{upload.name}: unsupported type. Allowed: {allowed}")
        return cleaned


class UploadForm(forms.Form):
    files = MultipleFileField(
        widget=MultipleFileInput(attrs={"accept": ",".join(sorted(SUPPORTED_EXTENSIONS))})
    )


class ReviewDecisionForm(forms.Form):
    action = forms.ChoiceField(choices=[("approve", "Approve"), ("reject", "Reject")])
    note = forms.CharField(required=False, max_length=2000)
