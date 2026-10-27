#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Use a Turing USB screen (REVISION: TUR_USB) as a console display.
#
# Mode 1: display the standard input (e.g. command output)
#   tail -f /var/log/syslog | python console-to-lcd.py
#   some_command 2>&1 | python console-to-lcd.py --landscape
#
# Mode 2: run a shell (with its prompt) and mirror it on the screen. Type in the terminal running this script
#   python console-to-lcd.py --shell --landscape
#   (requires pyte: pip install pyte). Type `exit` to quit.
#   --side 0|1 selects the left (0) or right (1) half; --dual runs one shell per half (Ctrl+] switches the input)
#
# Stop main.py first: only one process can use the USB device at a time.

from library.pythoncheck import check_python_version

check_python_version()

import argparse
import re
import select
import signal
import sys
import threading
import time
from collections import deque

from PIL import Image, ImageDraw, ImageFont

from library.lcd.lcd_comm import Orientation
from library.lcd.lcd_comm_turing_usb import LcdCommTuringUSB
from library.log import logger

# Fonts: Source Han Sans has Japanese glyphs (stdin mode and non-ASCII characters in shell mode)
FONT_PATH = "res/fonts/SourceHanSansCN/SourceHanSansCN-Regular.otf"
MONO_FONT_PATH = "res/fonts/roboto-mono/RobotoMono-Regular.ttf"
MONO_BOLD_FONT_PATH = "res/fonts/roboto-mono/RobotoMono-Bold.ttf"
MAX_HISTORY = 500  # lines kept in memory (stdin mode)
DEFAULT_REFRESH_INTERVAL = 3.0  # min. seconds between two screen updates (frequent full-frame USB writes can crash the device)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Terminal color names used by pyte -> RGB
DEFAULT_FG = (0, 255, 0)
DEFAULT_BG = (0, 0, 0)
COLORS = {
    "black": (0, 0, 0), "red": (205, 49, 49), "green": (13, 188, 121), "brown": (229, 229, 16),
    "blue": (36, 114, 200), "magenta": (188, 63, 188), "cyan": (17, 168, 205), "white": (229, 229, 229),
    "brightblack": (102, 102, 102), "brightred": (241, 76, 76), "brightgreen": (35, 209, 139),
    "brightbrown": (245, 245, 67), "brightblue": (59, 142, 234), "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219), "brightwhite": (255, 255, 255),
}


def wrap_line(line: str, font: ImageFont.FreeTypeFont, max_width: int) -> list:
    """Split a line into several rows so that each row fits in max_width pixels"""
    rows, current = [], ""
    for ch in line:
        if current and font.getlength(current + ch) > max_width:
            rows.append(current)
            current = ch
        else:
            current += ch
    rows.append(current)
    return rows


def render_lines(lines, font, width, height, line_height, margin) -> Image.Image:
    image = Image.new("RGBA", (width, height), DEFAULT_BG + (255,))
    draw = ImageDraw.Draw(image)
    max_rows = max(1, (height - 2 * margin) // line_height)

    # Wrap from the newest line backwards until the screen is full
    rows = []
    for line in reversed(lines):
        rows = wrap_line(line, font, width - 2 * margin) + rows
        if len(rows) >= max_rows:
            break
    for i, row in enumerate(rows[-max_rows:]):
        draw.text((margin, margin + i * line_height), row, font=font, fill=DEFAULT_FG + (255,))
    return image


def parse_color(value: str, default: tuple) -> tuple:
    if value == "default":
        return default
    if value in COLORS:
        return COLORS[value]
    if len(value) == 6:  # hex RGB from 256-color / truecolor sequences
        try:
            return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
        except ValueError:
            pass
    return default


def run_stdin_mode(lcd_comm, args, width, height):
    font = ImageFont.truetype(FONT_PATH, args.font_size)
    line_height = int(args.font_size * 1.3)
    margin = 4

    lines = deque(maxlen=MAX_HISTORY)
    lock = threading.Lock()
    changed = threading.Event()
    eof = threading.Event()
    stop = threading.Event()

    def reader():
        for raw in sys.stdin:
            text = ANSI_RE.sub("", raw.rstrip("\r\n")).replace("\t", "    ")
            with lock:
                lines.append(text)
            changed.set()
        eof.set()
        changed.set()

    def sighandler(signum, frame):
        stop.set()
        changed.set()

    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)

    threading.Thread(target=reader, daemon=True).start()

    # Initial blank screen
    lcd_comm.DisplayPILImage(render_lines([], font, width, height, line_height, margin), args.ox, args.oy)

    while not stop.is_set():
        changed.wait()
        if stop.is_set():
            break
        changed.clear()
        with lock:
            snapshot = list(lines)
        lcd_comm.DisplayPILImage(render_lines(snapshot, font, width, height, line_height, margin), args.ox, args.oy)
        if eof.is_set():
            logger.info("End of input: the last output stays on screen. Press Ctrl+C to exit.")
            while not stop.is_set():
                time.sleep(0.5)
            break
        time.sleep(args.interval)  # coalesce bursts of output into one refresh


def run_shell_mode(lcd_comm, args, geoms):
    """Run one shell per screen area in geoms [(x, y, width, height), ...] and mirror them on the screen.
    With several areas, the keyboard goes to the active one: press Ctrl+] to switch."""
    # POSIX only (pty): the shell mode is not available on Windows
    import fcntl
    import os
    import pty
    import struct
    import termios
    import tty

    try:
        import pyte
    except ImportError:
        logger.error("Shell mode needs the pyte module: pip install pyte")
        sys.exit(1)

    font = ImageFont.truetype(MONO_FONT_PATH, args.font_size)
    font_bold = ImageFont.truetype(MONO_BOLD_FONT_PATH, args.font_size)
    cell_w = max(1, int(font.getlength("M")))
    cjk_font = ImageFont.truetype(FONT_PATH, cell_w * 2)  # full-width glyphs span two cells
    cell_h = int(args.font_size * 1.3)
    shell = os.environ.get("SHELL", "/bin/bash")
    multi = len(geoms) > 1

    class Pane:
        def __init__(self, geom):
            self.x, self.y, self.width, self.height = geom
            self.cols = max(10, self.width // cell_w)
            self.rows = max(5, self.height // cell_h)
            self.screen = pyte.Screen(self.cols, self.rows)
            self.stream = pyte.ByteStream(self.screen)
            self.alive = True
            # Start the shell on a pseudo terminal sized like the LCD text grid
            self.pid, self.master_fd = pty.fork()
            if self.pid == 0:
                os.environ["TERM"] = "xterm-256color"
                os.environ["COLUMNS"], os.environ["LINES"] = str(self.cols), str(self.rows)
                os.execvp(shell, [shell])
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))

        def render(self, active: bool) -> Image.Image:
            image = Image.new("RGBA", (self.width, self.height), DEFAULT_BG + (255,))
            draw = ImageDraw.Draw(image)
            cursor = self.screen.cursor
            for y in range(self.rows):
                line = self.screen.buffer[y]
                for x in range(self.cols):
                    char = line[x]
                    fg = parse_color(char.fg, DEFAULT_FG)
                    bg = parse_color(char.bg, DEFAULT_BG)
                    if char.reverse:
                        fg, bg = bg, fg
                    if active and self.alive and not cursor.hidden and x == cursor.x and y == cursor.y:
                        fg, bg = bg, fg  # block cursor: invert colors
                    px, py = x * cell_w, y * cell_h
                    if bg != DEFAULT_BG:
                        draw.rectangle((px, py, px + cell_w - 1, py + cell_h - 1), fill=bg + (255,))
                    if char.data.strip():
                        f = cjk_font if ord(char.data[0]) > 127 else (font_bold if char.bold else font)
                        draw.text((px, py), char.data, font=f, fill=fg + (255,))
            if multi and active:  # marker of the pane receiving the keyboard input
                draw.rectangle((0, 0, self.width - 1, 1), fill=(255, 200, 0, 255))
            return image

        def close(self):
            self.alive = False
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            try:
                os.kill(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass

    panes = [Pane(g) for g in geoms]
    # Bounding box of all panes: only this area is sent to the screen
    bx, by = min(p.x for p in panes), min(p.y for p in panes)
    bw = max(p.x + p.width for p in panes) - bx
    bh = max(p.y + p.height for p in panes) - by
    active = 0
    stdout_fd = sys.stdout.fileno()

    def repaint_terminal():
        """Redraw the PC terminal with the (plain text) content of the active pane"""
        p = panes[active]
        text = "\x1b[2J\x1b[H" + "\r\n".join(l.rstrip() for l in p.screen.display)
        text += f"\x1b[{p.screen.cursor.y + 1};{p.screen.cursor.x + 1}H"
        os.write(stdout_fd, text.encode("utf-8", "replace"))

    def compose() -> Image.Image:
        canvas = Image.new("RGBA", (bw, bh), DEFAULT_BG + (255,))
        for i, p in enumerate(panes):
            canvas.paste(p.render(i == active), (p.x - bx, p.y - by))
        return canvas

    logger.info(f"Shell started on the LCD ({panes[0].cols}x{panes[0].rows} characters per area)."
                + (" Ctrl+] switches the active area." if multi else "") + " Type 'exit' to quit.")

    stdin_fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)
    # Keep output post-processing (\n -> \r\n) so that messages printed by this process do not staircase
    raw_attrs = termios.tcgetattr(stdin_fd)
    raw_attrs[1] |= termios.OPOST | termios.ONLCR
    termios.tcsetattr(stdin_fd, termios.TCSANOW, raw_attrs)
    last_render = 0.0
    last_frame = None
    dirty = True
    try:
        while any(p.alive for p in panes):
            timeout = max(0.0, args.interval - (time.monotonic() - last_render)) if dirty else None
            fds = [stdin_fd] + [p.master_fd for p in panes if p.alive]
            readable, _, _ = select.select(fds, [], [], timeout)
            if stdin_fd in readable:
                data = os.read(stdin_fd, 4096)
                if not data:
                    break
                # Ctrl+] (0x1d) switches the active area; everything else goes to the active shell
                for i, chunk in enumerate(data.split(b"\x1d")):
                    if i > 0 and multi:
                        others = [k for k in range(len(panes)) if k != active and panes[k].alive]
                        if others:
                            active = others[0]
                            repaint_terminal()
                            dirty = True
                    if chunk and panes[active].alive:
                        os.write(panes[active].master_fd, chunk)
            for idx, p in enumerate(panes):
                if not p.alive or p.master_fd not in readable:
                    continue
                try:
                    data = os.read(p.master_fd, 65536)
                except OSError:  # shell exited
                    data = b""
                if not data:
                    p.close()
                    if idx == active:
                        alive = [k for k in range(len(panes)) if panes[k].alive]
                        if alive:
                            active = alive[0]
                            repaint_terminal()
                    dirty = True
                    continue
                if idx == active:
                    os.write(stdout_fd, data)  # also keep the terminal usable
                p.stream.feed(data)
                dirty = True
            if dirty and time.monotonic() - last_render >= args.interval:
                frame = compose()
                frame_bytes = frame.tobytes()
                if frame_bytes != last_frame:  # do not resend an identical frame
                    lcd_comm.DisplayPILImage(frame, bx, by)
                    last_frame = frame_bytes
                last_render = time.monotonic()
                dirty = False
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        for p in panes:
            if p.alive:
                p.close()


def main():
    parser = argparse.ArgumentParser(description="Use a Turing USB screen as a console display")
    parser.add_argument("--shell", action="store_true", help="run a shell and mirror it on the screen")
    parser.add_argument("--dual", action="store_true",
                        help="with --shell: one shell on each half of the screen (Ctrl+] switches the keyboard input)")
    parser.add_argument("--landscape", action="store_true", help="landscape orientation (default: portrait)")
    parser.add_argument("--side", type=int, choices=[0, 1], default=0,
                        help="screen half used: 0 = left (top in portrait), 1 = right (bottom in portrait) (default: 0)")
    parser.add_argument("--fontsize", "--font-size", dest="font_size", type=int, default=16,
                        help="font size in pixels (default: %(default)s)")
    parser.add_argument("--brightness", type=int, default=25, help="brightness in %% [0-100]")
    parser.add_argument("--interval", type=float, default=DEFAULT_REFRESH_INTERVAL,
                        help="minimum seconds between two screen updates (default: %(default)s)")
    args = parser.parse_args()

    lcd_comm = LcdCommTuringUSB()
    lcd_comm.InitializeComm()
    lcd_comm.SetBrightness(level=args.brightness)
    lcd_comm.SetOrientation(Orientation.LANDSCAPE if args.landscape else Orientation.PORTRAIT)
    full_w, full_h = lcd_comm.get_width(), lcd_comm.get_height()

    # Use only one half of the screen (split along its longest side); the other half stays black
    width, height, args.ox, args.oy = full_w, full_h, 0, 0
    if full_w >= full_h:
        width = full_w // 2
        args.ox = args.side * width
    else:
        height = full_h // 2
        args.oy = args.side * height

    if args.dual and not args.shell:
        parser.error("--dual requires --shell")

    if args.shell:
        if args.dual:
            geoms = [(0, 0, width, height),
                     (width if full_w >= full_h else 0, 0 if full_w >= full_h else height, width, height)]
        else:
            geoms = [(args.ox, args.oy, width, height)]
        run_shell_mode(lcd_comm, args, geoms)
    else:
        run_stdin_mode(lcd_comm, args, width, height)


if __name__ == "__main__":
    main()
