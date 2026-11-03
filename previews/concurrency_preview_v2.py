"""V2 mock UI: an ordered, scrollable transcript above a compact live footer.

    python previews/concurrency_preview_v2.py
    python previews/concurrency_preview_v2.py -if --speed 1
    python previews/concurrency_preview_v2.py --jobs 3
    python previews/concurrency_preview_v2.py --no-anim
    python previews/concurrency_preview_v2.py --snapshot 10

Page Up/Down or arrows: browse transcript. Home: first file. End: resume follow.
Space: pause/resume the simulation. Q or Ctrl+C: stop and keep current details.
Completed stages and diagnostics appear immediately, in the original file order.
Only changed terminal rows are rewritten; the screen is never cleared per tick.
The final full transcript and summary are printed to normal terminal scrollback.
All data is fictional. No media, FFmpeg processes, or reports are involved.
"""
import argparse
import math
import os
import pathlib
import re
import shutil
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import concurrency_preview as v1


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


def states_at(file, now):
    # A completed file must not retain a running row due to float rounding.
    if now >= file.end:
        return [(stage, 'done', 1) for stage in file.stages]
    return list(v1.stage_states(file, now))


class Transcript(v1.Preview):
    def __init__(self, reporter, files, jobs):
        super().__init__(reporter, files, jobs)
        self.offset = 0
        self.follow = True

    def file_lines(self, index, now):
        r, file = self.reporter, self.files[index]
        lines = [self.heading(index)]
        states = states_at(file, now)
        for stage, state, fraction in states:
            if state == 'done':
                lines.append(self.stage_line(stage, state, fraction, now))
            else:
                # Progress animation belongs exclusively in the footer.
                label = 'running (see live progress)' if state == 'running' else 'queued'
                if state == 'queued' and stage.tool == 'Check':
                    label = 'waiting'
                prefix = f'    {stage.tool.ljust(9)}{stage.label.ljust(25)}'
                lines.append(r.paint(prefix + label, v1.CYAN if state == 'running' else v1.GRAY))

        completed = {(stage.tool, stage.label) for stage, state, _ in states if state == 'done'}
        messages = []
        # Reveal each mock diagnostic when its own stage finishes, not at batch end.
        for message in file.warnings:
            trigger = ('FFmpeg', 'decode all A/V streams') if 'skipped' in message else ('Check', 'A/V durations & offsets')
            if trigger in completed:
                messages.append(('warn', message))
        for message in file.errors:
            trigger = ('FFprobe', 'video stream 0') if message.startswith('FFprobe') else ('FFmpeg', 'decode all A/V streams')
            if trigger in completed:
                messages.append(('error', message))
        if messages:
            lines.append('  ' + r.paint(r.symbols['stem'], v1.GRAY))
            for position, (state, message) in enumerate(messages):
                branch = r.symbols['last' if position == len(messages) - 1 else 'branch']
                colour = v1.RED if state == 'error' else v1.YELLOW
                lines.append(f'  {r.paint(branch, v1.GRAY)} {r.paint(r.symbols[state], colour, v1.BOLD)} {message}')
        if now >= file.end:
            lines.append(self.verdict_line(file))
        return lines

    def document(self, now, width):
        lines, bounds = [], {}
        for index, file in enumerate(self.files):
            if file.start > now:
                continue
            if lines:
                lines.append('')
            start = len(lines)
            for line in self.file_lines(index, now):
                lines.extend(wrap_line(line, width))
            bounds[index] = (start, len(lines))
        return lines, bounds

    def progress_line(self, index, now, width):
        r, file = self.reporter, self.files[index]
        states = states_at(file, now)
        running = next(((stage, fraction) for stage, state, fraction in states if state == 'running'), None)
        if running is None:
            return f'  [{index + 1}] {file.name} | finishing'
        stage, fraction = running
        # Reserve the right end for progress even on a narrow terminal.
        progress = f'[{r.bar(fraction, 12)}] {fraction * 100:5.1f}%' if stage.media and width >= 80 else (
            f'{fraction * 100:5.1f}%' if stage.media else 'checking')
        progress_width = 21 if stage.media and width >= 80 else 6 if stage.media else 8
        label_width = max(6, min(29, (width - progress_width - 5) // 2))
        name_width = max(6, min(28, width - label_width - progress_width - 5))
        name = v1.fit(f'[{index + 1}/{len(self.files)}] {file.name}', name_width).ljust(name_width)
        label = v1.fit(f'{stage.tool} {stage.label}', label_width).ljust(label_width)
        return f'  {r.paint(name, v1.CYAN)} {label} {progress}'

    def footer(self, now, width, paused=False):
        r = self.reporter
        active = [i for i, file in enumerate(self.files) if file.start <= now < file.end]
        finished = sum(now >= file.end for file in self.files)
        queued = sum(now < file.start for file in self.files)
        lines = [r.paint(r.symbols['rule'] * min(18, max(1, width - 16)) + ' Live progress ', v1.GRAY),
                 f'  {finished}/{len(self.files)} finished | {len(active)} active | {queued} queued'
                 f' | elapsed {v1.clock_time(now)}' + (' | PAUSED' if paused else '')]
        lines.extend(self.progress_line(index, now, width) for index in active)
        if not active:
            lines.append(r.paint('  All mock files finished.', v1.GREEN))
        lines.append(r.paint('  PgUp/PgDn: browse | Home: first | End: follow | Space: pause | Q: stop', v1.GRAY))
        return lines

    def frame(self, now, columns, height, action=None, paused=False):
        width = max(1, columns - 1)
        document, bounds = self.document(now, width)
        footer = self.footer(now, width, paused)
        if height < len(footer) + 3:
            footer = footer[:max(1, height - 3)]
        room = max(1, height - len(footer) - 1)
        maximum = max(0, len(document) - room)
        if action in ('up', 'down', 'pageup', 'pagedown', 'home'):
            self.follow = False
            delta = {'up': -1, 'down': 1, 'pageup': -max(1, room - 1), 'pagedown': max(1, room - 1)}
            self.offset = 0 if action == 'home' else self.offset + delta[action]
        elif action == 'end':
            self.follow = True
            self.offset = maximum

        if self.follow:
            # Bring finished details into view immediately, even while file 1 runs.
            finished = [i for i, file in enumerate(self.files) if now >= file.end]
            if finished:
                latest = max(finished, key=lambda i: self.files[i].end)
                start, end = bounds[latest]
                if start < self.offset:
                    self.offset = start
                elif end > self.offset + room:
                    self.offset = max(start, end - room)
        self.offset = min(maximum, max(0, self.offset))
        end = min(len(document), self.offset + room)
        mode = 'follow completions' if self.follow else 'browsing'
        title = self.reporter.paint(f' VerifyVideo v2 | Transcript {self.offset + 1}-{end}/{len(document)}'
                                    f' | {mode}', v1.CYAN, v1.BOLD)
        body = document[self.offset:end]
        return ([title] + body + [''] * (room - len(body)) + footer)[:height]

    def snapshot(self, now, width):
        lines = [f'\n  --- V2 snapshot at {v1.clock_time(now)} ---',
                 self.reporter.paint('  Full transcript | original file order', v1.CYAN, v1.BOLD)]
        document, _ = self.document(now, width)
        return lines + document + [''] + self.footer(now, width)


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
        fitted = [v1.fit(line, max(1, columns - 1)) for line in lines]
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
        return {'q': 'quit', ' ': 'pause', 'k': 'up', 'j': 'down'}.get(key.lower())

    def __exit__(self, *exception):
        if self.saved is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


def nonnegative_float(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError('must be a finite number greater than or equal to zero')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--jobs', type=int, choices=range(1, 6), default=2, help='Mock file workers (default: 2)')
    parser.add_argument('-if', '--ignore-ffmpeg', action='store_true', help='Skip the mock FFmpeg stage')
    parser.add_argument('--speed', type=v1.positive_float, default=2, help='Simulation speed (default: 2)')
    parser.add_argument('--no-anim', action='store_true', help='Print representative full-transcript snapshots')
    parser.add_argument('--snapshot', type=nonnegative_float, metavar='SECONDS', help='Print one simulated moment')
    args = parser.parse_args(argv)
    r = v1.Reporter()
    files = v1.mock_files(args.ignore_ffmpeg)
    v1.schedule(files, args.jobs)
    preview = Transcript(r, files, args.jobs)
    total = max(file.end for file in files)
    v1.print_banner(r)
    r.message(r.paint('  CONCURRENCY PREVIEW V2 | fictional data and timings', v1.CYAN, v1.BOLD))
    r.message(f'  Workers    {preview.jobs} files; sequential checks within each file')
    r.message('  Checks     FFprobe scans' + ('' if args.ignore_ffmpeg else ' + FFmpeg decode'))
    r.message(f'  Animation  {args.speed:g}x speed; displayed times are simulated')
    r.message('  Transcript Ordered file blocks; stage details appear as soon as available')
    for index, file in enumerate(files, 1):
        r.message(f'    {index}  {file.name}')

    if args.snapshot is not None:
        for line in preview.snapshot(min(args.snapshot, total), 109):
            r.message(line)
        return 0
    if args.no_anim or not r.ansi:
        moments = sorted({0.0, min(file.end for file in files) + 0.01, max(0, files[0].end - 0.5)})
        for now in moments:
            for line in preview.snapshot(now, 109):
                r.message(line)
        r.message('\n  --- Final ordered transcript ---')
        for line in preview.document(total, 109)[0] + preview.summary(total):
            r.message(line)
        return 0

    now, paused, stopped = 0.0, False, False
    last_tick = time.monotonic()
    try:
        with Screen(r) as screen, Keyboard() as keyboard:
            while True:
                tick = time.monotonic()
                if not paused:
                    now = min(total, now + (tick - last_tick) * args.speed)
                last_tick = tick
                action = keyboard.read()
                if action == 'quit':
                    stopped = True
                    break
                if action == 'pause':
                    paused = not paused
                columns, rows = shutil.get_terminal_size((110, 35))
                height = max(1, rows - 1)  # Avoid scrolling at the last physical row.
                screen.draw(preview.frame(now, columns, height, action, paused), columns, height)
                if now >= total:
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        stopped = True

    r.message('\n  --- ' + ('Stopped: current' if stopped else 'Final ordered') + ' transcript ---')
    width = max(1, shutil.get_terminal_size((110, 35)).columns - 1)
    for line in preview.document(now, width)[0]:
        r.message(line)
    if stopped:
        r.message('\n  Preview stopped. Only details reached so far are shown.')
        return 130
    for line in preview.summary(total):
        r.message(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
