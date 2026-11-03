"""Shared mock data and formatting for the two V3 previews; no media is opened."""
import argparse
import pathlib
import shutil
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import concurrency_preview_v2 as v2

v1 = v2.v1


class Transcript(v2.Transcript):
    def file_lines(self, index, now):
        lines = super().file_lines(index, now)
        for position, (stage, state, fraction) in enumerate(v2.states_at(self.files[index], now), 1):
            if state == 'running':
                lines[position] = self.stage_line(stage, state, fraction, now)
        return lines

    def progress_line(self, index, now, width):
        file = self.files[index]
        running = next(((stage, fraction) for stage, state, fraction in v2.states_at(file, now)
                        if state == 'running'), None)
        if running is None:
            return f'  [{index + 1}] {file.name} | finishing'
        stage, fraction = running
        spinner = self.reporter.symbols['spinner']
        symbol = spinner[int(now * 8) % len(spinner)]
        prefix = '  ' + self.reporter.paint(symbol, v1.CYAN) + ' '
        bar_width = max(3, min(12, (width - 40) // 3))
        progress = f'[{self.reporter.bar(fraction, bar_width)}] {fraction * 100:5.1f}%' if stage.media else 'checking'
        if width < 32:
            return prefix + f'[{index + 1}] ' + progress
        progress_width = bar_width + 9 if stage.media else 8
        available = max(2, width - 6 - progress_width)
        name_width = min(28, available // 2)
        label_width = available - name_width
        name = v1.fit(f'[{index + 1}/{len(self.files)}] {file.name}', name_width).ljust(name_width)
        label = v1.fit(f'{stage.tool} {stage.label}', label_width).ljust(label_width)
        return prefix + self.reporter.paint(name, v1.CYAN) + ' ' + label + ' ' + progress

    def footer(self, now, width, paused=False, pinned=True):
        lines = super().footer(now, width, paused)
        controls = ('Wheel/drag: scroll | Home/End: first/follow | Space: pause | Q: stop' if pinned else
                    'Mouse wheel: terminal scrollback | Space: pause | Q/Ctrl+C: stop')
        lines[-1] = self.reporter.paint('  ' + controls, v1.GRAY)
        return lines

    def snapshot(self, now, width, pinned=True):
        mode = 'pinned + mouse scrollbar' if pinned else 'normal terminal scrollback'
        return [f'\n  --- V3 {mode}: snapshot at {v1.clock_time(now)} ---'] + self.document(now, width)[0] + [
            ''] + self.footer(now, width, pinned=pinned)


class StageLog:
    """Append stage completions immediately, with deterministic order within each poll."""
    def __init__(self, preview):
        self.preview = preview
        self.seen = set()
        self.messages = set()
        self.verdicts = set()

    def poll(self, now):
        p, lines = self.preview, []
        for index, file in enumerate(p.files):
            if now < file.start:
                continue
            updates = []
            for position, (stage, state, fraction) in enumerate(v2.states_at(file, now)):
                if state == 'done' and (index, position) not in self.seen:
                    updates.append(p.stage_line(stage, state, fraction, now))
                    self.seen.add((index, position))
            # The model reveals diagnostics as soon as the relevant stage finishes.
            for line in p.file_lines(index, now)[1 + len(file.stages):]:
                # Tree branches can change when another diagnostic arrives.
                message = v2.ANSI.sub('', line).strip().lstrip('\u251c\u2500\u2514\u2502|`- ')
                key = (index, message)
                if now >= file.end and line == p.verdict_line(file):
                    if index not in self.verdicts:
                        updates.append(line)
                        self.verdicts.add(index)
                elif key not in self.messages:
                    updates.append(line)
                    self.messages.add(key)
            if updates:
                lines.extend(['', p.heading(index) + p.reporter.paint(' | completed stage details', v1.GRAY)])
                lines.extend(updates)
        return lines


def active_area(preview, now, columns, height, paused=False):
    """Bound the mutable area so relative cursor updates cannot reach old history."""
    width = max(1, columns - 1)
    footer = preview.footer(now, width, paused, pinned=False)
    active = [i for i, file in enumerate(preview.files) if file.start <= now < file.end]
    blocks = []
    for index in active:
        blocks.extend(['', preview.heading(index)])
        for stage, state, fraction in v2.states_at(preview.files[index], now):
            if state == 'running':
                blocks.extend(v2.wrap_line(preview.stage_line(stage, state, fraction, now), width))
            elif state == 'done':
                blocks.append(preview.stage_line(stage, state, fraction, now))
    header = [preview.reporter.paint('  Active transcript | finished stages are saved above', v1.CYAN)]
    if len(header + blocks + footer) > height:
        # Every completed stage is already in scrollback; keep each active bar visible.
        blocks = []
        for index in active:
            blocks.append(preview.heading(index))
            blocks.append(preview.progress_line(index, now, width))
    if len(header + blocks + footer) > height:
        # Short windows still show each running task in the live section.
        blocks = []
    return (header + blocks + [''] + footer)[-height:]


def parser_for(description):
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--jobs', type=int, choices=range(1, 6), default=2)
    parser.add_argument('-if', '--ignore-ffmpeg', action='store_true')
    parser.add_argument('--speed', type=v1.positive_float, default=2, help='Simulation speed (default: 2)')
    parser.add_argument('--start-at', type=v2.nonnegative_float, default=0, metavar='SECONDS',
                        help='Start later in the simulation, useful for comparing scrolling')
    parser.add_argument('--paused', action='store_true', help='Start paused; Space resumes')
    parser.add_argument('--no-anim', action='store_true', help='Print representative snapshots without waiting')
    parser.add_argument('--snapshot', type=v2.nonnegative_float, metavar='SECONDS', help='Print one static moment')
    return parser


def run(description, pinned, screen_type, input_type, argv=None, title='V3', transcript_type=None):
    args = parser_for(description).parse_args(argv)
    r = v1.Reporter()
    files = v1.mock_files(args.ignore_ffmpeg)
    v1.schedule(files, args.jobs)
    preview = (transcript_type or Transcript)(r, files, args.jobs)
    total = max(file.end for file in files)
    v1.print_banner(r)
    mode = 'PINNED + MOUSE SCROLLBAR' if pinned else 'NORMAL TERMINAL SCROLLBACK'
    r.message(r.paint(f'  CONCURRENCY PREVIEW {title} | ' + mode, v1.CYAN, v1.BOLD))
    r.message(f'  Workers    {preview.jobs} files; sequential checks within each file')
    r.message('  Checks     FFprobe scans' + ('' if args.ignore_ffmpeg else ' + FFmpeg decode'))
    r.message(f'  Animation  {args.speed:g}x speed; all media, timings and diagnostics are fictional')
    if not pinned:
        r.message('  History    Stage completions appear immediately; final transcript uses scan order')
    for index, file in enumerate(files, 1):
        r.message(f'    {index}  {file.name}')
    r.message()

    if args.snapshot is not None:
        for line in preview.snapshot(min(args.snapshot, total), 109, pinned):
            r.message(line)
        return 0
    if args.no_anim or not r.ansi:
        for now in sorted({0.0, min(file.end for file in files) + 0.01, max(0, files[0].end - 0.5)}):
            for line in preview.snapshot(now, 109, pinned):
                r.message(line)
        print_final(preview, total, total, False, 109)
        return 0

    now, paused, stopped = min(args.start_at, total), args.paused, False
    log = StageLog(preview)
    last_tick = time.monotonic()
    try:
        with screen_type(r) as screen, input_type() as keyboard:
            while True:
                tick = time.monotonic()
                if not paused:
                    now = min(total, now + (tick - last_tick) * args.speed)
                last_tick = tick
                events = keyboard.read_all()
                if any(event.get('action') == 'quit' for event in events):
                    stopped = True
                    break
                for event in events:
                    if event.get('action') == 'pause':
                        paused = not paused
                columns, rows = shutil.get_terminal_size((110, 35))
                if pinned:
                    height = max(1, rows - 1)
                    lines = screen.frame(preview, now, columns, height, events, paused)
                    screen.draw(lines, columns, height)
                else:
                    height = max(1, rows - 3)
                    history = log.poll(now)
                    lines = [] if now >= total else active_area(preview, now, columns, height, paused)
                    screen.draw(history, lines, columns, rows)
                if now >= total:
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        stopped = True
    width = max(1, shutil.get_terminal_size((110, 35)).columns - 1)
    print_final(preview, now, total, stopped, width)
    return 130 if stopped else 0


def print_final(preview, now, total, stopped, width):
    r = preview.reporter
    r.message('\n  --- ' + ('Stopped: current' if stopped else 'Final ordered') + ' transcript ---')
    for line in preview.document(now, width)[0]:
        r.message(line)
    if stopped:
        r.message('\n  Preview stopped; only details reached so far are shown.')
    else:
        for line in preview.summary(total):
            r.message(line)


class PlainInput(v2.Keyboard):
    """No mouse capture: scrolling belongs entirely to the terminal."""
    def read_all(self):
        action = self.read()
        return [{'action': action}] if action else []
