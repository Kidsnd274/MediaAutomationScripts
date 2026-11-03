"""Concurrent terminal input/rendering adapted from the approved V5 reference.

Uses real structured verification events; no runtime imports from previews.
"""
import copy
from dataclasses import dataclass, field
import math
import os
import re
import shutil
import sys
import threading
import time

import VerifyVideo as vv
from verification_runtime import FilePool

ANSI = re.compile(r'(\033\[[0-9;]*m)')

def wrap_line(line, width):
    """Wrap transcript text, carrying its colour onto continuation rows."""
    width = max(1, width)
    continuation = ' ' * min(4, width - 1)
    rows, active, current, used = [], [], '', 0
    for part in ANSI.split(line):
        if ANSI.fullmatch(part):
            active = [] if part == '\033[0m' else active + [part]
            current += part
            continue
        for character in part:
            if used == width:
                rows.append(current + ('\033[0m' if active else ''))
                current = continuation + ''.join(active)
                used = len(continuation)
            current += character
            used += 1
    rows.append(current + ('\033[0m' if active else ''))
    return rows


class Screen:
    """A single buffered write per frame; untouched rows are left alone."""
    def __init__(self, reporter):
        self.output = reporter.output
        self.previous = []
        self.size = None

    def __enter__(self):
        # Keep the caller's screen; final results return to ordinary scrollback.
        self.output.write('\033[?1049h\033[?25l')
        self.output.flush()
        return self

    def draw(self, lines, columns, height):
        if self.size != (columns, height):
            self.previous = []
            self.size = (columns, height)
        fitted = [vv.fit(line, max(1, columns - 1)) for line in lines]
        fitted += [''] * (height - len(fitted))
        updates = []
        for row, line in enumerate(fitted):
            if row >= len(self.previous) or line != self.previous[row]:
                # Replace the row before erasing its old trailing characters.
                updates.append(f'\033[{row + 1};1H{line}\033[0m\033[K')
        if updates:
            self.output.write(''.join(updates))
            self.output.flush()
        self.previous = fitted

    def __exit__(self, *exception):
        self.output.write('\033[0m\033[?25h\033[?1049l')
        self.output.flush()


class Keyboard:
    """Non-blocking navigation using only the standard library."""
    def __init__(self):
        self.saved = None
        self.enabled = False
        self.pending = ''

    def __enter__(self):
        if not sys.stdin.isatty():
            return self
        try:
            if os.name == 'nt':
                import msvcrt
                self.windows = msvcrt
            else:
                import termios
                import tty
                self.fd = sys.stdin.fileno()
                self.saved = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
            self.enabled = True
        except (ImportError, OSError, ValueError):
            pass
        return self

    def read(self):
        if not self.enabled:
            return None
        if os.name == 'nt':
            if not self.windows.kbhit():
                return None
            key = self.windows.getwch()
            if key in ('\x00', '\xe0'):
                return {'H': 'up', 'P': 'down', 'I': 'pageup', 'Q': 'pagedown',
                        'G': 'home', 'O': 'end'}.get(self.windows.getwch())
        else:
            import select
            if select.select([self.fd], [], [], 0)[0]:
                self.pending += os.read(self.fd, 64).decode('ascii', errors='ignore')
            if not self.pending:
                return None
            sequences = {'\033[A': 'up', '\033[B': 'down', '\033[5~': 'pageup', '\033[6~': 'pagedown',
                         '\033[H': 'home', '\033[F': 'end', '\033[1~': 'home', '\033[4~': 'end',
                         '\033OH': 'home', '\033OF': 'end'}
            for sequence, action in sequences.items():
                if self.pending.startswith(sequence):
                    self.pending = self.pending[len(sequence):]
                    return action
            if any(sequence.startswith(self.pending) for sequence in sequences):
                return None
            key, self.pending = self.pending[0], self.pending[1:]
        if key == '\x03':
            raise KeyboardInterrupt
        return {'q': 'quit', 'k': 'up', 'j': 'down'}.get(key.lower())

    def __exit__(self, *exception):
        if self.saved is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


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
        action = action or {'q': 'quit', 'k': 'up', 'j': 'down'}.get(key.uChar.UnicodeChar.lower())
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
            self.fallback = Keyboard().__enter__()
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
        action = {'q': 'quit', 'k': 'up', 'j': 'down'}.get(character.lower())
        if action:
            events.append({'action': action})
    return events, pending


@dataclass
class JobState:
    name: str
    started: bool = False
    stages: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    result: object = None
    elapsed: float = 0
    terminal: bool = False
    cancelled: bool = False
    completion: int = 0


class Model:
    """Reconciles reliable final records and rejects stale/terminal progress."""
    def __init__(self, names):
        self.files = [JobState(name) for name in names]
        self.lock = threading.Lock()
        self.revision = 0
        self.pending = []

    def accept(self, event):
        with self.lock:
            job = self.files[event['job_index']]
            if job.terminal:
                return
            kind = event['kind']
            if kind == 'job_start':
                job.started = True
            elif kind in ('stage_start', 'stage_finish', 'progress'):
                stage = dict(event['stage'])
                previous = job.stages.get(stage['stage_id'])
                if previous and (previous['state'] != 'running' or
                                 previous.get('sequence', 0) >= event['sequence']):
                    return
                stage['sequence'] = event['sequence']
                job.stages[stage['stage_id']] = stage
            elif kind == 'diagnostic':
                item = (event['state'], event['message'])
                if item not in job.diagnostics:
                    job.diagnostics.append(item)
            elif kind == 'job_finish':
                job.started = job.terminal = True
                job.cancelled = event['cancelled']
                job.result, job.elapsed = event['result'], event['elapsed']
                if event['stages'] is not None:
                    job.stages = {stage['stage_id']: dict(stage) for stage in event['stages']}
                else:
                    # A crashed worker cannot supply a final snapshot. Preserve
                    # its known diagnostics and mark unfinished stages honestly.
                    if job.result:
                        for state, message in reversed(job.diagnostics):
                            issues = job.result.errors if state == 'error' else job.result.warnings
                            if message not in issues:
                                issues.insert(0, message)
                    for stage_id, stage in list(job.stages.items()):
                        if stage['state'] == 'running':
                            job.stages[stage_id] = dict(stage, state='error', elapsed_seconds=None,
                                                       details=['worker exited; stage incomplete'])
                if event['diagnostics'] is not None:
                    job.diagnostics = list(event['diagnostics'])
                # Operational errors need to be visible even without diagnostic events.
                if job.result:
                    for item in ([('warn', x) for x in job.result.warnings] +
                                 [('error', x) for x in job.result.errors]):
                        if item not in job.diagnostics:
                            job.diagnostics.append(item)
                self.revision += 1
                job.completion = self.revision
            if kind != 'progress':
                self.pending.append(copy.deepcopy(event))

    def snapshot(self):
        with self.lock:
            # Stage dictionaries are replaced, never mutated after publication.
            # Copy their containers without holding the collector for a deep
            # copy of the entire completed transcript on each screen refresh.
            files = []
            for job in self.files:
                snapshot = copy.copy(job)
                snapshot.stages = dict(job.stages)
                snapshot.diagnostics = list(job.diagnostics)
                files.append(snapshot)
            return files

    def logs(self):
        with self.lock:
            events, self.pending = self.pending, []
            return events


def verdict_lines(reporter, result, elapsed):
    r = reporter
    messages = [('warn', message) for message in result.warnings] + [('error', message) for message in result.errors]
    lines = []
    if messages:
        lines.append('  ' + r.paint(r.symbols['stem'], vv.GRAY))
        for position, (state, message) in enumerate(messages):
            branch = r.symbols['last' if position == len(messages) - 1 else 'branch']
            lines.append('  %s %s %s' % (r.paint(branch, vv.GRAY),
                         r.paint(r.symbols[state], vv.STATE_COLOURS[state], vv.BOLD), message))
    verdict, _, colour = vv.VERDICTS[result.status]
    counts = r.join([vv.plural(len(result.errors), 'error'), vv.plural(len(result.warnings), 'warning'),
                     vv.clock_time(elapsed)])
    lines.append('  %s %s  %s' % (r.paint(r.symbols['bullet'], colour),
                 r.paint(verdict, colour, vv.BOLD), r.paint(counts, vv.GRAY)))
    return lines


class Transcript:
    def __init__(self, reporter, files, jobs):
        self.reporter, self.files, self.jobs = reporter, files, jobs
        self.offset, self.follow, self.drag_offset = 0, True, None

    def heading(self, index):
        r = self.reporter
        return r.paint('[%d/%d]' % (index + 1, len(self.files)), vv.CYAN, vv.BOLD) + ' ' + r.paint(self.files[index].name, vv.BOLD)

    def stage_line(self, stage, now, compact=False):
        r = self.reporter
        state = stage['state']
        if state != 'running':
            return r.stage('skip' if state == 'cancelled' else state, stage['tool'], stage['label'],
                           stage['details'], stage['elapsed_seconds'])
        spinner = r.symbols['spinner'][int(now * 8) % len(r.symbols['spinner'])]
        prefix = '  ' + r.paint(spinner, vv.CYAN) + ' '
        prefix += ('%s %s ' % (stage['tool'], stage['label']) if compact else
                   stage['tool'].ljust(9) + stage['label'].ljust(25))
        total, position = stage['total_seconds'], stage['position_seconds']
        details = []
        if total and position is not None:
            fraction = min(0.999, max(0, position / total))
            prefix += '[%s] %5.1f%% ' % (r.bar(fraction, 10 if compact else 14), fraction * 100)
            if not compact:
                details.append(vv.clock_time(position) + ' / ' + vv.clock_time(total))
        elif position is not None:
            details.append(vv.clock_time(position) + ' processed')
        else:
            details.append('checking...')
        if stage['frame_count'] is not None and not compact:
            details.append('{:,} frames'.format(stage['frame_count']))
        return prefix + r.paint(r.join(details), vv.GRAY)

    def file_lines(self, index, now):
        job = self.files[index]
        lines = [self.heading(index)]
        lines += [self.stage_line(stage, now) for _, stage in sorted(job.stages.items())]
        if job.result:
            lines += verdict_lines(self.reporter, job.result, job.elapsed)
        else:
            for state, message in job.diagnostics:
                lines.append('  %s %s' % (self.reporter.symbols[state], message))
            if job.cancelled:
                lines.append('  Interrupted; not checked.')
            elif not job.started:
                lines.append('  Queued')
        return lines

    def document(self, now, width):
        lines, bounds = [], {}
        for index in range(len(self.files)):
            if lines:
                lines.append('')
            start = len(lines)
            for line in self.file_lines(index, now):
                lines.extend(wrap_line(line, width))
            bounds[index] = (start, len(lines))
        return lines, bounds

    def footer(self, now, width):
        r = self.reporter
        active = [i for i, job in enumerate(self.files) if job.started and not job.terminal]
        done = sum(job.terminal for job in self.files)
        left = r.symbols['rule'] * min(18, max(1, width - 16)) + ' Live progress '
        lines = [r.paint(left + r.symbols['rule'] * max(0, width - len(left)), vv.GRAY),
                 '  %d/%d finished | %d active | %d queued | elapsed %s' %
                 (done, len(self.files), len(active), len(self.files) - done - len(active), vv.clock_time(now))]
        for index in active:
            stages = [s for s in self.files[index].stages.values() if s['state'] == 'running']
            spinner = r.symbols['spinner'][int(now * 8) % len(r.symbols['spinner'])]
            prefix = '  ' + r.paint(spinner, vv.CYAN) + ' '
            stage = stages[-1] if stages else None
            total, position = (stage['total_seconds'], stage['position_seconds']) if stage else (None, None)
            bar_width = max(3, min(12, (width - 40) // 3))
            fraction = min(0.999, max(0, position / total)) if total and position is not None else None
            progress = ('[%s] %5.1f%%' % (r.bar(fraction, bar_width), fraction * 100) if fraction is not None else
                        vv.clock_time(position) + ' processed' if position is not None else 'checking')
            if width < 32:
                lines.append(prefix + '[%d] ' % (index + 1) + progress)
                continue
            available = max(2, width - 6 - vv.visible_width(progress))
            name_width = min(28, available // 2)
            label_width = available - name_width
            name = vv.fit('[%d/%d] %s' % (index + 1, len(self.files), self.files[index].name), name_width)
            label = vv.fit(stage['tool'] + ' ' + stage['label'] if stage else 'starting / finishing', label_width)
            lines.append(prefix + r.paint(name.ljust(name_width), vv.CYAN) + ' ' + label.ljust(label_width) + ' ' + progress)
        lines.append(r.paint('  Wheel/drag: scroll | Arrows/PgUp/PgDn | Home/End: first/follow | Q: stop', vv.GRAY))
        return lines

    def frame(self, now, columns, height, events=()):
        # V5's transcript viewport and draggable scrollbar, driven by real state.
        r = self.reporter
        width = max(1, columns - 1)
        content_width = max(1, width - 2)
        document, bounds = self.document(now, content_width)
        footer = self.footer(now, width)
        if height < len(footer) + 3:
            footer = footer[:max(1, height - 3)]
        room = max(1, height - len(footer) - 1)
        maximum = max(0, len(document) - room)
        thumb_size = max(1, min(room, round(room * room / max(1, len(document)))))
        travel = room - thumb_size

        def thumb_start():
            return round(self.offset * travel / maximum) if maximum else 0

        for event in events:
            action = event.get('action')
            if action in ('up', 'down', 'pageup', 'pagedown', 'home'):
                self.follow = False
                delta = {'up': -1, 'down': 1, 'pageup': -max(1, room - 1), 'pagedown': max(1, room - 1)}
                self.offset = 0 if action == 'home' else self.offset + delta[action]
            elif action == 'end':
                self.follow = True
            elif action == 'wheel' and 2 <= event['y'] <= room + 1:
                self.follow = False
                self.offset += event['lines']
            elif action == 'release':
                self.drag_offset = None
            elif action in ('click', 'drag'):
                if action == 'click' and event['x'] >= width - 1 and 2 <= event['y'] <= room + 1:
                    row = event['y'] - 2
                    start = thumb_start()
                    self.drag_offset = row - start if start <= row < start + thumb_size else thumb_size // 2
                if self.drag_offset is not None:
                    self.follow = False
                    position = min(travel, max(0, event['y'] - 2 - self.drag_offset))
                    self.offset = round(position * maximum / travel) if travel else 0
            self.offset = min(maximum, max(0, self.offset))
        if self.follow:
            completed = [i for i, job in enumerate(self.files) if job.terminal]
            if completed:
                latest = max(completed, key=lambda i: self.files[i].completion)
                start, end = bounds[latest]
                if start < self.offset:
                    self.offset = start
                elif end > self.offset + room:
                    self.offset = max(start, end - room)
        self.offset = min(maximum, max(0, self.offset))
        visible = document[self.offset:self.offset + room]
        visible += [''] * (room - len(visible))
        top = thumb_start()
        unicode = r.symbols['done'] == '\u2713'
        body = []
        for row, line in enumerate(visible):
            text = vv.fit(line, content_width)
            padding = ' ' * max(0, content_width - vv.visible_width(text))
            thumb = top <= row < top + thumb_size
            symbol = ('\u2588' if unicode else '#') if thumb else ('\u2502' if unicode else '|')
            body.append(text + padding + ' ' + r.paint(symbol, vv.CYAN if thumb else vv.GRAY))
        mode = 'follow completions' if self.follow else 'browsing'
        title = r.paint(' VerifyVideo | rows %d-%d/%d | %s' %
                        (self.offset + 1, min(len(document), self.offset + room), len(document), mode), vv.CYAN, vv.BOLD)
        return ([title] + body + footer)[:height]


def run_concurrent(files, names, jobs, config, reporter):
    import contextlib
    model = Model(names)
    transcript = Transcript(reporter, model.snapshot(), jobs)
    pool = FilePool(files, jobs, config, model.accept)
    interrupted = False
    started = time.monotonic()
    try:
        with contextlib.ExitStack() as stack:
            keyboard = screen = None
            if reporter.ansi and vv.is_terminal(sys.stdin):
                keyboard = stack.enter_context(MouseInput())
                if keyboard.backend:
                    screen = stack.enter_context(Screen(reporter))
            pool.start()
            last_plain_update = time.monotonic()
            while not pool.done.is_set():
                events = keyboard.read_all() if keyboard else []
                if any(event.get('action') == 'quit' for event in events):
                    raise KeyboardInterrupt
                if screen:
                    transcript.files = model.snapshot()
                    columns, rows = shutil.get_terminal_size((110, 35))
                    height = max(1, rows - 1)
                    screen.draw(transcript.frame(time.monotonic() - started, columns, height, events), columns, height)
                    model.logs()  # No log backlog in interactive mode.
                else:
                    for event in model.logs():
                        index, kind = event['job_index'], event['kind']
                        if kind == 'stage_start':
                            stage = event['stage']
                            reporter.message('[%d/%d] %s | %s %s ...' %
                                             (index + 1, len(files), names[index], stage['tool'], stage['label']))
                        elif kind == 'stage_finish':
                            reporter.message('[%d/%d] %s' % (index + 1, len(files), names[index]))
                            reporter.message(transcript.stage_line(event['stage'], 0))
                        elif kind == 'diagnostic':
                            reporter.message('[%d] %s: %s' % (index + 1, event['state'], event['message']))
                    if time.monotonic() - last_plain_update >= 30:
                        last_plain_update = time.monotonic()
                        for index, job in enumerate(model.snapshot()):
                            if job.terminal:
                                continue
                            for stage in job.stages.values():
                                if stage['state'] == 'running':
                                    reporter.message('[%d/%d] %s' % (index + 1, len(files),
                                                     transcript.stage_line(stage, time.monotonic() - started)))
                pool.done.wait(0.1)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if pool.thread.ident is not None:
            while True:
                try:
                    pool.close()
                    break
                except KeyboardInterrupt:
                    interrupted = True
                    pool.cancel.set()
    elapsed = time.monotonic() - started
    transcript.files = model.snapshot()
    reporter.message('\n  --- %s transcript ---' % ('Interrupted: partial ordered' if interrupted or pool.error else 'Final ordered'))
    for index in range(len(files)):
        job = transcript.files[index]
        if not job.terminal:
            job.cancelled = True
            for stage_id, stage in list(job.stages.items()):
                if stage['state'] == 'running':
                    job.stages[stage_id] = dict(stage, state='cancelled', elapsed_seconds=None,
                                               details=['interrupted; not checked'])
        for line in transcript.file_lines(index, elapsed):
            reporter.message(line)
        reporter.message()
    checked = [(index, files[index], job.result, job.elapsed) for index, job in enumerate(transcript.files)
               if job.result is not None and not job.cancelled]
    return checked, interrupted, pool.error, elapsed
