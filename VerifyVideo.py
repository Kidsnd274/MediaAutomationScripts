import argparse
from array import array
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import islice
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


video_file_extensions = ['.mkv', '.mp4', '.avi', '.webm']
current_directory = pathlib.Path(__file__).resolve().parent

BOLD, RED, GREEN, YELLOW, CYAN, GRAY = '1', '31', '32', '33', '36', '90'
ANSI_CODE = re.compile(r'(\033\[[0-9;]*[A-Za-z])')
STATE_COLOURS = {'done': GREEN, 'error': RED, 'warn': YELLOW, 'skip': GRAY, 'note': CYAN}
VERDICTS = {'OK': ('PASSED', 'PASS', GREEN), 'OK WITH WARNINGS': ('PASSED WITH WARNINGS', 'WARN', YELLOW),
            'FAILED': ('FAILED', 'FAIL', RED)}
UNICODE_SYMBOLS = {'done': '✓', 'error': '✗', 'warn': '▲', 'skip': '–', 'note': '→', 'bullet': '●',
                   'branch': '├─', 'last': '└─', 'stem': '│', 'bar': '━', 'track': '━', 'dot': '·',
                   'rule': '─', 'box': '╭╮╰╯─│', 'spinner': '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'}
ASCII_SYMBOLS = {'done': '+', 'error': 'x', 'warn': '!', 'skip': '-', 'note': '>', 'bullet': '*',
                 'branch': '|-', 'last': '`-', 'stem': '|', 'bar': '#', 'track': '-', 'dot': '|',
                 'rule': '-', 'box': '++++-|', 'spinner': '|/-\\'}
TOOL_WIDTH, LABEL_WIDTH, DETAIL_WIDTH = 9, 25, 28


def clock_time(seconds):
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{int(hours)}:{int(minutes):02}:{seconds:05.2f}' if hours else f'{int(minutes)}:{seconds:05.2f}'


def plural(count, word):
    return f'{count:,} {word}{"" if count == 1 else "s"}'


def is_terminal(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def enable_ansi(stream):
    """Enable escape sequences in a Windows console using only the standard library."""
    if os.name != 'nt':
        return os.environ.get('TERM') != 'dumb'
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(mode.value & 0x0004 or kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (ImportError, AttributeError, OSError, ValueError):
        return False


def supports_unicode(stream):
    try:
        ''.join(UNICODE_SYMBOLS.values()).encode(getattr(stream, 'encoding', None) or 'utf-8')
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def visible_width(text):
    return len(ANSI_CODE.sub('', text))


def fit(text, width):
    """Truncate to a visible width without cutting escape sequences in half."""
    parts, visible = [], 0
    for piece in ANSI_CODE.split(text):
        if ANSI_CODE.fullmatch(piece):
            parts.append(piece)
        else:
            parts.append(piece[:max(0, width - visible)])
            visible += len(piece)
    return ''.join(parts)


class Reporter:
    """Writes status output, using colour and Unicode only where the stream supports them."""
    def __init__(self, output=None):
        self.output = output if output is not None else sys.stdout
        self.interactive = is_terminal(self.output)
        self.ansi = self.interactive and enable_ansi(self.output)
        self.colour = self.ansi and not os.environ.get('NO_COLOR')
        unicode = supports_unicode(self.output)
        self.symbols = dict(UNICODE_SYMBOLS if unicode else ASCII_SYMBOLS)
        if unicode and not self.colour:
            self.symbols['track'] = '─'  # Keep the progress bar readable without colour.

    def message(self, message=''):
        print(message, file=self.output, flush=True)

    def paint(self, text, *codes):
        text = str(text)
        return f'\033[{";".join(codes)}m{text}\033[0m' if self.colour and codes and text else text

    def join(self, parts):
        return f' {self.symbols["dot"]} '.join(parts)

    def bar(self, fraction, width=20):
        filled = int(fraction * width)
        return (self.paint(self.symbols['bar'] * filled, CYAN) +
                self.paint(self.symbols['track'] * (width - filled), GRAY))

    def stage(self, state, tool, label, details=(), elapsed=None):
        detail = self.join(details).ljust(DETAIL_WIDTH)
        line = (f'  {self.paint(self.symbols[state], STATE_COLOURS[state], BOLD)} '
                f'{self.paint(tool.ljust(TOOL_WIDTH), GRAY)}{label.ljust(LABEL_WIDTH)}'
                f'{self.paint(detail, RED) if state == "error" else detail}')
        if elapsed is not None:
            line += self.paint(clock_time(elapsed).rjust(8), GRAY)
        return line.rstrip()


class Progress:
    """A live status line per stage in a terminal; occasional plain updates in redirected logs."""
    def __init__(self, reporter, tool, label, total=None, severity='error'):
        self.reporter = reporter
        self.tool = tool
        self.label = label
        self.total = total if total is not None and math.isfinite(total) and total > 0 else None
        self.severity = severity  # State used when the stage raises.
        self.interactive = bool(reporter and reporter.interactive)
        self.started = time.monotonic()
        self.updated = self.started
        self.width = 0
        self.frame = 0
        self.detail = []
        self.state = None  # None (done), 'warn' or 'error'.

    def __enter__(self):
        if self.reporter:
            self.write(self.running_line() if self.interactive else
                       f'    {self.tool.ljust(TOOL_WIDTH)}{self.label} ...')
        return self

    def write(self, text, final=False):
        reporter = self.reporter
        if reporter is None:
            return
        if not self.interactive:
            reporter.message(text)
            return
        text = fit(text, max(20, shutil.get_terminal_size((100, 20)).columns - 1))
        if reporter.ansi:
            text += '\033[K'
        else:
            width = visible_width(text)
            text += ' ' * max(0, self.width - width)
            self.width = width
        print('\r' + text, end='\n' if final else '', file=reporter.output, flush=True)

    def running_line(self, fraction=None, details=()):
        reporter = self.reporter
        spinner = reporter.symbols['spinner']
        line = (f'  {reporter.paint(spinner[self.frame % len(spinner)], CYAN)} '
                f'{reporter.paint(self.tool.ljust(TOOL_WIDTH), GRAY)}{self.label.ljust(LABEL_WIDTH)}')
        self.frame += 1
        if fraction is not None:
            line += f'{reporter.bar(fraction)} {fraction * 100:5.1f}%  '
        return line + reporter.paint(reporter.join(details), GRAY)

    def update(self, position=None, count=None):
        if self.reporter is None:
            return
        now = time.monotonic()
        if now - self.updated < (0.25 if self.interactive else 30):
            return
        self.updated = now
        fraction = None
        details = []
        if position is not None and math.isfinite(position):
            position = max(0, position)
            if self.total:
                fraction = min(0.999, position / self.total)
                details.append(f'{clock_time(position)} / {clock_time(self.total)}')
            else:
                details.append(f'{clock_time(position)} processed')
        if count is not None:
            details.append(f'{count:,} frames')
        details.append(f'elapsed {clock_time(now - self.started)}')
        if self.interactive:
            self.write(self.running_line(fraction, details))
        else:
            percent = [f'{fraction * 100:5.1f}%'] if fraction is not None else []
            self.write(f'    {self.tool.ljust(TOOL_WIDTH)}{self.label}: ' + ' | '.join(percent + details))

    def __exit__(self, exc_type, exc_value, traceback):
        if self.reporter is None:
            return
        state = self.severity if exc_type else self.state or 'done'
        details = self.detail or ([] if state == 'done' else
                                  ['see errors below' if state == 'error' else 'see warnings below'])
        self.write(self.reporter.stage(state, self.tool, self.label, details,
                                       time.monotonic() - self.started), final=True)


@dataclass
class Result:
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def status(self):
        return 'FAILED' if self.errors else 'OK WITH WARNINGS' if self.warnings else 'OK'


@dataclass
class Stream:
    index: int
    kind: str
    time_base: Fraction
    sample_rate: float = 0
    count: int = 0
    timestamps: array = field(default_factory=lambda: array('d'))
    start: float = math.inf
    last: float = -math.inf
    end: float = -math.inf
    last_duration: float = 0
    interval: float = 0
    missing_timestamps: int = 0
    missing_durations: int = 0
    frame_rate: Fraction = 0
    expected_duration: float = None

    def add_frame(self, frame):
        self.count += 1
        tick = number(frame.get('best_effort_timestamp'))
        if tick is None:
            tick = number(frame.get('pts'))
        timestamp = tick * self.time_base if tick is not None and self.time_base else None
        if self.kind == 'video':
            self.timestamps.append(timestamp if timestamp is not None else math.nan)
        if timestamp is None:
            self.missing_timestamps += 1
            return
        duration = number(frame.get('duration')) or number(frame.get('pkt_duration'))
        duration = duration * self.time_base if duration and duration > 0 else 0
        if self.kind == 'audio':
            samples = number(frame.get('nb_samples'))
            duration = samples / self.sample_rate if samples and self.sample_rate else 0
        if not duration:
            self.missing_durations += 1
        self.start = min(self.start, timestamp)
        if timestamp >= self.last:
            self.last = timestamp
            self.last_duration = duration
        self.end = max(self.end, timestamp + duration)

    def finish(self, warnings):
        if self.kind == 'video':
            intervals = [b - a for a, b in zip(self.timestamps, islice(self.timestamps, 1, None))
                         if math.isfinite(a) and math.isfinite(b) and b > a]
            self.interval = statistics.median(intervals) if intervals else 0
            if self.count and not self.last_duration:
                if self.interval and math.isfinite(self.last):
                    self.end = max(self.end, self.last + self.interval)
                    warnings.append(f'Video stream {self.index}: final-frame duration estimated from timestamps.')
                else:
                    warnings.append(f'Video stream {self.index}: final-frame duration unavailable.')
        elif self.missing_durations:
            warnings.append(f'Audio stream {self.index}: sample timing incomplete; duration check may be incomplete.')
        if self.missing_timestamps:
            warnings.append(f'{self.kind.capitalize()} stream {self.index}: timestamps missing for '
                            f'{self.missing_timestamps} frame(s); timing check incomplete.')

    @property
    def duration(self):
        return self.end - self.start if math.isfinite(self.start) and math.isfinite(self.end) else None


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def time_base(value):
    try:
        result = Fraction(value)
        return result if result > 0 else 0
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return 0


def executable(name):
    for candidate in (current_directory / (name + '.exe'),
                      current_directory / 'ffmpeg' / (name + '.exe'),
                      current_directory / name):
        if candidate.is_file():
            return str(candidate)
    return name + '.exe' if sys.platform == 'win32' else name


def diagnostic_messages(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[:20] + ([f'... {len(lines) - 20} more diagnostic lines'] if len(lines) > 20 else [])


def probe_metadata(file, ffprobe):
    process = subprocess.run([ffprobe, '-v', 'error', '-show_format', '-show_streams',
                              '-of', 'json', str(file)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8', errors='replace')
    errors = diagnostic_messages(process.stderr)
    if process.returncode:
        errors.append(f'FFprobe metadata check exited with code {process.returncode}.')
    try:
        data = json.loads(process.stdout)
    except ValueError as error:
        raise ValueError(f'Invalid FFprobe JSON: {error}. ' + ' '.join(errors)) from error
    if not isinstance(data, dict) or not isinstance(data.get('streams'), list):
        raise ValueError('FFprobe did not return a stream list. ' + ' '.join(errors))
    streams = []
    for item in data['streams']:
        if not isinstance(item, dict):
            raise ValueError('FFprobe returned an invalid stream entry.')
        kind = item.get('codec_type')
        if kind not in ('video', 'audio') or item.get('disposition', {}).get('attached_pic'):
            continue
        streams.append(Stream(int(item['index']), kind, time_base(item.get('time_base')),
                              number(item.get('sample_rate')) or 0,
                              frame_rate=time_base(item.get('r_frame_rate')),
                              expected_duration=number(item.get('duration'))))
    return streams, number(data.get('format', {}).get('duration')), errors


def run_streaming(command, consume):
    # A temporary stderr file avoids pipe deadlocks while stdout is read incrementally.
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8', errors='replace') as diagnostics:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=diagnostics,
                              text=True, encoding='utf-8', errors='replace') as process:
            try:
                for line in process.stdout:
                    if line.strip():
                        consume(line.strip())
                process.wait()
            except BaseException:
                process.kill()
                process.wait()
                raise
        diagnostics.seek(0)
        # Keep the report small even for a badly damaged file.
        errors = []
        extra = 0
        for line in diagnostics:
            if line.strip():
                if len(errors) < 20:
                    errors.append(line.strip())
                else:
                    extra += 1
        if extra:
            errors.append(f'... {extra} more diagnostic lines')
    return process.returncode, errors


def scan_stream(file, stream, ffprobe, progress=None):
    # Selecting one A/V stream prevents subtitle decoding from affecting this check.
    command = [ffprobe, '-v', 'error', '-select_streams', str(stream.index),
               '-threads', '8', '-show_frames', '-show_entries',
               'frame=stream_index,best_effort_timestamp,pts,duration,pkt_duration,nb_samples:frame_side_data=',
               '-of', 'compact=p=0:nk=0', str(file)]

    def consume(line):
        frame = dict(part.split('=', 1) for part in line.split('|') if '=' in part)
        if int(frame['stream_index']) != stream.index:
            raise ValueError('FFprobe returned an unexpected frame stream index.')
        stream.add_frame(frame)
        if progress:
            position = stream.last - stream.start if math.isfinite(stream.start) else None
            progress.update(position, stream.count)

    returncode, errors = run_streaming(command, consume)
    if returncode:
        errors.append(f'FFprobe stream {stream.index} exited with code {returncode}.')
    return errors


def timestamp_checks(streams, result, source=False, strict_timing=False):
    for stream in streams:
        if stream.kind != 'video':
            continue
        duplicates = backwards = 0
        first_duplicate = first_backwards = None
        for frame, (a, b) in enumerate(zip(stream.timestamps, islice(stream.timestamps, 1, None)), 2):
            if not math.isfinite(a) or not math.isfinite(b):
                continue
            if b == a:
                duplicates += 1
                if first_duplicate is None:
                    first_duplicate = frame, b
            elif b < a:
                backwards += 1
                if first_backwards is None:
                    first_backwards = frame, b
        prefix = 'Original: ' if source else ''
        for count, first, description, fatal in (
                (duplicates, first_duplicate, 'Duplicate presentation timestamps', strict_timing),
                (backwards, first_backwards, 'Backwards presentation timestamps', True)):
            if not count:
                continue
            frame, timestamp = first
            message = (f'{prefix}{description} in video stream {stream.index}: {count} occurrence(s), '
                       f'first at frame {frame} ({timestamp:.9f}s).')
            issues = result.errors if fatal and not source else result.warnings
            if message not in issues:
                issues.append(message)


def inspect_video(file, ffprobe, result, source=False, strict_timing=False, reporter=None):
    issues = result.warnings if source else result.errors
    prefix = 'Original: ' if source else ''
    severity = 'warn' if source else 'error'
    label = 'original ' if source else ''
    try:
        with Progress(reporter, 'FFprobe', label + 'metadata', severity=severity) as progress:
            streams, duration, errors = probe_metadata(file, ffprobe)
            progress.detail = [plural(len(streams), 'A/V stream')]
            if errors:
                progress.state = severity
        incomplete = bool(errors)
        issues.extend(prefix + message for message in errors)
        videos = [stream for stream in streams if stream.kind == 'video']
        if not videos:
            issues.append(prefix + 'No motion-video streams found.')
            if source:
                result.warnings.append('Original could not be checked; source comparison is incomplete.')
            return [], duration
        for stream in streams:
            try:
                with Progress(reporter, 'FFprobe', f'{label}{stream.kind} stream {stream.index}',
                              stream.expected_duration or duration, severity=severity) as progress:
                    errors = scan_stream(file, stream, ffprobe, progress) if reporter else scan_stream(file, stream, ffprobe)
                    progress.detail = [f'{stream.count:,} frames']
                    if stream.duration is not None:
                        progress.detail.append(clock_time(stream.duration))
                    if errors or not stream.count:
                        progress.state = severity
                incomplete |= bool(errors)
                issues.extend(prefix + message for message in errors)
                if not stream.count:
                    incomplete = True
                    issues.append(prefix + f'No frames decoded in {stream.kind} stream {stream.index}.')
                warnings = []
                stream.finish(warnings)
                result.warnings.extend(prefix + message for message in warnings)
            except (OSError, ValueError, KeyError, TypeError) as error:
                incomplete = True
                issues.append(prefix + f'Could not scan stream {stream.index}: {error}')
        timestamp_checks(streams, result, source=source, strict_timing=strict_timing)
        if source and incomplete:
            result.warnings.append('Original has probe/decoder errors; source comparison is incomplete.')
        return streams, duration
    except (OSError, ValueError, KeyError, TypeError) as error:
        issues.append(prefix + f'Could not probe file: {error}')
        if source:
            result.warnings.append('Original could not be checked; source comparison is incomplete.')
        return [], None


def ffmpeg_check(file, streams, ffmpeg, progress=None):
    command = [ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-xerror',
               '-nostats', '-stats_period', '0.5', '-progress', 'pipe:1', '-i', str(file)]
    for stream in streams:
        command.extend(['-map', f'0:{stream.index}'])
    for index, stream in enumerate(s for s in streams if s.kind == 'video'):
        command.extend([f'-fps_mode:v:{index}', 'passthrough'])
        if stream.time_base:
            command.extend([f'-enc_time_base:v:{index}', str(stream.time_base)])
    command.extend(['-f', 'null', '-'])
    stats = {}

    def consume(line):
        key, separator, value = line.partition('=')
        if not separator:
            return
        stats[key] = value
        if key == 'progress' and progress:
            position = number(stats.get('out_time_us'))
            frames = number(stats.get('frame'))
            progress.update(position / 1e6 if position is not None else None,
                            int(frames) if frames is not None else None)

    returncode, errors = run_streaming(command, consume)
    if returncode:
        errors.append(f'FFmpeg decode check exited with code {returncode}.')
    return errors


def duration_checks(streams, container_duration, result, original=None):
    videos = [s for s in streams if s.kind == 'video' and s.duration is not None]
    audios = [s for s in streams if s.kind == 'audio']
    source_videos = [s for s in original or [] if s.kind == 'video']
    source_audios = [s for s in original or [] if s.kind == 'audio']
    for video_index, video in enumerate(videos):
        threshold = min(0.050, video.interval) if video.interval else 0.050
        for audio_index, audio in enumerate(audios):
            if audio.duration is None:
                result.warnings.append(f'Audio stream {audio.index}: duration check unavailable.')
                continue
            difference = abs(video.duration - audio.duration)
            source_video = source_audio = None
            if len(source_videos) == len(videos) and len(source_audios) == len(audios):
                source_video, source_audio = source_videos[video_index], source_audios[audio_index]
            inherited = audio_timing_matches(source_video, source_audio, video, audio)
            if difference > threshold + 1e-9:
                message = (f'A/V duration mismatch (video {video.index}, audio {audio.index}): '
                           f'{difference:.6f}s > {threshold:.6f}s tolerance.')
                if inherited:
                    source_difference = abs(source_video.duration - source_audio.duration)
                    source_threshold = min(0.050, source_video.interval) if source_video.interval else 0.050
                    # Compare signed differences so a new overhang cannot be called inherited.
                    change = abs((video.duration - audio.duration) -
                                 (source_video.duration - source_audio.duration))
                    rounding = min(0.050, max(video.interval, source_video.interval) / 2)
                    if source_difference > source_threshold + 1e-9 and change <= rounding + 1e-9:
                        message += (f' Already present in original ({source_difference:.6f}s); '
                                    'audio frame count and endpoints match.')
                result.warnings.append(message)
            offset = abs(audio.start - video.start)
            if offset > threshold + 1e-9:
                message = f'A/V start offset (video {video.index}, audio {audio.index}): {offset:.6f}s.'
                if inherited:
                    message += ' Already present in original; audio frame count and endpoints match.'
                result.warnings.append(message)
        if container_duration is not None and abs(video.duration - container_duration) > threshold + 1e-9:
            result.warnings.append(f'Container duration ({container_duration:.6f}s) differs from video '
                                   f'stream {video.index} ({video.duration:.6f}s); subtitles or offsets may explain this.')
    if not videos:
        result.warnings.append('Video duration check unavailable: decoded timestamps are missing.')


def audio_timing_matches(source_video, source_audio, video, audio):
    if source_video is None or source_audio is None:
        return False
    if any(s.duration is None or not s.count for s in (source_video, source_audio, video, audio)):
        return False
    if source_audio.count != audio.count:
        return False
    tolerance = source_audio.time_base + audio.time_base + 1e-9
    return (abs(source_audio.duration - audio.duration) <= tolerance and
            abs((source_audio.start - source_video.start) - (audio.start - video.start)) <=
            tolerance + source_video.time_base + video.time_base)


def find_original(file):
    stem = file.stem[:-4].casefold()
    matches = [candidate for candidate in file.parent.iterdir()
               if candidate.is_file() and candidate != file
               and candidate.suffix.lower() in video_file_extensions
               and candidate.stem.casefold() == stem]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, 'Multiple possible originals found; source comparison skipped: ' + ', '.join(p.name for p in matches)
    return None, 'Original not found in the same folder; source comparison skipped.'


def quantization_grid(source, target, shift, anchor, largest, tolerance):
    """Accept only nearest-grid rounding of every paired frame, never arbitrary jitter."""
    if source.count != target.count or not source.count or shift is None:
        return None
    grid = float(1 / target.frame_rate) if target.frame_rate else target.interval
    if not grid or largest > min(0.050, grid / 2) + tolerance:
        return None
    for a, b in zip(source.timestamps, target.timestamps):
        if not math.isfinite(a) or not math.isfinite(b):
            return None
        on_grid = anchor + round((b - anchor) / grid) * grid
        if abs(b - on_grid) > tolerance:
            return None
    return grid


def compare_original(original, output, result, strict_timing=False):
    timestamp_checks(original, result, source=True, strict_timing=strict_timing)
    timestamp_checks(output, result, strict_timing=strict_timing)
    source_videos = [s for s in original if s.kind == 'video']
    output_videos = [s for s in output if s.kind == 'video']
    if not source_videos:
        return
    if len(source_videos) != len(output_videos):
        result.errors.append(f'Video stream count changed: {len(source_videos)} original, {len(output_videos)} output.')
    shift = anchor = None
    for source, target in zip(source_videos, output_videos):
        for a, b in zip(source.timestamps, target.timestamps):
            if math.isfinite(a) and math.isfinite(b):
                shift = b - a
                anchor = b
                break
        if shift is not None:
            break
    if shift is None:
        result.warnings.append('Original timing comparison unavailable: no paired frame timestamps.')
    for source, target in zip(source_videos, output_videos):
        if source.count != target.count:
            result.errors.append(f'Frame count changed (original stream {source.index}, output {target.index}): '
                                 f'{source.count} original, {target.count} output.')
        tolerance = source.time_base + target.time_base + 1e-9
        first_mismatch = None
        largest = 0
        for frame, (a, b) in enumerate(zip(source.timestamps, target.timestamps), 1):
            if shift is None or not math.isfinite(a) or not math.isfinite(b):
                continue
            difference = abs(b - a - shift)
            largest = max(largest, difference)
            if difference > tolerance and first_mismatch is None:
                first_mismatch = frame, a, b
        if first_mismatch:
            grid = None if strict_timing else quantization_grid(source, target, shift, anchor, largest, tolerance)
            if grid:
                result.warnings.append(f'Frame timing rounded (original stream {source.index}, output {target.index}): '
                                       f'nearest {grid * 1000:.6f}ms grid, maximum change {largest * 1000:.3f}ms; '
                                       'frame count unchanged.')
                continue
            frame, a, b = first_mismatch
            result.errors.append(f'Frame timing changed (original stream {source.index}, output {target.index}), '
                                 f'first at frame {frame}: original {a:.9f}s, output {b:.9f}s, '
                                 f'common shift {shift:.9f}s; maximum difference {largest:.9f}s '
                                 f'> {tolerance:.9f}s tolerance.')


def verify(file, ffprobe, ffmpeg, staxrip=False, ignore_ffmpeg=False, strict_timing=False, reporter=None):
    result = Result()
    options = {'strict_timing': strict_timing, 'reporter': reporter}
    streams, duration = inspect_video(file, ffprobe, result, **options)
    if not streams:
        return result
    if ignore_ffmpeg:
        result.warnings.append('Independent FFmpeg decode check skipped (--ignore-ffmpeg).')
        if reporter:
            reporter.message(reporter.stage('skip', 'FFmpeg', 'decode check', ['skipped (--ignore-ffmpeg)']))
    else:
        try:
            starts = [s.start for s in streams if math.isfinite(s.start)]
            ends = [s.end for s in streams if math.isfinite(s.end)]
            decode_duration = max(ends) - min(starts) if starts and ends else duration
            with Progress(reporter, 'FFmpeg', 'decode all A/V streams', decode_duration) as progress:
                errors = ffmpeg_check(file, streams, ffmpeg, progress)
                result.errors.extend(errors)
                if errors:
                    progress.state = 'error'
        except OSError as error:
            result.errors.append(f'Could not run FFmpeg: {error}')
    source = []
    if staxrip:
        try:
            original, warning = find_original(file)
            if warning:
                result.warnings.append(warning)
            else:
                if reporter:
                    reporter.message(reporter.stage('note', 'Original', original.name))
                source, _ = inspect_video(original, ffprobe, result, source=True, **options)
                with Progress(reporter, 'Compare', 'frame counts & timing') as progress:
                    errors, warnings = len(result.errors), len(result.warnings)
                    compare_original(source, streams, result, strict_timing=strict_timing)
                    progress.state = ('error' if len(result.errors) != errors else
                                      'warn' if len(result.warnings) != warnings else None)
                    progress.detail = ['strict timing' if strict_timing else 'rounding allowed']
        except OSError as error:
            result.warnings.append(f'Could not locate original; source comparison skipped: {error}')
    with Progress(reporter, 'Check', 'A/V durations & offsets', severity='warn') as progress:
        previous = len(result.warnings)
        duration_checks(streams, duration, result, original=source)
        if len(result.warnings) != previous:
            progress.state = 'warn'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check video decoding, frame counts, and timing')
    parser.add_argument('-f', '--folder', nargs='*', default=[], help='Folders containing the videos')
    parser.add_argument('--file', nargs='+', action='append', default=[], metavar='PATH',
                        help='Specific video file(s) to check; may be repeated or combined with --folder')
    parser.add_argument('-r', '--recursive', action='store_true', help='Include subfolders')
    parser.add_argument('-s', '--staxrip', action='store_true', help="Check '_new' files and compare with originals")
    parser.add_argument('-if', '--ignore-ffmpeg', action='store_true', help='Skip the independent FFmpeg decode check')
    parser.add_argument('--strict-timing', action='store_true',
                        help='Fail on frame timing changes beyond time-base rounding, including duplicate timestamps')
    args = parser.parse_args(argv)
    selected_files = [pathlib.Path(p).resolve() for group in args.file for p in group]
    directories = [pathlib.Path(p).resolve() for p in args.folder]
    if not directories and not selected_files:
        directories = [current_directory]
    reporter = Reporter()
    say, paint, symbols = reporter.message, reporter.paint, reporter.symbols

    def display(path):
        for directory in directories:
            try:
                return str(path.relative_to(directory))
            except ValueError:
                pass
        return str(path)

    print_banner(reporter)
    settings = [('Folders' if len(directories) > 1 else 'Folder', str(directory)) for directory in directories[:1]]
    settings += [('', str(directory)) for directory in directories[1:]]
    settings.append(('Checks', 'FFprobe scans' + ('' if args.ignore_ffmpeg else ' + FFmpeg decode') +
                     (' + original comparison' if args.staxrip else '')))
    settings.append(('Timing', 'strict preservation' if args.strict_timing else 'verified timestamp rounding warns'))
    if args.recursive:
        settings.append(('Subfolders', 'included'))
    for key, value in settings:
        say(f'  {paint(key.ljust(11), GRAY)}{value}')

    files = set()
    failed = []
    problems = []
    for file in selected_files:
        try:
            if not file.is_file():
                raise ValueError('Not an existing file; use --folder for directories.')
            if file.suffix.lower() not in video_file_extensions:
                raise ValueError('Unsupported video extension. Supported: ' + ', '.join(video_file_extensions))
            if args.staxrip and not file.stem.endswith('_new'):
                raise ValueError("--staxrip requires a filename ending in '_new' to locate its original.")
            files.add(file)
        except (OSError, ValueError) as error:
            problems.append(f'Cannot select {file}: {error}')
            if file not in failed:
                failed.append(file)
    for directory in directories:
        try:
            for file in directory.rglob('*') if args.recursive else directory.iterdir():
                if (file.is_file() and file.suffix.lower() in video_file_extensions
                        and (not args.staxrip or file.stem.endswith('_new'))):
                    files.add(file)
        except OSError as error:
            problems.append(f'Cannot scan {directory}: {error}')
            failed.append(directory)
    if problems:
        say()
        for problem in problems:
            say(f'  {paint(symbols["error"], RED, BOLD)} {paint(problem, RED)}')
    if not files and not failed:
        say()
        say('  No video files found')
        return 0
    files = sorted(files)
    say()
    say(f'  {paint(plural(len(files), "video"), BOLD)} found')
    number_width = len(str(len(files)))
    for index, file in enumerate(files, 1):
        say(f'  {paint(str(index).rjust(number_width + 2), GRAY)}  {display(file)}')

    warnings = passed = 0
    checked = []
    started = time.monotonic()
    for index, file in enumerate(files, 1):
        say()
        say(f'{paint(f"[{index}/{len(files)}]", CYAN, BOLD)} {paint(display(file), BOLD)}')
        file_started = time.monotonic()
        result = verify(file, executable('ffprobe'), executable('ffmpeg'), args.staxrip, args.ignore_ffmpeg,
                        strict_timing=args.strict_timing, reporter=reporter)
        elapsed = time.monotonic() - file_started
        messages = [('warn', message) for message in result.warnings] + [('error', message) for message in result.errors]
        if messages:
            say(f'  {paint(symbols["stem"], GRAY)}')
            for position, (state, message) in enumerate(messages, 1):
                branch = symbols['last' if position == len(messages) else 'branch']
                say(f'  {paint(branch, GRAY)} {paint(symbols[state], STATE_COLOURS[state], BOLD)} {message}')
        verdict, _, colour = VERDICTS[result.status]
        counts = reporter.join([plural(len(result.errors), 'error'), plural(len(result.warnings), 'warning'),
                                clock_time(elapsed)])
        say(f'  {paint(symbols["bullet"], colour)} {paint(verdict, colour, BOLD)}  {paint(counts, GRAY)}')
        checked.append((file, result, elapsed))
        if result.errors:
            failed.append(file)
        else:
            passed += 1
            warnings += bool(result.warnings)

    say()
    say(paint(symbols['rule'] * 30 + ' Summary ' + symbols['rule'] * 30, GRAY))
    if checked:
        width = max(2, number_width)
        say(paint(f'  {"#":>{width}}  {"Result":<6}  {"Err":>3}  {"Warn":>4}  {"Time":>8}  File', GRAY))
        for index, (file, result, elapsed) in enumerate(checked, 1):
            _, word, colour = VERDICTS[result.status]
            errors = paint(f'{len(result.errors):>3}', RED, BOLD) if result.errors else f'{0:>3}'
            warned = paint(f'{len(result.warnings):>4}', YELLOW) if result.warnings else f'{0:>4}'
            say(f'  {index:>{width}}  {paint(f"{word:<6}", colour, BOLD)}  {errors}  {warned}  '
                f'{clock_time(elapsed):>8}  {display(file)}')
        say()
    totals = [(f'{symbols["done"]} {passed} passed', passed, GREEN),
              (f'{symbols["warn"]} {warnings} with warnings', warnings, YELLOW),
              (f'{symbols["error"]} {len(failed)} failed', len(failed), RED)]
    say('  ' + '   '.join(paint(text, colour, BOLD) if count else paint(text, GRAY) for text, count, colour in totals) +
        '   ' + paint(f'{plural(len(files), "video")} in {clock_time(time.monotonic() - started)}', GRAY))
    if failed:
        say()
        say(f'  {paint("Failed", RED, BOLD)}')
        for file in failed:
            say(f'    {paint(symbols["error"], RED)} {display(file)}')
        try:
            report = pathlib.Path('corrupted_files.txt')
            report.write_text(''.join(file.name + '\n' for file in failed), encoding='utf-8')
            say(f'  {paint("Report", GRAY)}  {report.resolve()}')
        except OSError as error:
            say(f'  {paint(symbols["warn"], YELLOW, BOLD)} Could not write corrupted_files.txt: {error}')
    return 1 if failed else 0


def print_banner(reporter):
    symbols = reporter.symbols
    top_left, top_right, bottom_left, bottom_right, line, side = symbols['box']
    lines = [('VerifyVideo', (BOLD, CYAN)),
             (f'Script to check for corrupted videos {symbols["dot"]} by Kidsnd274', ()),
             ('https://github.com/Kidsnd274/MediaAutomationScripts', (GRAY,))]
    width = max(len(text) for text, _ in lines) + 4
    reporter.message(reporter.paint(top_left + line * width + top_right, GRAY))
    for text, codes in lines:
        reporter.message(reporter.paint(side, GRAY) + '  ' + reporter.paint(text.ljust(width - 2), *codes) +
                         reporter.paint(side, GRAY))
    reporter.message(reporter.paint(bottom_left + line * width + bottom_right, GRAY))
    reporter.message()


if __name__ == '__main__':
    sys.exit(main())
