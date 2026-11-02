"""Output style preview for VerifyVideo.py (mock data, no FFmpeg needed).

Usage: python previews/style_preview.py [A] [B] [C] [--no-anim]
"""
import os
import sys
import time


def enable_ansi():
    """Turn on ANSI escape handling in the Windows console (stdlib only)."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


enable_ansi()
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass

ANIMATE = '--no-anim' not in sys.argv and sys.stdout.isatty()

BOLD, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GRAY = '1', '31', '32', '33', '34', '35', '36', '90'
ON_RED, ON_GREEN, ON_YELLOW, ON_CYAN, BLACK, WHITE = '41', '42', '43', '46', '30', '97'


def c(text, *codes):
    return f'\033[{";".join(codes)}m{text}\033[0m' if codes else str(text)


def clock(seconds):
    minutes, seconds = divmod(max(0, seconds), 60)
    return f'{int(minutes)}:{seconds:05.2f}'


def animate(render, total, seconds=0.9, steps=24):
    """Simulate the live, in-place progress line for a slow stage."""
    if not ANIMATE:
        return
    for step in range(steps):
        sys.stdout.write('\r' + render(total * step / steps, total, step) + '\033[K')
        sys.stdout.flush()
        time.sleep(seconds / steps)
    sys.stdout.write('\r\033[K')


# ----------------------------------------------------------------- mock data
URL = 'https://github.com/Kidsnd274/MediaAutomationScripts'
FOLDER = r'L:\repos\VerifyVideo'
CHECKS = 'FFprobe scans + FFmpeg decode'
TIMING = 'verified timestamp rounding warns'
BASE = 'Genshin Impact 2024.09.02 - 00.48.08.07'
TOTAL = 67.89
# stage: (tool, label, status, detail, elapsed, media duration to animate or 0)
FILES = [
    {'name': BASE + '.mp4', 'elapsed': 26.66, 'stages': [
        ('FFprobe', 'metadata', 'done', '3 A/V streams', 0.15, 0),
        ('FFprobe', 'video stream 0', 'done', '5,052 frames, media 1:24.20', 16.31, 84.20),
        ('FFprobe', 'audio stream 1', 'done', '3,940 frames, media 1:24.05', 0.17, 0),
        ('FFprobe', 'audio stream 2', 'done', '3,940 frames, media 1:24.07', 0.17, 0),
        ('FFmpeg', 'decode all streams', 'done', '', 9.86, 84.20),
        ('Check', 'A/V durations & offsets', 'issues', '', 0.00, 0)],
     'warnings': ['A/V duration mismatch (video 0, audio 1): 0.144918s > 0.016589s tolerance.',
                  'A/V duration mismatch (video 0, audio 2): 0.127876s > 0.016589s tolerance.'],
     'errors': []},
    {'name': BASE + '_new-A.mp4', 'elapsed': 15.92, 'stages': [
        ('FFprobe', 'metadata', 'done', '3 A/V streams', 0.06, 0),
        ('FFprobe', 'video stream 0', 'failed', '', 7.70, 84.20),
        ('FFprobe', 'audio stream 1', 'done', '3,940 frames, media 1:24.05', 0.16, 0),
        ('FFprobe', 'audio stream 2', 'done', '3,940 frames, media 1:24.07', 0.16, 0),
        ('FFmpeg', 'decode all streams', 'failed', '', 7.84, 0),
        ('Check', 'A/V durations & offsets', 'done', '', 0.00, 0)],
     'warnings': [],
     'errors': ['[h264 @ 000001f2c3a4b5c0] Invalid NAL unit size (28471 > 9132).',
                '[h264 @ 000001f2c3a4b5c0] Error splitting the input into NAL units.',
                'FFprobe stream 0 exited with code 1.',
                'FFmpeg decode check exited with code 1.']},
    {'name': BASE + '_new-C.mp4', 'elapsed': 25.31, 'stages': [
        ('FFprobe', 'metadata', 'done', '3 A/V streams', 0.05, 0),
        ('FFprobe', 'video stream 0', 'done', '5,052 frames, media 1:24.20', 15.12, 84.20),
        ('FFprobe', 'audio stream 1', 'done', '3,940 frames, media 1:24.05', 0.16, 0),
        ('FFprobe', 'audio stream 2', 'done', '3,940 frames, media 1:24.07', 0.16, 0),
        ('FFmpeg', 'decode all streams', 'done', '', 9.82, 0),
        ('Check', 'A/V durations & offsets', 'done', '', 0.00, 0)],
     'warnings': [], 'errors': []},
]


def verdict(f):
    return 'FAILED' if f['errors'] else 'OK WITH WARNINGS' if f['warnings'] else 'OK'


FAILED = [f for f in FILES if f['errors']]
WARNED = [f for f in FILES if f['warnings'] and not f['errors']]
PASSED = [f for f in FILES if not f['errors']]
REPORT = FOLDER + r'\corrupted_files.txt'


# ------------------------------------------------- Style A: classic + colour
def style_a():
    word = {'done': ('done', GREEN), 'failed': ('failed', RED), 'issues': ('issues found', YELLOW),
            'skipped': ('skipped', GRAY)}
    tone = {'OK': GREEN, 'OK WITH WARNINGS': YELLOW, 'FAILED': RED}
    print(c('-----------------VerifyVideo-----------------', CYAN, BOLD))
    print('Script to check for corrupted videos')
    print(c('by Kidsnd274', GRAY))
    print(c(URL, GRAY))
    print()
    print(c('Folders:', BOLD))
    print('  ' + FOLDER)
    print(c('Checks:', BOLD), CHECKS)
    print(c('Timing:', BOLD), TIMING)
    print()
    print(c('Videos detected:', BOLD), c(len(FILES), CYAN, BOLD))
    for i, f in enumerate(FILES, 1):
        print(f'  {c(f"{i}.", GRAY)} {FOLDER}\\{f["name"]}')
    for i, f in enumerate(FILES, 1):
        print(f'\n{c(f"[{i}/{len(FILES)}]", CYAN, BOLD)} Checking: {c(f["name"], BOLD)}')
        for tool, label, status, detail, elapsed, media in f['stages']:
            if media:
                animate(lambda p, t, _: f'  {tool}: {label}: {c(f"{p / t * 100:5.1f}%", CYAN)} | '
                                        f'{clock(p)} / {clock(t)} | {c(f"elapsed {clock(elapsed * p / t)}", GRAY)}',
                        media)
            text, colour = word[status]
            extra = f'; {detail}' if detail else ''
            print(f'  {tool}: {label}: {c(text, colour, BOLD)}{extra} {c(f"(elapsed {clock(elapsed)})", GRAY)}')
        for message in f['warnings']:
            print(f'  {c("WARN:", YELLOW, BOLD)} {message}')
        for message in f['errors']:
            print(f'  {c("ERROR:", RED, BOLD)} {c(message, RED)}')
        status = verdict(f)
        counts = f'{len(f["errors"])} error(s), {len(f["warnings"])} warning(s); elapsed {clock(f["elapsed"])}'
        print(f'{c(status + ":", tone[status], BOLD)} {f["name"]} {c(f"({counts})", GRAY)}')
    print(f'\n{c("Failed files/checks:", RED, BOLD)}')
    for f in FAILED:
        print('  ' + c(f'{FOLDER}\\{f["name"]}', RED))
    print(f'Failure report: {REPORT}')
    print(f'\n{c("All files checked:", BOLD)} {len(FILES)} video(s), '
          f'{c(f"{len(FAILED)} failure(s)", RED, BOLD)}, {c(f"{len(WARNED)} with warnings", YELLOW, BOLD)}.')
    print(f'{c("Passed:", BOLD)} {c(len(PASSED), GREEN, BOLD)} | Total elapsed: {clock(TOTAL)}')


# ------------------------------------------------ Style B: icons + columns
def style_b():
    icon = {'done': ('✓', GREEN), 'failed': ('✗', RED), 'issues': ('▲', YELLOW), 'skipped': ('–', GRAY)}
    spinner = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    def bar(fraction, width=24):
        filled = int(fraction * width)
        return c('━' * filled, CYAN) + c('━' * (width - filled), GRAY)

    lines = [('VerifyVideo', (BOLD, CYAN)), ('Check videos for corruption · by Kidsnd274', ()),
             (URL, (GRAY,))]
    width = max(len(text) for text, _ in lines) + 4
    print(c('╭' + '─' * width + '╮', GRAY))
    for text, codes in lines:
        print(c('│', GRAY) + '  ' + c(text.ljust(width - 2), *codes) + c('│', GRAY))
    print(c('╰' + '─' * width + '╯', GRAY))
    print()
    for key, value in (('Folder', FOLDER), ('Checks', CHECKS), ('Timing', TIMING)):
        print(f'  {c(key.ljust(8), GRAY)}{value}')
    print()
    print(f'  {c(len(FILES), BOLD)} videos found')
    for i, f in enumerate(FILES, 1):
        print(f'  {c(str(i).rjust(3), GRAY)}  {f["name"]}')

    for i, f in enumerate(FILES, 1):
        print(f'\n{c(f"[{i}/{len(FILES)}]", CYAN, BOLD)} {c(f["name"], BOLD)}')
        for tool, label, status, detail, elapsed, media in f['stages']:
            if media:
                animate(lambda p, t, k: f'  {c(spinner[k % len(spinner)], CYAN)} {c(tool.ljust(8), GRAY)}'
                                        f'{label.ljust(25)}{bar(p / t)} {p / t * 100:5.1f}%  '
                                        f'{c(f"{clock(p)} / {clock(t)}  {int(p * 60):,} frames", GRAY)}', media)
            symbol, colour = icon[status]
            detail = detail.replace(', media ', ' · ') or ('errors (see below)' if status == 'failed' else '')
            detail = c(detail.ljust(28), RED) if status == 'failed' else detail.ljust(28)
            print(f'  {c(symbol, colour, BOLD)} {c(tool.ljust(8), GRAY)}{label.ljust(25)}{detail}'
                  f'{c(clock(elapsed).rjust(8), GRAY)}')
        messages = [('▲', YELLOW, m) for m in f['warnings']] + [('✗', RED, m) for m in f['errors']]
        if messages:
            print(f'  {c("│", GRAY)}')
            for k, (symbol, colour, message) in enumerate(messages):
                branch = '└─' if k == len(messages) - 1 else '├─'
                print(f'  {c(branch, GRAY)} {c(symbol, colour, BOLD)} {message}')
        status = verdict(f)
        text, colour = {'OK': ('PASSED', GREEN), 'OK WITH WARNINGS': ('PASSED WITH WARNINGS', YELLOW),
                        'FAILED': ('FAILED', RED)}[status]
        counts = f'{len(f["errors"])} errors · {len(f["warnings"])} warnings · {clock(f["elapsed"])}'
        print(f'  {c("●", colour)} {c(text, colour, BOLD)}  {c(counts, GRAY)}')

    print('\n' + c('─' * 30 + ' Summary ' + '─' * 30, GRAY))
    print(f'  {c(f"✓ {len(PASSED)} passed", GREEN, BOLD)}   {c(f"▲ {len(WARNED)} with warnings", YELLOW, BOLD)}   '
          f'{c(f"✗ {len(FAILED)} failed", RED, BOLD)}   {c(f"{len(FILES)} videos in {clock(TOTAL)}", GRAY)}')
    print(f'\n  {c("Failed", RED, BOLD)}')
    for f in FAILED:
        print(f'    {c("✗", RED)} {f["name"]}')
    print(f'  {c("Report", GRAY)}  {REPORT}')


# ---------------------------------------------- Style C: badges + results table
def style_c():
    def badge(text, *codes):
        return c(f' {text} ', *codes, BOLD)

    stage_word = {'done': ('ok', GREEN), 'failed': ('FAIL', RED), 'issues': ('warn', YELLOW),
                  'skipped': ('skip', GRAY)}
    verdict_badge = {'OK': ('PASS', ON_GREEN, BLACK), 'OK WITH WARNINGS': ('WARN', ON_YELLOW, BLACK),
                     'FAILED': ('FAIL', ON_RED, WHITE)}

    def leader(label, width=26):
        return f'{label} {c("·" * (width - len(label)), GRAY)} '

    def bar(fraction, width=20):
        filled = int(fraction * width)
        return c('█' * filled, CYAN) + c('░' * (width - filled), GRAY)

    print(f'{badge("VerifyVideo", ON_CYAN, BLACK)}  check videos for corruption  {c("· by Kidsnd274", GRAY)}')
    print(c(URL, GRAY))
    print()
    for key, value in (('Folder', FOLDER), ('Checks', CHECKS), ('Timing', TIMING),
                       ('Videos', f'{len(FILES)} found')):
        print(f' {c(key.ljust(7), BOLD)} {value}')

    for i, f in enumerate(FILES, 1):
        print(f'\n {c(f"{i}/{len(FILES)}", CYAN, BOLD)}  {c(f["name"], BOLD)}')
        for tool, label, status, detail, elapsed, media in f['stages']:
            label = f'{tool} {label}' if tool != 'Check' else label
            if media:
                animate(lambda p, t, _: f'      {leader(label)}[{bar(p / t)}] {p / t * 100:3.0f}%  '
                                        f'{c(f"{clock(p)} / {clock(t)}", GRAY)}', media)
            text, colour = stage_word[status]
            detail = detail.replace(', media ', ', ')
            print(f'      {leader(label)}{c(text.ljust(5), colour, BOLD)} {detail.ljust(28)}'
                  f'{c(clock(elapsed).rjust(8), GRAY)}')
        for message in f['warnings']:
            print(f'      {c("warn ", YELLOW, BOLD)} {message}')
        for message in f['errors']:
            print(f'      {c("error", RED, BOLD)} {message}')
        text, *codes = verdict_badge[verdict(f)]
        counts = f'{len(f["errors"])} errors · {len(f["warnings"])} warnings · {clock(f["elapsed"])}'
        print(f' {badge(text, *codes)} {c(counts, GRAY)}')

    print(f'\n {c("Results", BOLD)}')
    print(c(' ' + '─' * 78, GRAY))
    print(c(f'  {"#":>2}  {"Result":<6}  {"Err":>3}  {"Warn":>4}  {"Time":>8}  File', GRAY))
    for i, f in enumerate(FILES, 1):
        text, background, _ = verdict_badge[verdict(f)]
        colour = {ON_GREEN: GREEN, ON_YELLOW: YELLOW, ON_RED: RED}[background]
        errors = c(f'{len(f["errors"]):>3}', RED, BOLD) if f['errors'] else f'{0:>3}'
        warnings = c(f'{len(f["warnings"]):>4}', YELLOW) if f['warnings'] else f'{0:>4}'
        print(f'  {i:>2}  {c(f"{text:<6}", colour, BOLD)}  {errors}  {warnings}  {clock(f["elapsed"]):>8}  {f["name"]}')
    print(c(' ' + '─' * 78, GRAY))
    print(f' {len(FILES)} videos  ·  {c(f"{len(PASSED)} passed", GREEN, BOLD)}  ·  '
          f'{c(f"{len(WARNED)} with warnings", YELLOW, BOLD)}  ·  {c(f"{len(FAILED)} failed", RED, BOLD)}  ·  '
          f'{clock(TOTAL)} total')
    print(f' Failure report → {REPORT}')


STYLES = {'A': ('Classic + colour', style_a), 'B': ('Icons + aligned columns', style_b),
          'C': ('Badges + results table', style_c)}


def main():
    chosen = [arg.upper() for arg in sys.argv[1:] if arg.upper() in STYLES] or list(STYLES)
    for key in chosen:
        name, render = STYLES[key]
        print('\n' + c(f'════════════ Style {key}: {name} ════════════', MAGENTA, BOLD) + '\n')
        render()
    print()


if __name__ == '__main__':
    main()
