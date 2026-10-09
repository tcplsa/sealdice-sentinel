import importlib.util
import struct
from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")
spec = importlib.util.spec_from_file_location("qr_capture", Path("integrations/snowluma-web/qr_capture.py"))
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def login_image(round_finders=False):
    image = Image.new("RGB", (320, 460), "#eeeeee")
    draw = ImageDraw.Draw(image)
    draw.rectangle((65, 150, 255, 340), fill="white")
    for x, y in [(75, 160), (224, 160), (75, 309)]:
        for size, inset, color in [(21, 0, "black"), (15, 3, "white"), (9, 6, "black")]:
            box = (x+inset, y+inset, x+inset+size-1, y+inset+size-1)
            (draw.ellipse if round_finders else draw.rectangle)(box, fill=color)
    draw.text((100, 30), "Desktop label outside QR", fill="black")
    return image


@pytest.mark.parametrize("rounded", [False, True])
def test_only_qr_region_is_returned_for_square_and_qq_circular_finders(rounded):
    image = login_image(rounded)
    x0, y0, x1, y1 = capture.qr_bounds(image)
    assert 40 <= x0 < 75 and 120 <= y0 < 160
    assert 245 <= x1 <= 285 and 330 <= y1 <= 370
    assert y0 > 30  # The label/window decoration is outside the returned image.


def test_missing_qr_never_falls_back_to_window_or_desktop():
    with pytest.raises(ValueError, match="finder"):
        capture.qr_bounds(Image.new("RGB", (320, 460), "white"))
    with pytest.raises(ValueError, match="header"):
        capture.decode_xwd(b"not a screenshot")
    header = [0]*25
    header[0:6] = [100, 7, 2, 24, 1024, 768]
    with pytest.raises(ValueError, match="Unsupported"):
        capture.decode_xwd(struct.pack(">25I", *header))


def test_expired_qr_is_located_without_reusing_a_previous_process_location():
    image = login_image(round_finders=True)
    image = Image.blend(image, Image.new('RGB', image.size, 'white'), 0.975)
    draw = ImageDraw.Draw(image)
    draw.text((115, 232), 'QR expired', fill='#ff4040')
    draw.rounded_rectangle((121, 264, 199, 296), radius=10, outline='#cccccc')
    with pytest.raises(ValueError, match='finder'):
        capture.qr_bounds(image)
    box = capture.expired_qr_bounds(image)
    assert box[0] < 75 and box[1] < 160 and box[2] > 245 and box[3] > 330


def test_faded_qr_without_verified_expiry_label_and_button_is_not_clicked():
    image = Image.blend(login_image(True), Image.new('RGB', (320, 460), 'white'), 0.975)
    with pytest.raises(ValueError, match='not verified'):
        capture.expired_qr_bounds(image)
