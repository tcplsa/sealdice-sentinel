"""Capture only a validated QQ login QR, never return the desktop or message UI."""
import argparse
import ctypes
import ctypes.util
import io
import itertools
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image


def decode_xwd(raw):
    if len(raw) < 100:
        raise ValueError('Short XWD header')
    header = struct.unpack('>25I', raw[:100])
    size, version, fmt, depth, width, height = header[:6]
    byte_order, bpp, stride = header[7], header[11], header[12]
    masks, colors = header[14:17], header[19]
    if (version != 7 or fmt != 2 or depth != 24 or bpp != 32 or byte_order != 0
            or masks != (0xff0000, 0xff00, 0xff) or not 250 <= width <= 500
            or not 300 <= height <= 650 or stride < width * 4 or size < 100):
        raise ValueError('Unsupported QQ login screenshot')
    offset = size + colors * 12
    pixels = raw[offset:offset + stride * height]
    if len(pixels) != stride * height:
        raise ValueError('Incomplete screenshot')
    return Image.frombytes('RGB', (width, height), pixels, 'raw', 'BGRX', stride)


def finder_ratio(runs):
    unit = sum(runs) / 7
    return unit >= 1.2 and all(abs(x - unit * scale) <= unit * 0.8
                             for x, scale in zip(runs, [1, 1, 3, 1, 1], strict=True))


def vertical_center(pixels, width, height, x, y, threshold, unit):
    def dark(at):
        return 0 <= at < height and pixels[at * width + x] < threshold

    up = down = 0
    while dark(y-up) and up <= 5*unit:
        up += 1
    while dark(y+down) and down <= 5*unit:
        down += 1
    top, bottom = y-up, y+down
    white_up = white_down = black_up = black_down = 0
    while top >= 0 and not dark(top) and white_up <= 3*unit:
        white_up += 1
        top -= 1
    while bottom < height and not dark(bottom) and white_down <= 3*unit:
        white_down += 1
        bottom += 1
    while dark(top) and black_up <= 3*unit:
        black_up += 1
        top -= 1
    while dark(bottom) and black_down <= 3*unit:
        black_down += 1
        bottom += 1
    runs = [black_up, white_up, up+down-1, white_down, black_down]
    return (y + (down-up)/2, sum(runs)/7) if finder_ratio(runs) else None


def qr_bounds(image, thresholds=(100, 150, 200, 235)):
    width, height = image.size
    pixels = image.convert('L').tobytes()
    for threshold in thresholds:
        candidates = []
        for y in range(0, height, 2):
            start = 0
            last = pixels[y*width] < threshold
            runs = []
            for x in range(1, width+1):
                dark = x < width and pixels[y*width+x] < threshold
                if x < width and dark == last:
                    continue
                runs.append((last, start, x-start))
                runs = runs[-5:]
                if (len(runs) == 5 and [r[0] for r in runs] == [True, False, True, False, True]
                        and finder_ratio([r[2] for r in runs])):
                    lengths = [r[2] for r in runs]
                    cx = int(runs[0][1] + lengths[0] + lengths[1] + lengths[2]/2)
                    vertical = vertical_center(pixels, width, height, cx, y, threshold,
                                               sum(lengths)/7)
                    if vertical:
                        cy, unit = vertical
                        if not any(abs(cx-a)<unit*2 and abs(cy-b)<unit*2
                                   for a, b, _ in candidates):
                            candidates.append((cx, cy, unit))
                start, last = x, dark
        for points in itertools.combinations(candidates[:24], 3):
            for origin in points:
                other = [p for p in points if p is not origin]
                horizontal = min(other, key=lambda p: abs(p[1]-origin[1]))
                vertical = next(p for p in other if p is not horizontal)
                dx, dy = horizontal[0]-origin[0], vertical[1]-origin[1]
                unit = sum(p[2] for p in points)/3
                if (dx < 60 or dy < 60 or abs(dx-dy)>max(dx, dy)*0.12
                        or abs(horizontal[1]-origin[1])>unit*2
                        or abs(vertical[0]-origin[0])>unit*2):
                    continue
                margin = unit * 7.5
                box = (int(origin[0]-margin), int(origin[1]-margin),
                       int(horizontal[0]+margin), int(vertical[1]+margin))
                if (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
                        and 90 <= box[2]-box[0] <= 330 and 90 <= box[3]-box[1] <= 330):
                    return box
    raise ValueError('QR finder markers not visible')


def expired_qr_bounds(image):
    # QQ fades all three finder markers on expiry. A fresh process has no saved
    # location, so verify the faded card, red expiry label and refresh outline
    # directly in this window before clicking anything.
    box = qr_bounds(image, thresholds=(245, 248, 250, 252, 254))
    left, top, right, bottom = box
    width, height = right-left, bottom-top
    cx, cy = (left+right)//2, (top+bottom)//2
    label = image.crop((left+width//5, cy-int(height*0.12),
                        right-width//5, cy+int(height*0.04)))
    red = sum(r > 190 and g < 150 and b < 150 and r-g > 70
              for r, g, b in label.convert('RGB').getdata())
    button = image.crop((cx-int(width*0.2), cy+int(height*0.08),
                         cx+int(width*0.2), cy+int(height*0.25)))
    outline = sum(160 <= r <= 230 and abs(r-g) < 5 and abs(r-b) < 5
                  for r, g, b in button.convert('RGB').getdata())
    if red < 20 or outline < 30:
        raise ValueError('Expired QQ QR and refresh button not verified')
    return box


def login_window():
    tree = subprocess.check_output(['xwininfo', '-display', ':33', '-root', '-tree'],
                                   text=True, timeout=4)
    matches = []
    for line in tree.splitlines():
        match = re.search(r'(0x[0-9a-f]+) "QQ":.*? (\d+)x(\d+)\+.*?  ([+-]\d+)([+-]\d+)$', line)
        if match and 250 <= int(match[2]) <= 500 and 300 <= int(match[3]) <= 650:
            matches.append(match)
    if len(matches) != 1:
        raise ValueError('No unique QQ login window')
    match = matches[0]
    value = subprocess.check_output(['xprop', '-display', ':33', '-id', match[1], '_NET_WM_PID'],
                                    text=True, timeout=4)
    pid = re.search(r'=\s*(\d+)', value)
    if not pid:
        raise ValueError('QQ window PID unavailable')
    if 'snowluma-dice3.service' not in Path('/proc', pid[1], 'cgroup').read_text():
        raise ValueError('Window outside dedicated SL client')
    return match[1], int(match[4]), int(match[5]), int(pid[1])


def capture(window):
    raw = subprocess.check_output(['xwd', '-display', ':33', '-id', window, '-screen', '-silent'],
                                  timeout=4)
    return decode_xwd(raw)


def show_window(window):
    x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XMapRaised.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(b':33')
    if not display:
        raise ValueError('Dedicated display unavailable')
    try:
        x11.XMapRaised(display, int(window, 16))
        x11.XFlush(display)
    finally:
        x11.XCloseDisplay(display)


def refresh_at(x, y):
    x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
    xtst = ctypes.CDLL(ctypes.util.find_library('Xtst'))
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                        ctypes.c_ulong]
    display = x11.XOpenDisplay(b':33')
    if not display:
        raise ValueError('Dedicated display unavailable')
    try:
        xtst.XTestFakeMotionEvent(display, -1, x, y, 0)
        xtst.XTestFakeButtonEvent(display, 1, 1, 0)
        xtst.XTestFakeButtonEvent(display, 1, 0, 0)
        x11.XFlush(display)
    finally:
        x11.XCloseDisplay(display)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--show', action='store_true')
    parser.add_argument('--location-file', type=Path)
    args = parser.parse_args()
    window, x, y, pid = login_window()
    if args.show:
        show_window(window)
        time.sleep(0.3)
    image = capture(window)
    expired = False
    try:
        box = qr_bounds(image)
    except ValueError:
        if not args.refresh:
            raise
        box = expired_qr_bounds(image)
        expired = True
    if args.refresh and expired:
        # QQ's expired-code refresh button is below the centre, still entirely
        # inside the expired QR card verified in this exact client/window.
        refresh_at(x + (box[0]+box[2])//2,
                   y + (box[1]+box[3])//2 + int((box[3]-box[1])*0.17))
        for _ in range(12):
            time.sleep(0.25)
            image = capture(window)
            try:
                box = qr_bounds(image)
                break
            except ValueError:
                continue
        else:
            raise ValueError('QQ did not refresh the expired code')
    if args.location_file and os.geteuid() == 0:
        import pwd
        args.location_file.write_text(json.dumps({'window': window, 'pid': pid,
                                                 'size': image.size, 'box': box}))
        args.location_file.chmod(0o640)
        os.chown(args.location_file, 0, pwd.getpwnam('snowluma-web').pw_gid)
    output = io.BytesIO()
    image.crop(box).save(output, format='PNG')
    data = output.getvalue()
    if len(data) > 128*1024:
        raise ValueError('Unexpected QR image size')
    sys.stdout.buffer.write(data)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # noqa: BLE001 - do not leak screenshot details or native errors
        print(type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
