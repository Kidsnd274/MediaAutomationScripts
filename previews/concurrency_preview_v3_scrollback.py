"""V3B: normal terminal scrollback, with animated transcript and live rows.

    python previews/concurrency_preview_v3_scrollback.py
    python previews/concurrency_preview_v3_scrollback.py -if --speed 1
    python previews/concurrency_preview_v3_scrollback.py --start-at 10 --paused

Use your terminal's scrollbar/mouse wheel normally. Space pauses; Q/Ctrl+C stops.
Stage completions and diagnostics append immediately, in completion order.
The animated area follows that history, without an alternate screen or mouse capture.
The final full transcript and results table are printed in the original file order.
All data is fictional; no media, decoder processes or reports are involved.
"""
import pathlib
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _concurrency_preview_v3 as shared


class InlineScreen:
    """Keep an ordinary scrollback buffer and rewrite only the small active tail."""
    def __init__(self, reporter):
        self.output = reporter.output
        self.previous = []
        self.size = None

    def __enter__(self):
        self.output.write('\033[?25l')
        self.output.flush()
        return self

    def draw(self, history, panel, columns, rows):
        width = max(1, columns - 1)
        panel = [shared.v1.fit(line, width) for line in panel]
        output = []
        if self.size is not None and self.size != (columns, rows):
            # Terminal reflow changes old row positions. Leave those as a snapshot.
            self.previous = []
            output.append('\n')
        self.size = (columns, rows)
        old_height = len(self.previous)
        if not history and panel == self.previous:
            return
        if old_height:
            output.append(f'\033[{old_height}F')
        if history or len(panel) != old_height:
            wrapped = [row for line in history for row in shared.v2.wrap_line(line, width)]
            lines = wrapped + panel
            for line in lines:
                output.append(line + '\033[0m\033[K\n')
            leftover = max(0, old_height - len(lines))
            if leftover:
                output.append('\033[K\n' * leftover)
                output.append(f'\033[{leftover}F')
        else:
            cursor_row = 0
            for row, line in enumerate(panel):
                if line == self.previous[row]:
                    continue
                if row > cursor_row:
                    output.append(f'\033[{row - cursor_row}E')
                output.append(line + '\033[0m\033[K\r')
                cursor_row = row
            if old_height > cursor_row:
                output.append(f'\033[{old_height - cursor_row}E')
        self.output.write(''.join(output))
        self.output.flush()
        self.previous = panel

    def __exit__(self, *exception):
        self.output.write('\033[0m\033[?25h')
        self.output.flush()


def main(argv=None):
    return shared.run(__doc__, False, InlineScreen, shared.PlainInput, argv)


if __name__ == '__main__':
    sys.exit(main())
