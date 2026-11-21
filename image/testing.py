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


def printed_layout(kind):
    """Draw each word at a known location and return its normalized ink box."""
    width, height = 1050, 600
    photo = kind == "page"
    image = Image.new("RGB", (width, height), "#aaa9a5" if photo else "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=34)
    boxes = {}

    def write(x, y, phrase):
        for word in phrase.split():
            draw.text((x, y), word, font=font, fill="#121212")
            left, top, right, bottom = draw.textbbox((x, y), word, font=font)
            if photo:
                top += left / 80
                bottom += right / 80
            boxes[word] = (left / width, top / height, right / width, bottom / height)
            x += draw.textlength(word + " ", font=font)

    if kind == "chat":
        for x, y, person, stamp, body in (
            (65, 70, "Morgan", "09:15", "Bring the binder"),
            (265, 255, "Riley", "09:16", "Meet at noon"),
        ):
            draw.rounded_rectangle(
                (x - 12, y - 10, x + 420, y + 104), 12, fill="#f1f4f5"
            )
            write(x, y, f"{person}  {stamp}")
            write(x, y + 51, body)
    elif kind == "receipt":
        write(70, 40, "Stationery")
        for number, (name, price) in enumerate(
            (("Pencils", "$3.50"), ("Folders", "$8.25"), ("Paper", "$2.40"))
        ):
            write(70, 140 + 65 * number, name)
            write(565, 140 + 65 * number, price)
    elif kind == "form":
        write(65, 60, "Registration")
        for y, label, value in (
            (155, "Name:", "River Hall"),
            (232, "City:", "Toronto"),
        ):
            write(65, y, label)
            write(315, y, value)
    elif kind == "columns":
        for y, left, right in ((160, "Morning", "East"), (220, "Evening", "West")):
            write(60, y, left)
            write(560, y, right)
    elif photo:
        draw.rectangle((42, 26, 1008, 554), fill="#fffefa")
        write(90, 80, "Station report")
        write(90, 175, "Morning trains arrived early.")
        write(90, 225, "Passengers used another platform.")
        image = image.transform(
            image.size,
            Image.Transform.AFFINE,
            (1, 0, 0, -1 / 80, 1, 0),
            resample=Image.Resampling.BICUBIC,
            fillcolor="#aaa9a5",
        )
    stream = io.BytesIO()
    image.save(stream, format="JPEG" if photo else "PNG")
    return stream.getvalue(), boxes
