"""Explicit intake and metadata forms for current catalogue item types."""

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.catalog.services import (
    create_catalogued_item,
    create_catalogued_message,
    create_uploaded_media,
    inspect_uploaded_media,
)
from pandect.catalog.uploads import validate_upload_sizes
from pandect.events.models import Event
from pandect.messages.models import (
    CommunicationParticipant,
    ImageMessage,
    Message,
    MessageParticipant,
    TextMessage,
    VideoMessage,
)
from pandect.notes.models import Note
from pandect.photos.models import Image
from pandect.videos.models import Video

DATETIME_WIDGET = forms.DateTimeInput(attrs={"type": "datetime-local"})


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        if not data:
            return super().clean(data, initial)
        files = data if isinstance(data, (list, tuple)) else [data]
        return [super().clean(upload, initial) for upload in files]


class BulkMediaForm(forms.Form):
    files = MultipleFileField(
        label="Image, video, and audio files",
        widget=MultipleFileInput(attrs={"accept": "image/*,video/*,audio/*"}),
        help_text=(
            "Select 1–50 files from your computer. You can mix images, videos, "
            "and audio recordings, at most 2 GiB per file and 8 GiB per batch. "
            "Each original is checked locally and saved unchanged."
        ),
    )

    def clean_files(self):
        files = self.cleaned_data["files"]
        try:
            validate_upload_sizes(files)
        except ValueError as error:
            raise ValidationError(str(error)) from error
        errors = []
        inspections = []
        for upload in files:
            try:
                inspections.append(inspect_uploaded_media(upload))
            except ValueError as error:
                errors.append(ValidationError(f"{upload.name}: {error}"))
        if errors:
            raise ValidationError(errors)
        self.inspections = inspections
        return files


class MediaIntakeForm(forms.Form):
    expected_type = None
    file = forms.FileField()

    def clean_file(self):
        upload = self.cleaned_data["file"]
        try:
            validate_upload_sizes([upload])
            inspection = inspect_uploaded_media(upload)
        except ValueError as error:
            raise ValidationError(str(error)) from error
        if inspection.item_type != self.expected_type:
            expected_label = CatalogItem.ItemType(self.expected_type).label.lower()
            raise ValidationError(f"Select a {expected_label} file for this item type.")
        self.inspection = inspection
        return upload

    def create(self):
        extension = create_uploaded_media(
            self.cleaned_data["file"],
            expected_type=self.expected_type,
            inspection=self.inspection,
        )
        return extension.catalog_item


class ImageIntakeForm(MediaIntakeForm):
    expected_type = CatalogItem.ItemType.IMAGE
    file = forms.FileField(
        label="Image file",
        widget=forms.FileInput(attrs={"accept": "image/*"}),
        help_text="Choose an original image from your computer, such as a JPG or PNG.",
    )


class VideoIntakeForm(MediaIntakeForm):
    expected_type = CatalogItem.ItemType.VIDEO
    file = forms.FileField(
        label="Video file",
        widget=forms.FileInput(attrs={"accept": "video/*"}),
        help_text="Choose an original video from your computer, such as an MP4.",
    )


class AudioIntakeForm(MediaIntakeForm):
    expected_type = CatalogItem.ItemType.AUDIO
    file = forms.FileField(
        label="Audio file",
        widget=forms.FileInput(attrs={"accept": "audio/*"}),
        help_text="Choose an audio recording from your computer, such as WAV or MP3.",
    )


class EventIntakeForm(forms.Form):
    description = forms.CharField(
        label="What happened?",
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text=(
            "Describe what you observed, for example ‘A vehicle arrived at the "
            "north entrance.’ You can add the date and time in the next step."
        ),
    )

    def create(self):
        return create_catalogued_item(
            Event,
            extension_fields={"description": self.cleaned_data["description"]},
        ).catalog_item


class NoteIntakeForm(forms.Form):
    body = forms.CharField(
        label="Note text",
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text=(
            "Write your observation or working note. The first non-empty line "
            "becomes its title; you can change the title in the next step."
        ),
    )

    def create(self):
        body = self.cleaned_data["body"]
        title = next((line.strip() for line in body.splitlines() if line.strip()), "")
        return create_catalogued_item(
            Note,
            item_fields={"title": title[:255]},
            extension_fields={"body": body},
        ).catalog_item


class TextMessageIntakeForm(forms.Form):
    body = forms.CharField(
        label="Message text",
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text=(
            "Enter the message as received. You can add its sender, recipients, "
            "and date and time in the next step."
        ),
    )

    def create(self):
        return create_catalogued_message(
            TextMessage,
            message_fields={"body": self.cleaned_data["body"]},
        ).message.catalog_item


class MediaMessageIntakeForm(forms.Form):
    media_model = None
    message_model = None
    media_field = ""
    body = forms.CharField(
        label="Caption or message text",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Enter any text that accompanied the image or video, if known.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[
            self.media_field
        ].queryset = self.media_model.objects.select_related("catalog_item")

    def create(self):
        subtype = create_catalogued_message(
            self.message_model,
            message_fields={"body": self.cleaned_data["body"]},
            subtype_fields={self.media_field: self.cleaned_data[self.media_field]},
        )
        return subtype.message.catalog_item


class ImageMessageIntakeForm(MediaMessageIntakeForm):
    media_model = Image
    message_model = ImageMessage
    media_field = "image"
    image = forms.ModelChoiceField(
        label="Image already in the catalogue",
        queryset=Image.objects.none(),
        empty_label="Choose a saved image",
        help_text="Choose the image that was sent with this message.",
    )


class VideoMessageIntakeForm(MediaMessageIntakeForm):
    media_model = Video
    message_model = VideoMessage
    media_field = "video"
    video = forms.ModelChoiceField(
        label="Video already in the catalogue",
        queryset=Video.objects.none(),
        empty_label="Choose a saved video",
        help_text="Choose the video that was sent with this message.",
    )


INTAKE_FORMS = {
    CatalogItem.ItemType.IMAGE: ImageIntakeForm,
    CatalogItem.ItemType.VIDEO: VideoIntakeForm,
    CatalogItem.ItemType.AUDIO: AudioIntakeForm,
    CatalogItem.ItemType.EVENT: EventIntakeForm,
    CatalogItem.ItemType.NOTE: NoteIntakeForm,
    CatalogItem.ItemType.TEXT_MESSAGE: TextMessageIntakeForm,
    CatalogItem.ItemType.IMAGE_MESSAGE: ImageMessageIntakeForm,
    CatalogItem.ItemType.VIDEO_MESSAGE: VideoMessageIntakeForm,
}


class ItemMetadataForm(forms.Form):
    title = forms.CharField(
        max_length=255,
        required=False,
        help_text="A short name to help you recognize this item in the catalogue.",
    )
    occurred_at = forms.DateTimeField(
        label="Date and time",
        required=False,
        widget=DATETIME_WIDGET,
    )

    def __init__(self, *args, item, **kwargs):
        self.item = item
        initial = {"title": item.title, "occurred_at": item.occurred_at}
        initial.update(self.extension_initial())
        kwargs["initial"] = initial | kwargs.get("initial", {})
        super().__init__(*args, **kwargs)
        self.fields["occurred_at"].help_text = (
            "When this happened or the material was created, rather than when "
            "you added it. Enter the time in "
            f"{timezone.get_current_timezone_name()}. Leave blank if unknown."
        )

    def extension_initial(self):
        return {}

    @transaction.atomic
    def save(self):
        self.item.title = self.cleaned_data["title"]
        self.item.occurred_at = self.cleaned_data["occurred_at"]
        self.item.save()
        self.save_extension()
        return self.item

    def save_extension(self):
        return None


class ImageMetadataForm(ItemMetadataForm):
    original_filename = forms.CharField(
        max_length=255,
        required=False,
        help_text="The source filename and extension, for example photo.jpg.",
    )
    mime_type = forms.CharField(
        label="File format (MIME type)",
        max_length=255,
        required=False,
        help_text="The technical file format, for example image/jpeg or video/mp4.",
    )
    width = forms.IntegerField(
        label="Width in pixels",
        min_value=1,
        required=False,
        help_text="The image or video frame width, for example 1920.",
    )
    height = forms.IntegerField(
        label="Height in pixels",
        min_value=1,
        required=False,
        help_text="The image or video frame height, for example 1080.",
    )

    def extension_initial(self):
        image = self.item.image
        return {
            "original_filename": image.original_filename,
            "mime_type": image.mime_type,
            "width": image.width,
            "height": image.height,
        }

    def save_extension(self):
        image = self.item.image
        for field in ("original_filename", "mime_type", "width", "height"):
            setattr(image, field, self.cleaned_data[field])
        image.save()


class VideoMetadataForm(ImageMetadataForm):
    duration = forms.DurationField(
        required=False,
        help_text="Use HH:MM:SS, for example 00:02:15.",
    )

    def extension_initial(self):
        video = self.item.video
        return {
            "original_filename": video.original_filename,
            "mime_type": video.mime_type,
            "width": video.width,
            "height": video.height,
            "duration": video.duration,
        }

    def save_extension(self):
        video = self.item.video
        for field in (
            "original_filename",
            "mime_type",
            "width",
            "height",
            "duration",
        ):
            setattr(video, field, self.cleaned_data[field])
        video.save()


class AudioMetadataForm(ItemMetadataForm):
    original_filename = forms.CharField(
        max_length=255,
        required=False,
        help_text=(
            "The source filename, including its extension, for example recording.wav."
        ),
    )
    mime_type = forms.CharField(
        label="File format (MIME type)",
        max_length=255,
        required=False,
        help_text="The technical file format, for example audio/wav or audio/mpeg.",
    )
    duration = forms.DurationField(
        required=False,
        help_text="Use HH:MM:SS, for example 00:02:15.",
    )

    def extension_initial(self):
        audio = self.item.audio
        return {
            "original_filename": audio.original_filename,
            "mime_type": audio.mime_type,
            "duration": audio.duration,
        }

    def save_extension(self):
        audio = self.item.audio
        for field in ("original_filename", "mime_type", "duration"):
            setattr(audio, field, self.cleaned_data[field])
        audio.save()


class EventMetadataForm(ItemMetadataForm):
    ended_at = forms.DateTimeField(
        label="End date and time", required=False, widget=DATETIME_WIDGET
    )
    description = forms.CharField(
        label="What happened?",
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="Describe the event as observed, without guessing missing details.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ended_at"].help_text = (
            f"When the event ended, in {timezone.get_current_timezone_name()}. "
            "Leave blank if the end time is unknown."
        )

    def extension_initial(self):
        return {
            "ended_at": self.item.event.ended_at,
            "description": self.item.event.description,
        }

    def clean(self):
        cleaned_data = super().clean()
        occurred_at = cleaned_data.get("occurred_at")
        ended_at = cleaned_data.get("ended_at")
        if occurred_at and ended_at and ended_at < occurred_at:
            self.add_error("ended_at", "An event cannot end before it occurs.")
        return cleaned_data

    def save_extension(self):
        event = self.item.event
        event.ended_at = self.cleaned_data["ended_at"]
        event.description = self.cleaned_data["description"]
        event.save()

    @transaction.atomic
    def save(self):
        """Order related saves so validation always sees a valid event interval."""
        current_end = self.item.event.ended_at
        new_occurrence = self.cleaned_data["occurred_at"]
        save_event_first = (
            current_end is not None
            and new_occurrence is not None
            and new_occurrence > current_end
        )
        self.item.title = self.cleaned_data["title"]
        self.item.occurred_at = new_occurrence
        if save_event_first:
            self.save_extension()
            self.item.save()
        else:
            self.item.save()
            self.save_extension()
        return self.item


class NoteMetadataForm(ItemMetadataForm):
    body = forms.CharField(
        label="Note text",
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text="Review the observation or working note you saved in the last step.",
    )

    def extension_initial(self):
        return {"body": self.item.note.body}

    def save_extension(self):
        note = self.item.note
        note.body = self.cleaned_data["body"]
        note.save()


class MessageMetadataForm(ItemMetadataForm):
    body = forms.CharField(
        label="Message text or caption",
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="The message as received, or any caption accompanying its media.",
    )
    sender_name = forms.CharField(
        max_length=255,
        required=False,
        help_text="The sender’s display name, if known, for example Alex Kim.",
    )
    sender_identifier = forms.CharField(
        max_length=512,
        required=False,
        help_text="For example an email address, phone number, or account ID.",
    )
    sender_type = forms.ChoiceField(
        label="Sender kind",
        choices=CommunicationParticipant.ParticipantType.choices,
        required=False,
        help_text="Choose the kind of sender, or Unknown if you do not know.",
    )
    recipients = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text=(
            "Enter one recipient per line, for example Alex Kim | "
            "alex@example.test | to. Use | to separate name, identifier, and role. "
            "A name alone is also accepted. Roles: to, cc, bcc, mentioned, unknown."
        ),
    )
    reply_to = forms.ModelChoiceField(
        label="Replies to",
        queryset=Message.objects.none(),
        required=False,
        empty_label="No known earlier message",
        help_text="Choose the earlier catalogue message that this message answers.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.item.item_type == CatalogItem.ItemType.TEXT_MESSAGE:
            self.fields["body"].required = True
            self.fields["body"].label = "Message text"
        self.fields["reply_to"].queryset = Message.objects.exclude(
            pk=self.item.pk
        ).select_related("catalog_item")

    def extension_initial(self):
        message = self.item.message
        sender = message.sender
        recipients = "\n".join(
            " | ".join(
                (
                    link.participant.display_name,
                    link.participant.identifier,
                    link.role,
                )
            )
            for link in message.recipients.select_related("participant")
        )
        return {
            "body": message.body,
            "sender_name": sender.display_name if sender else "",
            "sender_identifier": sender.identifier if sender else "",
            "sender_type": (
                sender.participant_type
                if sender
                else CommunicationParticipant.ParticipantType.UNKNOWN
            ),
            "recipients": recipients,
            "reply_to": message.reply_to,
        }

    def clean_body(self):
        body = self.cleaned_data["body"]
        if (
            self.item.item_type == CatalogItem.ItemType.TEXT_MESSAGE
            and not body.strip()
        ):
            raise ValidationError("A text message requires a non-empty body.")
        return body

    def clean_recipients(self):
        value = self.cleaned_data["recipients"]
        parsed = []
        valid_roles = {choice.value for choice in MessageParticipant.Role}
        for line_number, line in enumerate(value.splitlines(), start=1):
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) > 3:
                raise ValidationError(
                    f"Recipient line {line_number} has too many fields."
                )
            parts += [""] * (3 - len(parts))
            display_name, identifier, role = parts
            role = role or MessageParticipant.Role.TO
            if not display_name and not identifier:
                raise ValidationError(
                    f"Recipient line {line_number} needs a name or identifier."
                )
            if role not in valid_roles:
                raise ValidationError(
                    f"Recipient line {line_number} has an unknown role: {role}."
                )
            parsed.append((display_name, identifier, role))
        return parsed

    def save_extension(self):
        message = self.item.message
        body_changed = message.body != self.cleaned_data["body"]
        sender_name = self.cleaned_data["sender_name"]
        sender_identifier = self.cleaned_data["sender_identifier"]
        if sender_name or sender_identifier:
            current = message.sender
            sender_values = (
                sender_name,
                sender_identifier,
                self.cleaned_data["sender_type"]
                or CommunicationParticipant.ParticipantType.UNKNOWN,
            )
            current_values = (
                (current.display_name, current.identifier, current.participant_type)
                if current
                else None
            )
            if sender_values != current_values:
                message.sender = CommunicationParticipant.objects.create(
                    display_name=sender_values[0],
                    identifier=sender_values[1],
                    participant_type=sender_values[2],
                )
        else:
            message.sender = None
        message.body = self.cleaned_data["body"]
        message.reply_to = self.cleaned_data["reply_to"]
        message.save()

        if body_changed:
            from pandect.processing.services import (
                create_local_run,
                supersede_active_processing_runs,
            )

            if message.body.strip():
                create_local_run(self.item)
            else:
                supersede_active_processing_runs(self.item)

        message.recipients.all().delete()
        for display_name, identifier, role in self.cleaned_data["recipients"]:
            participant = CommunicationParticipant.objects.create(
                display_name=display_name,
                identifier=identifier,
            )
            MessageParticipant.objects.create(
                message=message,
                participant=participant,
                role=role,
            )


METADATA_FORMS = {
    CatalogItem.ItemType.IMAGE: ImageMetadataForm,
    CatalogItem.ItemType.VIDEO: VideoMetadataForm,
    CatalogItem.ItemType.AUDIO: AudioMetadataForm,
    CatalogItem.ItemType.EVENT: EventMetadataForm,
    CatalogItem.ItemType.NOTE: NoteMetadataForm,
    CatalogItem.ItemType.TEXT_MESSAGE: MessageMetadataForm,
    CatalogItem.ItemType.IMAGE_MESSAGE: MessageMetadataForm,
    CatalogItem.ItemType.VIDEO_MESSAGE: MessageMetadataForm,
}
