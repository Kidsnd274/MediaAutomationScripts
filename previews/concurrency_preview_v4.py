"""V4 mock UI: finished files go to normal scrollback; a small live panel tracks the workers.

    python previews/concurrency_preview_v4.py
    python previews/concurrency_preview_v4.py --jobs 3 --speed 1
    python previews/concurrency_preview_v4.py -if
    python previews/concurrency_preview_v4.py --snapshot 9      # the screen at one moment
    python previews/concurrency_preview_v4.py --no-anim         # what a redirected log looks like

Design
  * Each file's block is printed once, complete, the moment that file finishes, in exactly the
    format the sequential checker uses. Blocks never interleave, and nothing is reprinted.
  * Blocks appear in completion order. The [n/N] tag identifies each one, and the summary table
    restores the original order, so a slow first file never holds back the rest.
  * Below the blocks, a live panel is redrawn in place: one row per busy worker (file, stage pips,
    current stage, progress, problems so far) and one totals row. Only that panel changes.
  * No alternate screen, mouse capture or raw keyboard mode: the terminal's own scrollback,
    mouse wheel, selection and copy/paste keep working while checks run.
  * Redirected output gets the same blocks plus an occasional plain status line.

Ctrl+C stops; the summary then marks files that were interrupted or never started.
All data is fictional. No media is opened and no FFmpeg tools are run.
"""
import argparse
from dataclasses import dataclass
import math
import pathlib
import shutil
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from VerifyVideo import (BOLD, CYAN, GRAY, GREEN, RED, STATE_COLOURS, YELLOW, Reporter, clock_time, fit,
                         plural, print_banner, visible_width)

FPS = 10
LOG_STATUS_EVERY = 10  # Simulated seconds between plain status lines in redirected output.
PIPS = {'done': '●', 'warn': '▲', 'error': '✗', 'skip': '–', 'running': '○', 'queued': '·'}
ASCII_PIPS = {'done': '#', 'warn': '!', 'error': 'x', 'skip': '-', 'running': '>', 'queued': '.'}


# --- Mock data ---------------------------------------------------------------------------------

@dataclass
class Stage:
    tool: str
    label: str
    seconds: float
    details: tuple = ()
    media: float = 0      # Media duration the stage walks through; drives the progress bar.
    state: str = 'done'   # Final state: done, warn, error or skip.
    message: str = ''     # Diagnostic revealed when the stage finishes.


@dataclass
class File:
    name: str
    stages: list
    start: float = 0

    @property
    def duration(self):
        return sum(stage.seconds for stage in self.stages)

    @property
    def end(self):
        return self.start + self.duration

    @property
    def errors(self):
        return [stage.message for stage in self.stages if stage.state == 'error' and stage.message]

    @property
    def warnings(self):
        return [stage.message for stage in self.stages if stage.state in ('warn', 'skip') and stage.message]

    @property
    def verdict(self):
        return (('FAILED', 'FAIL', RED) if self.errors else
                ('PASSED WITH WARNINGS', 'WARN', YELLOW) if self.warnings else ('PASSED', 'PASS', GREEN))


def mock_files(ignore_ffmpeg):
    specs = [  # name, media seconds, video scan seconds, decode seconds
        ('Episode 01.mkv', 1440, 19, 9),  # Deliberately slow: later files finish around it.
        ('Episode 02.mkv', 90, 3, 2.5),
        ('Episode 03.mkv', 150, 4, 3),
        ('Episode 04.mkv', 120, 3.5, 2.5),
        ('[Subs] A Considerably Longer Series Title - 05 (1080p BD Remux).mkv', 300, 5, 4),
        ('Episode 06.mkv', 60, 2.5, 2),
    ]
    files = []
    for number, (name, media, video, decode) in enumerate(specs, 1):
        frames, packets = round(media * 23.976), round(media * 48000 / 1024)
        broken, mismatch = number == 4, number == 3
        stages = [
            Stage('FFprobe', 'metadata', 0.3, ('3 A/V streams',)),
            Stage('FFprobe', 'video stream 0', video,
                  ('see errors below',) if broken else (f'{frames:,} frames', clock_time(media)), media,
                  'error' if broken else 'done',
                  'FFprobe stream 0: error while decoding a video frame (mock diagnostic).' if broken else ''),
            Stage('FFprobe', 'audio stream 1', 0.6, (f'{packets:,} frames', clock_time(media)), media),
            Stage('FFprobe', 'audio stream 2', 0.6, (f'{packets:,} frames', clock_time(media)), media),
            Stage('FFmpeg', 'decode all A/V streams', 0, ('skipped (--ignore-ffmpeg)',), state='skip',
                  message='Independent FFmpeg decode check skipped (--ignore-ffmpeg).') if ignore_ffmpeg else
            Stage('FFmpeg', 'decode all A/V streams', decode, ('see errors below',) if broken else (), media,
                  'error' if broken else 'done',
                  'FFmpeg decode check exited with code 1 (mock diagnostic).' if broken else ''),
            Stage('Check', 'A/V durations & offsets', 0.3, ('see warnings below',) if mismatch else (),
                  state='warn' if mismatch else 'done',
                  message='A/V duration mismatch (video 0, audio 1): 0.024000s > 0.016667s tolerance.'
                  if mismatch else ''),
        ]
        files.append(File(name, stages))
    return files


def schedule(files, jobs):
    """Hand each file to whichever worker frees up first, in file order."""
    free = [0.0] * min(jobs, len(files))
    for file in files:
        worker = min(range(len(free)), key=free.__getitem__)
        file.start = free[worker]
        free[worker] = file.end


def timeline(file, now):
    """(stage, status, fraction) for each stage; status is queued, running or done."""
    states, offset = [], file.start
    for stage in file.stages:
        end = offset + stage.seconds
        if now >= end or now >= file.end:
            states.append((stage, 'done', 1.0))
        elif now < offset:
            states.append((stage, 'queued', 0.0))
        else:
            states.append((stage, 'running', (now - offset) / stage.seconds))
        offset = end
    return states


# --- Rendering ---------------------------------------------------------------------------------

def short_time(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


class View:
    # Live-row layouts, richest first: (stage pips, bar width, media position).
    LAYOUTS = ((True, 20, True), (True, 14, True), (True, 12, False), (False, 10, False), (False, 0, False))

    def __init__(self, reporter, files, jobs):
        self.r = reporter
        self.files = files
        self.jobs = min(jobs, len(files))
        self.unicode = reporter.symbols['done'] == '✓'
        self.pips = PIPS if self.unicode else ASCII_PIPS
        self.tag_width = len(self.tag(len(files) - 1))
        self.name_width = min(40, max(len(file.name) for file in files))
        self.label_width = max(len(stage.label) for file in files for stage in file.stages)
        self.media_width = max(len(f'{short_time(s.media)} / {short_time(s.media)}')
                               for file in files for s in file.stages)

    def tag(self, index):
        return f'[{index + 1}/{len(self.files)}]'

    def shorten(self, text, width):
        """Trim the middle so the distinguishing end of a filename survives."""
        if len(text) <= width:
            return text.ljust(width)
        mark = '…' if self.unicode else '~'
        head = max(1, (width - len(mark)) // 2)
        return text[:head] + mark + text[len(text) - (width - len(mark) - head):]

    # Scrollback ----------------------------------------------------------------------------

    def block(self, index):
        """A finished file, formatted exactly as the sequential checker prints it."""
        r, file = self.r, self.files[index]
        lines = ['', f'{r.paint(self.tag(index), CYAN, BOLD)} {r.paint(file.name, BOLD)}']
        for stage in file.stages:
            lines.append(r.stage(stage.state, stage.tool, stage.label, stage.details,
                                 None if stage.state == 'skip' else stage.seconds))
        messages = [('warn', m) for m in file.warnings] + [('error', m) for m in file.errors]
        if messages:
            lines.append('  ' + r.paint(r.symbols['stem'], GRAY))
            for position, (state, message) in enumerate(messages, 1):
                branch = r.symbols['last' if position == len(messages) else 'branch']
                lines.append(f'  {r.paint(branch, GRAY)} {r.paint(r.symbols[state], STATE_COLOURS[state], BOLD)}'
                             f' {message}')
        word, _, colour = file.verdict
        counts = r.join([plural(len(file.errors), 'error'), plural(len(file.warnings), 'warning'),
                         clock_time(file.duration)])
        lines.append(f'  {r.paint(r.symbols["bullet"], colour)} {r.paint(word, colour, BOLD)}  {r.paint(counts, GRAY)}')
        return lines

    # Live panel ----------------------------------------------------------------------------

    def layout(self, width):
        pips_width = max(len(file.stages) for file in self.files) + 2
        for pips, bar, media in self.LAYOUTS:
            fixed = (4 + self.tag_width + 1 + 2 + (pips_width if pips else 0) + self.label_width + 1 +
                     (bar + 1 if bar else 0) + 6 + (2 + self.media_width if media else 0) + 11)
            if width - fixed >= min(self.name_width, 16):
                break
        return pips, bar, media, max(8, min(self.name_width, width - fixed))

    def worker_row(self, index, now, width, frame):
        r, file = self.r, self.files[index]
        pips, bar, media, name_width = self.layout(width)
        states = timeline(file, now)
        spinner = r.symbols['spinner']
        row = (f'  {r.paint(spinner[frame % len(spinner)], CYAN)} {r.paint(self.tag(index).rjust(self.tag_width), CYAN)} '
               f'{r.paint(self.shorten(file.name, name_width), BOLD)}  ')
        if pips:
            marks = []
            for stage, status, _ in states:
                if status == 'done':
                    marks.append(r.paint(self.pips[stage.state], STATE_COLOURS[stage.state], BOLD))
                else:
                    marks.append(r.paint(self.pips[status], CYAN if status == 'running' else GRAY))
            row += ''.join(marks) + '  '
        current = next(((stage, fraction) for stage, status, fraction in states if status == 'running'), None)
        stage, fraction = current or (None, 0)
        row += (stage.label if stage else 'finishing').ljust(self.label_width) + ' '
        progress_width = (bar + 1 if bar else 0) + 6 + (2 + self.media_width if media else 0)
        if stage and stage.media:
            progress = (r.bar(fraction, bar) + ' ' if bar else '') + f'{fraction * 100:5.1f}%'
            if media:
                half = (self.media_width - 3) // 2
                position = f'{short_time(stage.media * fraction):>{half}} / {short_time(stage.media):<{half}}'
                progress += '  ' + r.paint(position, GRAY)
        else:
            progress = r.paint('checking…' if self.unicode else 'checking...', GRAY)
        row += progress + ' ' * max(0, progress_width - visible_width(progress))
        done = [stage for stage, status, _ in states if status == 'done']
        errors = sum(stage.state == 'error' for stage in done)
        warnings = sum(stage.state in ('warn', 'skip') and bool(stage.message) for stage in done)
        if errors:
            row += '  ' + r.paint(plural(errors, 'error'), RED, BOLD)
        elif warnings:
            row += '  ' + r.paint(plural(warnings, 'warning'), YELLOW)
        return row

    def totals_row(self, now, width, hidden=0):
        r, files = self.r, self.files
        finished = [file for file in files if file.end <= now]
        queued = sum(file.start > now for file in files)
        fraction = sum(min(1, max(0, (now - f.start) / f.duration)) if f.duration else float(now >= f.start)
                       for f in files) / len(files)
        failed = sum(bool(f.errors) for f in finished)
        warned = sum(bool(f.warnings) and not f.errors for f in finished)
        clean = len(finished) - failed - warned
        counts = '  '.join(r.paint(f'{r.symbols[state]} {count}', colour, BOLD) if count else
                           r.paint(f'{r.symbols[state]} {count}', GRAY)
                           for state, count, colour in (('done', clean, GREEN), ('warn', warned, YELLOW),
                                                        ('error', failed, RED)))
        parts = [f'{len(finished)}/{len(files)} done', counts]
        if queued:
            parts.append(r.paint(f'{queued} queued', GRAY))
        if hidden:
            parts.append(r.paint(f'{hidden} more running', GRAY))
        parts.append(r.paint(clock_time(now), GRAY))
        bar = r.bar(fraction, max(10, min(30, width // 6)))
        return f'  {bar} {r.paint(f"{fraction * 100:3.0f}%", BOLD)}   ' + f' {r.paint(r.symbols["dot"], GRAY)} '.join(parts)

    def panel(self, now, columns, rows, frame):
        """The live rows, never taller than the window and never wider than one line each."""
        width = max(20, columns - 1)
        active = [i for i, file in enumerate(self.files) if file.start <= now < file.end]
        room = max(1, rows - 2)
        shown = active[:max(0, room - 2)] if len(active) + 2 > room else active
        lines = [self.worker_row(i, now, width, frame) for i in shown]
        lines.append(self.totals_row(now, width, len(active) - len(shown)))
        if len(lines) + 1 < room:
            label = f' {plural(len(active), "file")} in progress · up to {self.jobs} at a time '
            if not self.unicode:
                label = label.replace('·', '|')
            rule = self.r.symbols['rule']
            lines[:0] = ['', self.r.paint('  ' + rule * 2 + label + rule * max(0, min(width, 110) - len(label) - 4), GRAY)]
        return [fit(line, width) for line in lines]

    def status_line(self, now):
        """Occasional plain progress for redirected output."""
        running = []
        for index, file in enumerate(self.files):
            if file.start <= now < file.end:
                stage = next(((s, f) for s, status, f in timeline(file, now) if status == 'running'), None)
                where = f'{stage[0].label} {stage[1] * 100:.0f}%' if stage and stage[0].media else (
                    stage[0].label if stage else 'finishing')
                running.append(f'{self.tag(index)} {file.name} ({where})')
        done = sum(file.end <= now for file in self.files)
        return self.r.paint(f'  {self.r.symbols["note"]} {clock_time(now)}  {done}/{len(self.files)} done; running: ' + ', '.join(running), GRAY)

    # Summary -------------------------------------------------------------------------------

    def summary(self, now, stopped):
        r, files = self.r, self.files
        rule = r.symbols['rule']
        width = max(2, len(str(len(files))))
        lines = ['', r.paint(rule * 30 + ' Summary ' + rule * 30, GRAY),
                 r.paint(f'  {"#":>{width}}  {"Result":<6}  {"Err":>3}  {"Warn":>4}  {"Time":>8}  File', GRAY)]
        for index, file in enumerate(files, 1):
            if file.end <= now:
                _, word, colour = file.verdict
                errors = r.paint(f'{len(file.errors):>3}', RED, BOLD) if file.errors else f'{0:>3}'
                warned = r.paint(f'{len(file.warnings):>4}', YELLOW) if file.warnings else f'{0:>4}'
                lines.append(f'  {index:>{width}}  {r.paint(f"{word:<6}", colour, BOLD)}  {errors}  {warned}  '
                             f'{clock_time(file.duration):>8}  {file.name}')
            else:
                word = 'STOP' if file.start <= now else 'SKIP'
                lines.append(r.paint(f'  {index:>{width}}  {word:<6}  {"-":>3}  {"-":>4}  {"-":>8}  {file.name}', GRAY))
        finished = [file for file in files if file.end <= now]
        failed = [file for file in finished if file.errors]
        passed = len(finished) - len(failed)
        warned = sum(bool(file.warnings) for file in finished if not file.errors)
        totals = [(f'{r.symbols["done"]} {passed} passed', passed, GREEN),
                  (f'{r.symbols["warn"]} {warned} with warnings', warned, YELLOW),
                  (f'{r.symbols["error"]} {len(failed)} failed', len(failed), RED)]
        work = sum(file.duration for file in finished)
        lines += ['', '  ' + '   '.join(r.paint(text, colour, BOLD) if count else r.paint(text, GRAY)
                                        for text, count, colour in totals) +
                  '   ' + r.paint(f'{plural(len(finished), "video")} in {clock_time(now)}', GRAY) +
                  r.paint(f'  ({clock_time(work)} of checks, {self.jobs} at a time)', GRAY)]
        if stopped:
            interrupted = sum(file.start <= now < file.end for file in files)
            unstarted = sum(file.start > now for file in files)
            lines.append('  ' + r.paint(f'{r.symbols["warn"]} Stopped', YELLOW, BOLD) + r.paint(
                f'  {interrupted} interrupted, {unstarted} not started; neither is counted above.', GRAY))
        if failed:
            lines += ['', f'  {r.paint("Failed", RED, BOLD)}']
            lines += [f'    {r.paint(r.symbols["error"], RED)} {file.name}' for file in failed]
            lines.append(f'  {r.paint("Report", GRAY)}  (mock preview: corrupted_files.txt not written)')
        return lines


# --- Terminal output ---------------------------------------------------------------------------

class LiveRegion:
    """Keeps a few rows at the bottom up to date, below ordinary scrollback, one write per frame.

    The cursor rests at the end of the panel's last row. Each frame moves back to the panel's
    first row, prints any new permanent lines (which push the panel down), redraws the panel and
    clears whatever the previous, possibly taller, panel left behind.
    """
    def __init__(self, output):
        self.output = output
        self.panel = []
        self.widths = []

    def __enter__(self):
        self.output.write('\033[?25l')
        self.output.flush()
        return self

    def rewind(self, columns):
        if not self.widths:
            return ''
        # Rows may have rewrapped if the window narrowed since the last frame.
        rows = sum(max(1, math.ceil(width / max(1, columns))) for width in self.widths)
        return '\r' + (f'\033[{rows - 1}A' if rows > 1 else '')

    def draw(self, history, panel, columns):
        if not history and panel == self.panel:
            return
        parts = [self.rewind(columns)]
        parts += [line + '\033[0m\033[K\n' for line in history]
        parts.append('\n'.join(line + '\033[0m\033[K' for line in panel))
        parts.append('\033[J')
        self.output.write(''.join(parts))
        self.output.flush()
        self.panel = panel
        self.widths = [visible_width(line) for line in panel]

    def __exit__(self, *exception):
        columns = shutil.get_terminal_size((110, 30)).columns
        self.output.write(self.rewind(columns) + '\033[J\033[0m\033[?25h')
        self.output.flush()
        self.panel, self.widths = [], []


def completion_order(files):
    return sorted(range(len(files)), key=lambda i: (files[i].end, i))


def run_live(view, total, speed):
    r, files = view.r, view.files
    order, printed, now, stopped = completion_order(files), 0, 0.0, False
    started = time.monotonic()
    try:
        with LiveRegion(r.output) as region:
            while True:
                elapsed = time.monotonic() - started
                now = min(total, elapsed * speed)
                history = []
                while printed < len(order) and files[order[printed]].end <= now:
                    history += view.block(order[printed])
                    printed += 1
                columns, rows = shutil.get_terminal_size((110, 30))
                panel = view.panel(now, columns, rows, int(elapsed * FPS)) if now < total else []
                region.draw(history, panel, columns)
                if now >= total:
                    break
                time.sleep(1 / FPS)
    except KeyboardInterrupt:
        stopped = True
    for line in view.summary(now, stopped):
        r.message(line)
    return 130 if stopped else 0


def run_log(view, total):
    r, files = view.r, view.files
    tick = LOG_STATUS_EVERY
    for index in completion_order(files):
        while tick < files[index].end:
            r.message()
            r.message(view.status_line(tick))
            tick += LOG_STATUS_EVERY
        for line in view.block(index):
            r.message(line)
    for line in view.summary(total, False):
        r.message(line)
    return 0


def run_snapshot(view, at, columns, rows):
    r, files = view.r, view.files
    for index in completion_order(files):
        if files[index].end <= at:
            for line in view.block(index):
                r.message(line)
    if at < max(file.end for file in files):
        for line in view.panel(at, columns, rows, int(at * FPS)):
            r.message(line)
    else:
        for line in view.summary(at, False):
            r.message(line)
    return 0


def number(minimum, strict):
    def parse(value):
        result = float(value)
        if not math.isfinite(result) or result < minimum or (strict and result == minimum):
            raise argparse.ArgumentTypeError(f'must be a finite number {">" if strict else ">="} {minimum:g}')
        return result
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--jobs', type=int, choices=range(1, 7), default=2, help='Files checked at once (default: 2)')
    parser.add_argument('-if', '--ignore-ffmpeg', action='store_true', help='Skip the mock FFmpeg stage')
    parser.add_argument('--speed', type=number(0, True), default=2, help='Simulation speed (default: 2)')
    parser.add_argument('--no-anim', action='store_true', help='Print what redirected output would look like')
    parser.add_argument('--snapshot', type=number(0, False), metavar='SECONDS',
                        help='Print the screen as it would look at one simulated moment')
    args = parser.parse_args(argv)

    r = Reporter()
    files = mock_files(args.ignore_ffmpeg)
    schedule(files, args.jobs)
    view = View(r, files, args.jobs)
    total = max(file.end for file in files)
    columns, rows = shutil.get_terminal_size((120, 30))
    live = r.ansi and not args.no_anim and args.snapshot is None

    print_banner(r)
    mode = (f'{args.speed:g}x speed' if live else f'snapshot at {clock_time(args.snapshot)}'
            if args.snapshot is not None else 'redirected-log rendering')
    settings = [('Checks', 'FFprobe scans' + ('' if args.ignore_ffmpeg else ' + FFmpeg decode')),
                ('Timing', 'verified timestamp rounding warns'),
                ('Workers', f'{view.jobs} files at a time'),
                ('Preview', f'{mode} {r.symbols["dot"]} fictional data, nothing is scanned')]
    for key, value in settings:
        r.message(f'  {r.paint(key.ljust(11), GRAY)}{value}')
    r.message()
    r.message(f'  {r.paint(plural(len(files), "video"), BOLD)} found')
    for index, file in enumerate(files, 1):
        r.message(f'  {r.paint(str(index).rjust(len(str(len(files))) + 2), GRAY)}  {file.name}')

    if args.snapshot is not None:
        return run_snapshot(view, min(args.snapshot, total), columns, rows)
    if not live:
        return run_log(view, total)
    return run_live(view, total, args.speed)


if __name__ == '__main__':
    sys.exit(main())
