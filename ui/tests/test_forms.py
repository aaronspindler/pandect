from django import forms
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class SharedFieldTests(SimpleTestCase):
    def test_choice_groups_have_a_name_help_and_error_target(self):
        class ChoiceForm(forms.Form):
            sources = forms.MultipleChoiceField(
                label="Sources",
                choices=(("first", "First source"), ("second", "Second source")),
                widget=forms.CheckboxSelectMultiple,
                help_text="Choose the sources to include.",
            )

        form = ChoiceForm({})
        self.assertFalse(form.is_valid())
        html = render_to_string("ui/components/form_fields.html", {"form": form})
        self.assertInHTML("<legend>Sources</legend>", html)
        self.assertIn('id="id_sources_group" tabindex="-1"', html)
        self.assertIn('aria-describedby="id_sources_helptext id_sources_error"', html)
        self.assertIn('href="#id_sources_group"', html)
        self.assertIn('id="id_sources_helptext"', html)
        self.assertIn('id="id_sources_error"', html)
        self.assertIn('for="id_sources_0"', html)
        self.assertIn('for="id_sources_1"', html)
        self.assertEqual(html.count('id="id_sources"'), 1)

    def test_single_checkbox_keeps_its_clickable_label(self):
        class ApprovalForm(forms.Form):
            approve = forms.BooleanField(label="Approve processing")

        html = render_to_string(
            "ui/components/form_fields.html", {"form": ApprovalForm()}
        )
        self.assertInHTML('<label for="id_approve">Approve processing</label>', html)
        self.assertNotIn("<fieldset", html)
