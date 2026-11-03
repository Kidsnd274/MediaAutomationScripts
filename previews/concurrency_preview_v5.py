"""V5: V3 pinned, with a full-width divider above the live progress panel.

    python previews/concurrency_preview_v5.py
    python previews/concurrency_preview_v5.py -if --speed 1
    python previews/concurrency_preview_v5.py --start-at 10 --paused

Identical to V3 pinned except that the "Live progress" rule now spans the whole
window, so the pinned panel is clearly separated from the transcript above it.

Wheel: scroll transcript. Click/drag its right-hand scrollbar. Arrows/PgUp/PgDn
also browse; Home shows the first file; End follows completions. Space pauses.
Q/Ctrl+C stops. The full ordered transcript is printed to scrollback on exit.
All data is fictional; no media, decoder processes or reports are involved.
"""
import pathlib
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _concurrency_preview_v3 as shared
import concurrency_preview_v3_pinned as pinned


class Transcript(shared.Transcript):
    def footer(self, now, width, paused=False, pinned=True):
        lines = super().footer(now, width, paused, pinned)
        rule = self.reporter.symbols['rule']
        left = rule * min(18, max(1, width - 16)) + ' Live progress '
        lines[0] = self.reporter.paint(left + rule * max(0, width - len(left)), shared.v1.GRAY)
        return lines


class Screen(pinned.PinnedScreen):
    def frame(self, preview, now, columns, height, events=(), paused=False):
        lines = super().frame(preview, now, columns, height, events, paused)
        lines[0] = lines[0].replace('v3 pinned', 'v5', 1)
        return lines


def main(argv=None):
    return shared.run(__doc__, True, Screen, pinned.MouseInput, argv, title='V5', transcript_type=Transcript)


if __name__ == '__main__':
    sys.exit(main())
