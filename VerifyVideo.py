import argparse
from array import array
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import islice
import json
import math
import pathlib
import statistics
import subprocess
import sys
import tempfile


video_file_extensions = ['.mkv', '.mp4', '.avi', '.webm']
current_directory = pathlib.Path(__file__).resolve().parent


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
                              number(item.get('sample_rate')) or 0))
    return streams, number(data.get('format', {}).get('duration')), errors


def scan_stream(file, stream, ffprobe):
    # Selecting one A/V stream prevents subtitle decoding from affecting this check.
    command = [ffprobe, '-v', 'error', '-select_streams', str(stream.index),
               '-threads', '8', '-show_frames', '-show_entries',
               'frame=stream_index,best_effort_timestamp,pts,duration,pkt_duration,nb_samples:frame_side_data=',
               '-of', 'compact=p=0:nk=0', str(file)]
    # A temporary stderr file avoids pipe deadlocks while stdout is read incrementally.
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8', errors='replace') as diagnostics:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=diagnostics,
                              text=True, encoding='utf-8', errors='replace') as process:
            try:
                for line in process.stdout:
                    if not line.strip():
                        continue
                    frame = dict(part.split('=', 1) for part in line.strip().split('|') if '=' in part)
                    if int(frame['stream_index']) != stream.index:
                        raise ValueError('FFprobe returned an unexpected frame stream index.')
                    stream.add_frame(frame)
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
    if process.returncode:
        errors.append(f'FFprobe stream {stream.index} exited with code {process.returncode}.')
    return errors


def inspect_video(file, ffprobe, result, source=False):
    issues = result.warnings if source else result.errors
    prefix = 'Original: ' if source else ''
    try:
        streams, duration, errors = probe_metadata(file, ffprobe)
        incomplete = bool(errors)
        issues.extend(prefix + message for message in errors)
        videos = [stream for stream in streams if stream.kind == 'video']
        if not videos:
            issues.append(prefix + 'No motion-video streams found.')
            if source:
                result.warnings.append('Original could not be checked; source comparison is incomplete.')
            return [], duration
        for stream in streams:
            if source and stream.kind != 'video':
                continue
            try:
                errors = scan_stream(file, stream, ffprobe)
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
        if source and incomplete:
            result.warnings.append('Original has probe/decoder errors; source comparison is incomplete.')
        return streams, duration
    except (OSError, ValueError, KeyError, TypeError) as error:
        issues.append(prefix + f'Could not probe file: {error}')
        if source:
            result.warnings.append('Original could not be checked; source comparison is incomplete.')
        return [], None


def ffmpeg_check(file, streams, ffmpeg):
    command = [ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-xerror', '-i', str(file)]
    for stream in streams:
        command.extend(['-map', f'0:{stream.index}'])
    for index, stream in enumerate(s for s in streams if s.kind == 'video'):
        command.extend([f'-fps_mode:v:{index}', 'passthrough'])
        if stream.time_base:
            command.extend([f'-enc_time_base:v:{index}', str(stream.time_base)])
    command.extend(['-f', 'null', '-'])
    process = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8', errors='replace')
    errors = diagnostic_messages(process.stderr)
    if process.returncode:
        errors.append(f'FFmpeg decode check exited with code {process.returncode}.')
    return errors


def duration_checks(streams, container_duration, result):
    videos = [s for s in streams if s.kind == 'video' and s.duration is not None]
    audios = [s for s in streams if s.kind == 'audio']
    for video in videos:
        threshold = min(0.050, video.interval) if video.interval else 0.050
        for audio in audios:
            if audio.duration is None:
                result.warnings.append(f'Audio stream {audio.index}: duration check unavailable.')
                continue
            difference = abs(video.duration - audio.duration)
            if difference > threshold + 1e-9:
                result.warnings.append(f'A/V duration mismatch (video {video.index}, audio {audio.index}): '
                                       f'{difference:.6f}s > {threshold:.6f}s tolerance.')
            offset = abs(audio.start - video.start)
            if offset > threshold + 1e-9:
                result.warnings.append(f'A/V start offset (video {video.index}, audio {audio.index}): {offset:.6f}s.')
        if container_duration is not None and abs(video.duration - container_duration) > threshold + 1e-9:
            result.warnings.append(f'Container duration ({container_duration:.6f}s) differs from video '
                                   f'stream {video.index} ({video.duration:.6f}s); subtitles or offsets may explain this.')
    if not videos:
        result.warnings.append('Video duration check unavailable: decoded timestamps are missing.')


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


def compare_original(original, output, result):
    source_videos = [s for s in original if s.kind == 'video']
    output_videos = [s for s in output if s.kind == 'video']
    if not source_videos:
        return
    if len(source_videos) != len(output_videos):
        result.errors.append(f'Video stream count changed: {len(source_videos)} original, {len(output_videos)} output.')
    shift = None
    for source, target in zip(source_videos, output_videos):
        for a, b in zip(source.timestamps, target.timestamps):
            if math.isfinite(a) and math.isfinite(b):
                shift = b - a
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
            frame, a, b = first_mismatch
            result.errors.append(f'Frame timing changed (original stream {source.index}, output {target.index}), '
                                 f'first at frame {frame}: original {a:.9f}s, output {b:.9f}s, '
                                 f'common shift {shift:.9f}s; maximum difference {largest:.9f}s '
                                 f'> {tolerance:.9f}s tolerance.')


def verify(file, ffprobe, ffmpeg, staxrip=False, ignore_ffmpeg=False):
    result = Result()
    streams, duration = inspect_video(file, ffprobe, result)
    if not streams:
        return result
    if ignore_ffmpeg:
        result.warnings.append('Independent FFmpeg decode check skipped (--ignore-ffmpeg).')
    else:
        try:
            result.errors.extend(ffmpeg_check(file, streams, ffmpeg))
        except OSError as error:
            result.errors.append(f'Could not run FFmpeg: {error}')
    duration_checks(streams, duration, result)
    if staxrip:
        try:
            original, warning = find_original(file)
            if warning:
                result.warnings.append(warning)
            else:
                print('Original:', original.name)
                source, _ = inspect_video(original, ffprobe, result, source=True)
                compare_original(source, streams, result)
        except OSError as error:
            result.warnings.append(f'Could not locate original; source comparison skipped: {error}')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check video decoding, frame counts, and timing')
    parser.add_argument('-f', '--folder', nargs='*', default=[], help='Folders containing the videos')
    parser.add_argument('-r', '--recursive', action='store_true', help='Include subfolders')
    parser.add_argument('-s', '--staxrip', action='store_true', help="Check '_new' files and compare with originals")
    parser.add_argument('-if', '--ignore-ffmpeg', action='store_true', help='Skip the independent FFmpeg decode check')
    args = parser.parse_args(argv)
    directories = [pathlib.Path(p).resolve() for p in args.folder] or [current_directory]
    print('-----------------VerifyVideo-----------------')
    print('Folders:', [str(p) for p in directories])
    files = set()
    failed = []
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
    warnings = 0
    for file in sorted(files):
        print('\nChecking:', file)
        result = verify(file, executable('ffprobe'), executable('ffmpeg'), args.staxrip, args.ignore_ffmpeg)
        for message in result.warnings:
            print('WARN:', message)
        for message in result.errors:
            print('ERROR:', message)
        print(result.status + ':', file.name)
        warnings += bool(result.warnings)
        if result.errors:
            failed.append(file)
    if failed:
        print('\nFailed files/checks:')
        for file in failed:
            print(file)
        try:
            pathlib.Path('corrupted_files.txt').write_text(''.join(file.name + '\n' for file in failed), encoding='utf-8')
        except OSError as error:
            print('WARN: Could not write corrupted_files.txt:', error)
    print(f'\nAll files checked: {len(files)} video(s), {len(failed)} failure(s), {warnings} with warnings.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
