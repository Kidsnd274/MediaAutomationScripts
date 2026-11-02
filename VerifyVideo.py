import argparse
from array import array
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import islice
import json
import math
import pathlib
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


video_file_extensions = ['.mkv', '.mp4', '.avi', '.webm']
current_directory = pathlib.Path(__file__).resolve().parent


def clock_time(seconds):
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{int(hours)}:{int(minutes):02}:{seconds:05.2f}' if hours else f'{int(minutes)}:{seconds:05.2f}'


class Reporter:
    def __init__(self, output=None):
        self.output = output if output is not None else sys.stdout

    def message(self, message):
        print(message, file=self.output, flush=True)


class Progress:
    """One line per stage in a terminal; occasional updates in redirected logs."""
    def __init__(self, reporter, label, total=None):
        self.output = reporter.output if reporter else None
        self.label = label
        self.total = total if total is not None and math.isfinite(total) and total > 0 else None
        self.interactive = bool(self.output and self.output.isatty())
        self.started = time.monotonic()
        self.updated = self.started
        self.width = 0
        self.detail = ''
        self.issues = False

    def __enter__(self):
        self.write(f'  {self.label} ...')
        return self

    def write(self, text, final=False):
        if self.output is None:
            return
        if self.interactive:
            text = text[:max(20, shutil.get_terminal_size((100, 20)).columns - 1)]
            print('\r' + text.ljust(self.width), end='\n' if final else '', file=self.output, flush=True)
            self.width = len(text)
        else:
            print(text, file=self.output, flush=True)

    def update(self, position=None, count=None):
        if self.output is None:
            return
        now = time.monotonic()
        if now - self.updated < (0.5 if self.interactive else 30):
            return
        self.updated = now
        details = []
        if position is not None and math.isfinite(position):
            position = max(0, position)
            if self.total:
                details.append(f'{min(99.9, position / self.total * 100):5.1f}%')
                details.append(f'{clock_time(position)} / {clock_time(self.total)}')
            else:
                details.append(f'{clock_time(position)} processed')
        if count is not None:
            details.append(f'{count:,} frames')
        details.append(f'elapsed {clock_time(now - self.started)}')
        self.write(f'  {self.label}: ' + ' | '.join(details))

    def __exit__(self, exc_type, exc_value, traceback):
        status = 'failed' if exc_type else 'issues found' if self.issues else 'done'
        detail = f'; {self.detail}' if self.detail else ''
        self.write(f'  {self.label}: {status}{detail} (elapsed {clock_time(time.monotonic() - self.started)})', final=True)


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
    try:
        label = 'original' if source else 'output'
        with Progress(reporter, f'FFprobe: {label} metadata') as progress:
            streams, duration, errors = probe_metadata(file, ffprobe)
            progress.detail = f'{len(streams)} A/V stream(s)'
            progress.issues = bool(errors)
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
                with Progress(reporter, f'FFprobe: {label} {stream.kind} stream {stream.index}',
                              stream.expected_duration or duration) as progress:
                    errors = scan_stream(file, stream, ffprobe, progress) if reporter else scan_stream(file, stream, ffprobe)
                    progress.detail = f'{stream.count:,} frames'
                    if stream.duration is not None:
                        progress.detail += f', media {clock_time(stream.duration)}'
                    progress.issues = bool(errors) or not stream.count
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
            reporter.message('  FFmpeg: independent decode check skipped (--ignore-ffmpeg)')
    else:
        try:
            starts = [s.start for s in streams if math.isfinite(s.start)]
            ends = [s.end for s in streams if math.isfinite(s.end)]
            decode_duration = max(ends) - min(starts) if starts and ends else duration
            with Progress(reporter, 'FFmpeg: decode all output video/audio streams', decode_duration) as progress:
                errors = ffmpeg_check(file, streams, ffmpeg, progress)
                result.errors.extend(errors)
                progress.issues = bool(errors)
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
                    reporter.message('  Original: ' + original.name)
                source, _ = inspect_video(original, ffprobe, result, source=True, **options)
                with Progress(reporter, 'Compare original: frame counts and timing') as progress:
                    previous = len(result.errors), len(result.warnings)
                    compare_original(source, streams, result, strict_timing=strict_timing)
                    progress.issues = previous != (len(result.errors), len(result.warnings))
                    progress.detail = 'strict timing' if strict_timing else 'allow verified timestamp rounding'
        except OSError as error:
            result.warnings.append(f'Could not locate original; source comparison skipped: {error}')
    with Progress(reporter, 'Check audio/video durations and start offsets') as progress:
        previous = len(result.warnings)
        duration_checks(streams, duration, result, original=source)
        progress.issues = len(result.warnings) != previous
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
    print('-----------------VerifyVideo-----------------')
    print('Script to check for corrupted videos')
    print('by Kidsnd274')
    print('https://github.com/Kidsnd274/MediaAutomationScripts')
    print('')
    if directories:
        print('Folders:')
        for directory in directories:
            print('  ' + str(directory))
    print('Checks: FFprobe scans' + ('' if args.ignore_ffmpeg else ' + FFmpeg decode') +
          (' + original comparison' if args.staxrip else ''))
    print('Timing: ' + ('strict preservation' if args.strict_timing else 'verified timestamp rounding warns'))
    if args.recursive:
        print('Include subfolders: yes')
    files = set()
    failed = []
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
            print(f'FAILED: Cannot select {file}: {error}')
            if file not in failed:
                failed.append(file)
    for directory in directories:
        try:
            for file in directory.rglob('*') if args.recursive else directory.iterdir():
                if (file.is_file() and file.suffix.lower() in video_file_extensions
                        and (not args.staxrip or file.stem.endswith('_new'))):
                    files.add(file)
        except OSError as error:
            print(f'FAILED: Cannot scan {directory}: {error}')
            failed.append(directory)
    if not files and not failed:
        print('No video files found')
        return 0
    files = sorted(files)
    print(f'\nVideos detected: {len(files)}')
    for index, file in enumerate(files, 1):
        print(f'  {index}. {file}')
    warnings = passed = 0
    started = time.monotonic()
    reporter = Reporter()
    for index, file in enumerate(files, 1):
        print(f'\n[{index}/{len(files)}] Checking: {file.name}', flush=True)
        file_started = time.monotonic()
        result = verify(file, executable('ffprobe'), executable('ffmpeg'), args.staxrip, args.ignore_ffmpeg,
                        strict_timing=args.strict_timing, reporter=reporter)
        for message in result.warnings:
            print('  WARN:', message)
        for message in result.errors:
            print('  ERROR:', message)
        counts = f'{len(result.errors)} error(s), {len(result.warnings)} warning(s)'
        print(f'{result.status}: {file.name} ({counts}; elapsed {clock_time(time.monotonic() - file_started)})', flush=True)
        warnings += bool(result.warnings)
        if result.errors:
            failed.append(file)
        else:
            passed += 1
    if failed:
        print('\nFailed files/checks:')
        for file in failed:
            print(file)
        try:
            report = pathlib.Path('corrupted_files.txt')
            report.write_text(''.join(file.name + '\n' for file in failed), encoding='utf-8')
            print('Failure report:', report.resolve())
        except OSError as error:
            print('WARN: Could not write corrupted_files.txt:', error)
    print(f'\nAll files checked: {len(files)} video(s), {len(failed)} failure(s), {warnings} with warnings.')
    print(f'Passed: {passed} | Total elapsed: {clock_time(time.monotonic() - started)}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
