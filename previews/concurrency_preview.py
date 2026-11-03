"""Mock concurrent-file UI; no media is opened and no FFmpeg tools are run.

    python previews/concurrency_preview.py
    python previews/concurrency_preview.py -if
    python previews/concurrency_preview.py --jobs 3 --speed 1
    python previews/concurrency_preview.py --no-anim

Interactive ANSI terminals get an in-place dashboard below an ordered transcript.
Other outputs get a few labelled snapshots followed by the same ordered results.
All timings, progress, warnings and errors are fictional.
"""
import argparse
from dataclasses import dataclass, field
import math
import pathlib
import shutil
import sys
import time

# Reuse the actual script's formatting without running its verification code.
sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from VerifyVideo import BOLD, CYAN, GRAY, GREEN, RED, YELLOW, Reporter, clock_time, fit, print_banner


@dataclass
class Stage:
    tool: str
    label: str
    seconds: float
    details: tuple = ()
    media: float = 0
    frames: int = 0
    state: str = 'done'


@dataclass
class File:
    name: str
    stages: list
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    start: float = 0

    @property
    def duration(self):
        return sum(stage.seconds for stage in self.stages)

    @property
    def end(self):
        return self.start + self.duration

    @property
    def verdict(self):
        return ('FAILED', 'FAIL', RED) if self.errors else (
            ('PASSED WITH WARNINGS', 'WARN', YELLOW) if self.warnings else ('PASSED', 'PASS', GREEN))


def mock_files(ignore_ffmpeg):
    files = []
    # File 1 is deliberately slow: later files finish and workers keep going.
    for index, (video_seconds, decode_seconds, media) in enumerate(
            [(19, 9, 600), (4, 3, 60), (6, 4, 120), (3, 2, 90), (4, 2, 180)], 1):
        frames = round(media * 60)
        warnings = ['Independent FFmpeg decode check skipped (--ignore-ffmpeg).'] if ignore_ffmpeg else []
        errors = []
        if index == 3:
            warnings.append('A/V duration mismatch (video 0, audio 1): 0.024000s > 0.016667s tolerance.')
        if index == 4:
            errors.append('FFprobe stream 0: error while decoding a video frame (mock diagnostic).')
            if not ignore_ffmpeg:
                errors.append('FFmpeg decode check exited with code 1 (mock diagnostic).')
        stages = [
            Stage('FFprobe', 'metadata', 0.15, ('3 A/V streams',)),
            Stage('FFprobe', 'video stream 0', video_seconds,
                  (f'{frames:,} frames', clock_time(media)), media, frames,
                  'error' if index == 4 else 'done'),
            Stage('FFprobe', 'audio stream 1', 0.35,
                  (f'{round(media * 48000 / 1024):,} frames', clock_time(media)), media),
            Stage('FFprobe', 'audio stream 2', 0.35,
                  (f'{round(media * 48000 / 1024):,} frames', clock_time(media)), media),
            Stage('FFmpeg', 'decode all A/V streams', 0 if ignore_ffmpeg else decode_seconds,
                  ('skipped (--ignore-ffmpeg)',) if ignore_ffmpeg else (), media, frames,
                  'skip' if ignore_ffmpeg else 'error' if index == 4 else 'done'),
            Stage('Check', 'A/V durations & offsets', 0.1,
                  ('see warnings below',) if index == 3 else (), state='warn' if index == 3 else 'done'),
        ]
        files.append(File(f'Episode {index:02}.mkv', stages, warnings, errors))
    return files


def schedule(files, jobs):
    available = [0.0] * min(jobs, len(files))
    for file in files:
        worker = min(range(len(available)), key=available.__getitem__)
        file.start = available[worker]
        available[worker] = file.end


def stage_states(file, now):
    offset = file.start
    for stage in file.stages:
        end = offset + stage.seconds
        state = 'queued' if now < offset else 'done' if now >= end else 'running'
        fraction = min(0.999, max(0, (now - offset) / stage.seconds)) if stage.seconds else 1
        yield stage, state, fraction
        offset = end


class Preview:
    def __init__(self, reporter, files, jobs):
        self.reporter = reporter
        self.files = files
        self.jobs = min(jobs, len(files))
        self.committed = 0

    def heading(self, index):
        r = self.reporter
        return r.paint(f'[{index + 1}/{len(self.files)}]', CYAN, BOLD) + ' ' + r.paint(self.files[index].name, BOLD)

    def stage_line(self, stage, state, fraction, now):
        r = self.reporter
        if state == 'done':
            return r.stage(stage.state, stage.tool, stage.label, stage.details,
                           None if stage.state == 'skip' else stage.seconds)
        prefix = f'    {r.paint(stage.tool.ljust(9), GRAY)}{stage.label.ljust(25)}'
        if state == 'queued':
            return prefix + r.paint('waiting' if stage.tool == 'Check' else 'queued', GRAY)
        spinner = r.symbols['spinner'][int(now * 8) % len(r.symbols['spinner'])]
        prefix = f'  {r.paint(spinner, CYAN)} ' + prefix[4:]
        if stage.media:
            details = f'{clock_time(stage.media * fraction)} / {clock_time(stage.media)}'
            if stage.frames:
                details += f' | {int(stage.frames * fraction):,} frames'
            return prefix + f'[{r.bar(fraction, 14)}] {fraction * 100:5.1f}%  ' + r.paint(details, GRAY)
        return prefix + r.paint('checking...', GRAY)

    def verdict_line(self, file, waiting=False):
        r = self.reporter
        word, _, colour = file.verdict
        details = r.join([f'{len(file.errors)} errors', f'{len(file.warnings)} warnings', clock_time(file.duration)])
        if waiting:
            details += ' | awaiting transcript position'
        return f'  {r.paint(r.symbols["bullet"], colour)} {r.paint(word, colour, BOLD)}  {r.paint(details, GRAY)}'

    def transcript(self, index):
        r, file = self.reporter, self.files[index]
        lines = ['', self.heading(index)]
        lines.extend(self.stage_line(stage, 'done', 1, file.end) for stage in file.stages)
        messages = [('warn', message) for message in file.warnings] + [('error', message) for message in file.errors]
        if messages:
            lines.append('  ' + r.paint(r.symbols['stem'], GRAY))
            for position, (state, message) in enumerate(messages):
                branch = r.symbols['last' if position == len(messages) - 1 else 'branch']
                colour = RED if state == 'error' else YELLOW
                lines.append(f'  {r.paint(branch, GRAY)} {r.paint(r.symbols[state], colour, BOLD)} {message}')
        lines.append(self.verdict_line(file))
        return lines

    def ready_transcript(self, now):
        lines = []
        while self.committed < len(self.files) and self.files[self.committed].end <= now:
            lines.extend(self.transcript(self.committed))
            self.committed += 1
        return lines

    def dashboard(self, now, height):
        r = self.reporter
        done = sum(file.end <= now for file in self.files)
        active = [i for i, file in enumerate(self.files) if file.start <= now < file.end]
        waiting = [i for i, file in enumerate(self.files) if file.end <= now and i >= self.committed]
        queued = [i for i, file in enumerate(self.files) if file.start > now]
        lines = [r.paint(r.symbols['rule'] * 18 + ' Live progress ' + r.symbols['rule'] * 18, GRAY),
                 f'  {done}/{len(self.files)} finished | {len(active)} active | {len(queued)} queued'
                 f' | elapsed {clock_time(now)}']

        def blocks(compact=False):
            output = []
            for index in sorted(active + waiting):
                file = self.files[index]
                if index in waiting:
                    _, badge, colour = file.verdict
                    output.append(f'  [{index + 1}/{len(self.files)}] {file.name}  ' + r.paint(badge, colour, BOLD)
                                  + r.paint(' | completed; awaiting transcript position', GRAY))
                    continue
                output.extend(['', self.heading(index)])
                states = list(stage_states(file, now))
                if compact:
                    output.append(r.paint(f'    {sum(state == "done" for _, state, _ in states)}'
                                          f'/{len(states)} stages finished', GRAY))
                    states = [entry for entry in states if entry[1] == 'running']
                output.extend(self.stage_line(stage, state, fraction, now) for stage, state, fraction in states)
            return output

        body = blocks()
        if len(lines) + len(body) + 2 > height:
            body = blocks(compact=True)
        if len(lines) + len(body) + 2 > height:
            # Tiny terminals: retain an ordered, single-line view of each active file.
            body = []
            for index in active:
                current = next((stage for stage, state, _ in stage_states(self.files[index], now)
                                if state == 'running'), None)
                label = f'{current.tool} {current.label}' if current else 'finishing'
                body.append(f'  [{index + 1}] {self.files[index].name} | {label}')
            if waiting:
                body.append(f'  {len(waiting)} completed files awaiting transcript position')
        lines.extend(body)
        if queued:
            lines.extend(['', r.paint('  Queued: ' + ', '.join(f'[{i + 1}] {self.files[i].name}' for i in queued), GRAY)])
        if len(lines) > height:
            lines = lines[:height - 1] + [r.paint('  More activity hidden; enlarge terminal for stage details.', GRAY)]
        return lines

    def summary(self, elapsed):
        r = self.reporter
        lines = ['', r.paint(r.symbols['rule'] * 28 + ' Summary ' + r.symbols['rule'] * 28, GRAY), '',
                 r.paint(f'   #  {"Result":<6}  {"Err":>3}  {"Warn":>4}  {"Time":>8}  File', GRAY)]
        for index, file in enumerate(self.files, 1):
            _, badge, colour = file.verdict
            lines.append(f'  {index:>2}  {r.paint(badge.ljust(6), colour, BOLD)}  {len(file.errors):>3}'
                         f'  {len(file.warnings):>4}  {clock_time(file.duration):>8}  {file.name}')
        passed = sum(not file.errors for file in self.files)
        warned = sum(bool(file.warnings) and not file.errors for file in self.files)
        failed = sum(bool(file.errors) for file in self.files)
        lines.extend(['', f'  {passed} passed | {warned} with warnings | {failed} failed'
                      f' | {len(self.files)} videos in {clock_time(elapsed)}',
                      r.paint('  Mock preview only. No media scanned and no failure report written.', GRAY)])
        return lines


class LivePanel:
    def __init__(self, reporter):
        self.reporter = reporter
        self.height = 0

    def clear(self):
        if self.height:
            self.reporter.output.write(f'\033[{self.height}F\033[J')
            self.height = 0

    def draw(self, permanent, panel):
        self.clear()
        # Every line stays narrower than the terminal to avoid automatic wrapping.
        width = max(1, shutil.get_terminal_size((110, 35)).columns - 1)
        for line in permanent:
            self.reporter.message(line)
        for line in panel:
            self.reporter.message(fit(line, width))
        self.height = len(panel)


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite number greater than zero')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--jobs', type=int, choices=range(1, 6), default=2, help='Mock file workers, 1-5 (default: 2)')
    parser.add_argument('-if', '--ignore-ffmpeg', action='store_true', help='Preview skipping the FFmpeg pass')
    parser.add_argument('--speed', type=positive_float, default=2, help='Animation speed multiplier (default: 2)')
    parser.add_argument('--no-anim', action='store_true', help='Print representative snapshots without waiting')
    args = parser.parse_args(argv)
    reporter = Reporter()
    files = mock_files(args.ignore_ffmpeg)
    schedule(files, args.jobs)
    preview = Preview(reporter, files, args.jobs)
    print_banner(reporter)
    reporter.message(reporter.paint('  CONCURRENCY PREVIEW | fictional data and timings', CYAN, BOLD))
    reporter.message('  Checks     FFprobe scans' + ('' if args.ignore_ffmpeg else ' + FFmpeg decode'))
    reporter.message(f'  Workers    {preview.jobs} files; sequential checks within each file')
    reporter.message(f'  Animation  {args.speed:g}x speed; displayed times are simulated')
    reporter.message(f'\n  {len(files)} videos found')
    for index, file in enumerate(files, 1):
        reporter.message(f'    {index}  {file.name}')
    reporter.message()
    total = max(file.end for file in files)
    animate = reporter.ansi and not args.no_anim
    if not animate:
        # Catch an out-of-order completion and the long-running first file.
        moments = sorted({0.0, min(file.end for file in files) + 0.01, max(0, files[0].end - 0.5)})
        for now in moments:
            for line in preview.ready_transcript(now):
                reporter.message(line)
            reporter.message(f'\n  --- Snapshot at {clock_time(now)} ---')
            for line in preview.dashboard(now, 40):
                reporter.message(line)
        for line in preview.ready_transcript(total) + preview.summary(total):
            reporter.message(line)
        return 0

    panel = LivePanel(reporter)
    started = time.monotonic()
    reporter.output.write('\033[?25l')
    try:
        while True:
            now = min(total, (time.monotonic() - started) * args.speed)
            height = max(3, shutil.get_terminal_size((110, 35)).lines - 3)
            panel.draw(preview.ready_transcript(now), [] if now >= total else preview.dashboard(now, height))
            if now >= total:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        panel.clear()
        reporter.message('\n  Preview stopped.')
        return 130
    finally:
        reporter.output.write('\033[?25h')
        reporter.output.flush()
    for line in preview.summary(total):
        reporter.message(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
