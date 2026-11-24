from uuid import uuid4

from django import forms


class QuestionForm(forms.Form):
    submission_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)
    route = forms.CharField(widget=forms.HiddenInput)
    question = forms.CharField(
        max_length=2000,
        label="Question",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    include_archived = forms.BooleanField(
        label="Include archived evidence", required=False
    )
