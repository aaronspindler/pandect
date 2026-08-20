from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("processing", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="processingstage",
            name="kind",
            field=models.CharField(
                choices=[
                    ("text_extraction", "Text extraction"),
                    ("writing_style", "Writing style"),
                    ("writing_identity", "Writing identity"),
                    ("image_metadata", "Image metadata"),
                    ("image_ocr", "Image OCR"),
                    ("image_qr", "Image QR extraction"),
                    ("image_visual", "Image visual analysis"),
                    ("face_analysis", "Face analysis"),
                    ("face_identity", "Face identity"),
                    ("video_metadata", "Video metadata"),
                    ("video_subtitles", "Video subtitles"),
                    ("video_keyframes", "Video keyframes"),
                    ("video_audio", "Video audio extraction"),
                    ("video_frame_analysis", "Video frame analysis"),
                    ("audio_metadata", "Audio metadata"),
                    ("audio_split", "Audio splitting"),
                    ("audio_acoustics", "Audio acoustic analysis"),
                    ("voice_analysis", "Voice analysis"),
                    ("voice_identity", "Voice identity"),
                    ("remote_semantic", "Remote semantic analysis"),
                    ("remote_image_description", "Remote image description"),
                    ("remote_transcription", "Remote transcription"),
                    ("remote_video_description", "Remote video description"),
                    ("remote_aggregate", "Remote result aggregation (local)"),
                ],
                max_length=48,
            ),
        ),
    ]
