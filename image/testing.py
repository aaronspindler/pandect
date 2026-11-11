"""Synthetic JPEG exports for image regression tests and browser journeys."""

import io

from PIL import Image, ImageDraw, ImageFont


def plate_scene(*, angle=8, distractors=False):
    """Fictional plate in a larger scene, with a separate header and frame."""
    background = "#303844"
    with Image.new("RGB", (2400, 1600), background) as scene:
        if distractors:
            draw = ImageDraw.Draw(scene)
            for box in (
                (100, 100, 700, 300),
                (1000, 100, 1600, 300),
                (1700, 500, 2300, 700),
                (100, 1100, 700, 1300),
            ):
                draw.rectangle(box, fill="white", outline="black", width=6)
        with Image.new("RGB", (440, 180), "white") as panel:
            draw = ImageDraw.Draw(panel)
            draw.rectangle((0, 0, 439, 179), outline="black", width=6)
            draw.text(
                (150, 12),
                "SYNTHETIC",
                fill="navy",
                font=ImageFont.load_default(size=18),
            )
            draw.text(
                (35, 57), "TEST 482", fill="navy", font=ImageFont.load_default(size=64)
            )
            draw.text(
                (125, 151),
                "FICTIONAL PLATE",
                fill="black",
                font=ImageFont.load_default(size=16),
            )
            panel = panel.rotate(
                angle,
                expand=True,
                resample=Image.Resampling.BICUBIC,
                fillcolor=background,
            )
            scene.paste(panel, (1000, 750))
        stream = io.BytesIO()
        scene.save(stream, format="JPEG", quality=95)
        return stream.getvalue()


def jpeg_bytes(*, size=(1000, 240), multiple_pictures=False):
    with Image.new("RGB", size, "white") as image:
        ImageDraw.Draw(image).text(
            (size[0] // 25, size[1] // 4),
            "PANDECT LOCAL TEXT 12345",
            font=ImageFont.load_default(size=size[0] // 21),
            fill="black",
        )
        stream = io.BytesIO()
        if multiple_pictures:
            with Image.new("RGB", (32, 24), "gray") as auxiliary:
                image.save(stream, format="MPO", append_images=[auxiliary])
        else:
            image.save(stream, format="JPEG", progressive=True)
        return stream.getvalue()
