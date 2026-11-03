"""V3A: ordered transcript with a mouse scrollbar and a pinned live footer.

    python previews/concurrency_preview_v3_pinned.py
    python previews/concurrency_preview_v3_pinned.py -if --speed 1
    python previews/concurrency_preview_v3_pinned.py --start-at 10 --paused

Wheel: scroll transcript. Click/drag its right-hand scrollbar. Arrows/PgUp/PgDn
also browse; Home shows the first file; End follows completions. Space pauses.
Q/Ctrl+C stops. The full ordered transcript is printed to scrollback on exit.
Windows uses native console mouse events; other terminals use SGR mouse reports.
All data is fictional; no media, decoder processes or reports are involved.
"""
import math
import os
import pathlib
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _concurrency_preview_v3 as shared


class PinnedScreen(shared.v2.Screen):
    def __init__(self, reporter):
        super().__init__(reporter)
        self.drag_offset = None

    def frame(self, preview, now, columns, height, events=(), paused=False):
        r = preview.reporter
        width = max(1, columns - 1)
        content_width = max(1, width - 2)
        document, bounds = preview.document(now, content_width)
        footer = preview.footer(now, width, paused)
        if height < len(footer) + 3:
            footer = footer[:max(1, height - 3)]
        room = max(1, height - len(footer) - 1)
        maximum = max(0, len(document) - room)
        thumb_size = max(1, min(room, round(room * room / max(1, len(document)))))
        travel = room - thumb_size

        def thumb_start():
            return round(preview.offset * travel / maximum) if maximum else 0

        for event in events:
            action = event.get('action')
            if action in ('up', 'down', 'pageup', 'pagedown', 'home'):
                preview.follow = False
                delta = {'up': -1, 'down': 1, 'pageup': -max(1, room - 1), 'pagedown': max(1, room - 1)}
                preview.offset = 0 if action == 'home' else preview.offset + delta[action]
            elif action == 'end':
                preview.follow = True
            elif action == 'wheel' and 2 <= event['y'] <= room + 1:
                preview.follow = False
                preview.offset += event['lines']
            elif action == 'release':
                self.drag_offset = None
            elif action in ('click', 'drag'):
                if action == 'click' and event['x'] >= width - 1 and 2 <= event['y'] <= room + 1:
                    row = event['y'] - 2
                    start = thumb_start()
                    self.drag_offset = row - start if start <= row < start + thumb_size else thumb_size // 2
                if self.drag_offset is not None:
                    preview.follow = False
                    position = min(travel, max(0, event['y'] - 2 - self.drag_offset))
                    preview.offset = round(position * maximum / travel) if travel else 0
            preview.offset = min(maximum, max(0, preview.offset))

        if preview.follow:
            completed = [i for i, file in enumerate(preview.files) if file.end <= now]
            if completed:
                latest = max(completed, key=lambda i: preview.files[i].end)
                start, end = bounds[latest]
                if start < preview.offset:
                    preview.offset = start
                elif end > preview.offset + room:
                    preview.offset = max(start, end - room)
        preview.offset = min(maximum, max(0, preview.offset))
        visible = document[preview.offset:preview.offset + room]
        visible += [''] * (room - len(visible))
        top = thumb_start()
        unicode = r.symbols['done'] == '\u2713'
        body = []
        for row, line in enumerate(visible):
            text = shared.v1.fit(line, content_width)
            padding = ' ' * max(0, content_width - len(shared.v2.ANSI.sub('', text)))
            thumb = top <= row < top + thumb_size
            symbol = ('\u2588' if unicode else '#') if thumb else ('\u2502' if unicode else '|')
            body.append(text + padding + ' ' + r.paint(symbol, shared.v1.CYAN if thumb else shared.v1.GRAY))
        mode = 'follow completions' if preview.follow else 'browsing'
        title = r.paint(f' VerifyVideo v3 pinned | rows {preview.offset + 1}-'
                        f'{min(len(document), preview.offset + room)}/{len(document)} | {mode}',
                        shared.v1.CYAN, shared.v1.BOLD)
        return ([title] + body + footer)[:height]


def windows_event(record, viewport=(0, 0)):
    """Translate INPUT_RECORD; kept separate so wheel/drag handling is checkable."""
    if record.EventType == 1:
        key = record.Event.KeyEvent
        if not key.bKeyDown:
            return []
        if key.uChar.UnicodeChar == '\x03':
            raise KeyboardInterrupt
        action = {0x26: 'up', 0x28: 'down', 0x21: 'pageup', 0x22: 'pagedown',
                  0x24: 'home', 0x23: 'end'}.get(key.wVirtualKeyCode)
        action = action or {'q': 'quit', ' ': 'pause', 'k': 'up', 'j': 'down'}.get(key.uChar.UnicodeChar.lower())
        return [{'action': action}] * max(1, key.wRepeatCount) if action else []
    if record.EventType != 2:
        return []
    mouse = record.Event.MouseEvent
    position = {'x': mouse.dwMousePosition.X - viewport[0] + 1,
                'y': mouse.dwMousePosition.Y - viewport[1] + 1}
    if mouse.dwEventFlags & 4:
        delta = (mouse.dwButtonState >> 16) & 0xffff
        delta = delta - 0x10000 if delta >= 0x8000 else delta
        return [dict(position, action='wheel', lines=-int(math.copysign(max(1, round(abs(delta) / 120)) * 3, delta)))] if delta else []
    if mouse.dwEventFlags & 8:  # Horizontal wheel is not transcript navigation.
        return []
    action = ('drag' if mouse.dwEventFlags & 1 else 'click') if mouse.dwButtonState & 1 else 'release'
    return [dict(position, action=action)]


def console_types():
    import ctypes as c
    from ctypes import wintypes as w

    class Coord(c.Structure):
        _fields_ = [('X', w.SHORT), ('Y', w.SHORT)]

    class Character(c.Union):
        _fields_ = [('UnicodeChar', w.WCHAR), ('AsciiChar', c.c_char)]

    class Key(c.Structure):
        _fields_ = [('bKeyDown', w.BOOL), ('wRepeatCount', w.WORD), ('wVirtualKeyCode', w.WORD),
                    ('wVirtualScanCode', w.WORD), ('uChar', Character), ('dwControlKeyState', w.DWORD)]

    class Mouse(c.Structure):
        _fields_ = [('dwMousePosition', Coord), ('dwButtonState', w.DWORD),
                    ('dwControlKeyState', w.DWORD), ('dwEventFlags', w.DWORD)]

    class Event(c.Union):
        _fields_ = [('KeyEvent', Key), ('MouseEvent', Mouse), ('padding', c.c_byte * 16)]

    class Record(c.Structure):
        _fields_ = [('EventType', w.WORD), ('Event', Event)]

    class Rect(c.Structure):
        _fields_ = [('Left', w.SHORT), ('Top', w.SHORT), ('Right', w.SHORT), ('Bottom', w.SHORT)]

    class Buffer(c.Structure):
        _fields_ = [('dwSize', Coord), ('dwCursorPosition', Coord), ('wAttributes', w.WORD),
                    ('srWindow', Rect), ('dwMaximumWindowSize', Coord)]

    return Record, Buffer


class MouseInput:
    def __init__(self):
        self.backend = None
        self.saved = None
        self.pending = ''

    def __enter__(self):
        if not sys.stdin.isatty():
            return self
        if os.name == 'nt':
            import ctypes as c
            from ctypes import wintypes as w
            self.c, self.w = c, w
            self.Record, self.Buffer = console_types()
            self.api = c.WinDLL('kernel32', use_last_error=True)
            self.api.GetStdHandle.argtypes, self.api.GetStdHandle.restype = [w.DWORD], w.HANDLE
            self.api.GetConsoleMode.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
            self.api.SetConsoleMode.argtypes = [w.HANDLE, w.DWORD]
            self.api.GetNumberOfConsoleInputEvents.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
            self.api.ReadConsoleInputW.argtypes = [w.HANDLE, c.POINTER(self.Record), w.DWORD, c.POINTER(w.DWORD)]
            self.api.GetConsoleScreenBufferInfo.argtypes = [w.HANDLE, c.POINTER(self.Buffer)]
            self.handle = self.api.GetStdHandle(-10 & 0xffffffff)
            self.out_handle = self.api.GetStdHandle(-11 & 0xffffffff)
            mode = w.DWORD()
            if self.api.GetConsoleMode(self.handle, c.byref(mode)):
                # Mouse/window events + extended flags; disable Quick Edit and VT input.
                flags = (mode.value | 0x98) & ~(0x40 | 0x200 | 0x2 | 0x4)
                if self.api.SetConsoleMode(self.handle, flags):
                    self.saved = mode.value
                    self.backend = 'windows'
        else:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.backend = 'sgr'
            sys.stdout.write('\033[?1002h\033[?1006h')
            sys.stdout.flush()
        if self.backend is None:
            self.fallback = shared.v2.Keyboard().__enter__()
        return self

    def read_all(self):
        if self.backend == 'windows':
            count = self.w.DWORD()
            if not self.api.GetNumberOfConsoleInputEvents(self.handle, self.c.byref(count)) or not count.value:
                return []
            records = (self.Record * min(count.value, 128))()
            received = self.w.DWORD()
            if not self.api.ReadConsoleInputW(self.handle, records, len(records), self.c.byref(received)):
                return []
            buffer = self.Buffer()
            viewport = (0, 0)
            if self.api.GetConsoleScreenBufferInfo(self.out_handle, self.c.byref(buffer)):
                viewport = (buffer.srWindow.Left, buffer.srWindow.Top)
            return [event for record in records[:received.value] for event in windows_event(record, viewport)]
        if self.backend == 'sgr':
            import select
            if select.select([self.fd], [], [], 0)[0]:
                self.pending += os.read(self.fd, 4096).decode('ascii', errors='ignore')
            events, self.pending = decode_sgr(self.pending)
            return events
        if hasattr(self, 'fallback'):
            action = self.fallback.read()
            return [{'action': action}] if action else []
        return []

    def __exit__(self, *exception):
        if self.backend == 'windows':
            self.api.SetConsoleMode(self.handle, self.saved)
        elif self.backend == 'sgr':
            import termios
            try:
                sys.stdout.write('\033[?1002l\033[?1006l')
                sys.stdout.flush()
            finally:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        elif hasattr(self, 'fallback'):
            self.fallback.__exit__(*exception)


def decode_sgr(pending):
    """Decode complete keyboard/mouse sequences, retaining partial input."""
    events = []
    keys = {'\033[A': 'up', '\033[B': 'down', '\033[5~': 'pageup', '\033[6~': 'pagedown',
            '\033[H': 'home', '\033[F': 'end', '\033[1~': 'home', '\033[4~': 'end',
            '\033OH': 'home', '\033OF': 'end'}
    while pending:
        mouse = re.match(r'\033\[<(\d+);(\d+);(\d+)([Mm])', pending)
        if mouse:
            button, x, y = map(int, mouse.group(1, 2, 3))
            action = ('wheel' if button & 64 else 'release' if mouse.group(4) == 'm' else
                      'drag' if button & 32 else 'click')
            if button & 64 and button & 2:
                pending = pending[mouse.end():]
                continue
            if action == 'wheel' or button & 3 == 0:
                event = {'action': action, 'x': x, 'y': y}
                if action == 'wheel':
                    event['lines'] = 3 if button & 1 else -3
                events.append(event)
            pending = pending[mouse.end():]
            continue
        matched = next((sequence for sequence in keys if pending.startswith(sequence)), None)
        if matched:
            events.append({'action': keys[matched]})
            pending = pending[len(matched):]
            continue
        if any(sequence.startswith(pending) for sequence in keys) or re.fullmatch(r'\033\[<[0-9;]*', pending):
            break
        character, pending = pending[0], pending[1:]
        if character == '\x03':
            raise KeyboardInterrupt
        action = {'q': 'quit', ' ': 'pause', 'k': 'up', 'j': 'down'}.get(character.lower())
        if action:
            events.append({'action': action})
    return events, pending


def main(argv=None):
    return shared.run(__doc__, True, PinnedScreen, MouseInput, argv)


if __name__ == '__main__':
    sys.exit(main())
