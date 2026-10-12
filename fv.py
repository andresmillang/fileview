#!/usr/bin/env python3
"""fv — a read-only terminal file explorer with a live preview pane.

Left: directory listing.  Right: contents of the highlighted entry.
Only the stdlib is required; Pygments (if installed) adds syntax highlighting.

Keys
  Up/Down, PgUp/PgDn, Home/End   move the selection
  Enter / Right                  open directory
  Backspace / Left               go to parent directory
  j/k  J/K  space/b  g/G         scroll preview: line, 10 lines, page, top/bottom
  .                              toggle hidden files
  q / Q                          quit
"""
import array
import curses
import locale
import os
import stat
import sys

CHUNK = 64 * 1024
SNIFF = 8192
MAX_LINE = 4096          # bytes of a single line we ever render
HL_CONTEXT = 200         # lines of context fed to the lexer above the viewport
TAB = 4


# ---------------------------------------------------------------- file access

class LazyFile:
    """Line-addressable view over a file that only reads as far as needed.

    Line start offsets are indexed incrementally, so scrolling to line N reads
    roughly the first N lines once; the full file is never held in memory.
    """

    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.fstat(self.f.fileno()).st_size
        self.offsets = array.array("q", [0])   # 8 bytes/line, not ~36
        self.scanned = 0          # bytes indexed so far
        self.eof = self.size == 0

    def close(self):
        self.f.close()

    def _index_until(self, n):
        while len(self.offsets) <= n and not self.eof:
            self.f.seek(self.scanned)
            buf = self.f.read(CHUNK)
            if not buf:
                self.eof = True
                break
            base = self.scanned
            i = buf.find(b"\n")
            while i != -1:
                self.offsets.append(base + i + 1)
                i = buf.find(b"\n", i + 1)
            self.scanned += len(buf)
        if self.eof and self.offsets[-1] >= self.size and len(self.offsets) > 1:
            self.offsets.pop()    # trailing newline doesn't start a real line

    def line_count_known(self):
        return self.eof

    def count(self):
        """Lines indexed so far (exact once eof is reached)."""
        return len(self.offsets)

    def lines(self, start, n):
        self._index_until(start + n)
        out = []
        for i in range(start, min(start + n, len(self.offsets))):
            begin = self.offsets[i]
            end = self.offsets[i + 1] if i + 1 < len(self.offsets) else self.size
            self.f.seek(begin)
            raw = self.f.read(min(end - begin, MAX_LINE))
            out.append(raw.rstrip(b"\r\n").decode("utf-8", "replace"))
        return out

    def index_all(self):
        self._index_until(sys.maxsize)


def sniff_binary(path):
    with open(path, "rb") as f:
        head = f.read(SNIFF)
    if b"\0" in head:
        return True
    if not head:
        return False
    try:
        head.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        if e.start >= len(head) - 4:      # multibyte char cut by the sniff window
            return False
    text = bytes(range(32, 127)) + b"\n\r\t\f\b\x1b"
    nontext = sum(1 for b in head if b not in text and b < 128)
    return nontext / len(head) > 0.30


def human(n):
    for unit in "BKMGT":
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


# ---------------------------------------------------------------- highlighting

class Highlighter:
    """Optional Pygments wrapper; degrades to plain text if unavailable."""

    def __init__(self):
        self.ok = None
        self.cache = {}

    def _load(self):
        try:
            from pygments import lex
            from pygments.lexers import get_lexer_for_filename
            from pygments.token import Token
            from pygments.util import ClassNotFound
            self._lex, self._get, self._T, self._CNF = lex, get_lexer_for_filename, Token, ClassNotFound
            self.ok = True
        except ImportError:
            self.ok = False

    def lexer_for(self, path, sample):
        if self.ok is None:
            self._load()
        if not self.ok:
            return None
        if path not in self.cache:
            try:
                lx = self._get(os.path.basename(path), code=sample, stripnl=False, ensurenl=False)
                self.cache[path] = None if lx.name == "Text only" else lx
            except self._CNF:
                self.cache[path] = None
            except Exception:
                self.cache[path] = None
        return self.cache[path]

    def color_for(self, ttype):
        T = self._T
        for base, pair in (
            (T.Comment, 6), (T.Literal.String, 3), (T.Literal.Number, 5),
            (T.Keyword, 4), (T.Name.Builtin, 7), (T.Name.Function, 2),
            (T.Name.Class, 2), (T.Name.Decorator, 7), (T.Name.Tag, 4),
            (T.Name.Attribute, 2), (T.Operator.Word, 4), (T.Generic.Heading, 4),
            (T.Generic.Subheading, 4), (T.Generic.Inserted, 3), (T.Generic.Deleted, 8),
            (T.Error, 8),
        ):
            if ttype in base:
                return pair
        return 0

    def highlight(self, lexer, lines):
        """Return a list of [(text, pair), ...] per input line."""
        src = "\n".join(lines) + "\n"
        out, cur = [], []
        for ttype, value in self._lex(src, lexer):
            pair = self.color_for(ttype)
            parts = value.split("\n")
            for j, part in enumerate(parts):
                if j:
                    out.append(cur)
                    cur = []
                if part:
                    cur.append((part, pair))
        if cur:
            out.append(cur)
        while len(out) < len(lines):
            out.append([])
        return out[: len(lines)]


# ---------------------------------------------------------------- preview model

class Preview:
    """What the right pane shows for one path."""

    def __init__(self, path, hl):
        self.path = path
        self.kind = "message"      # message | text | dir
        self.message = []
        self.file = None
        self.lexer = None
        self.entries = []
        self.info = ""
        try:
            st = os.stat(path)
        except OSError as e:
            self.message = [f"Cannot stat: {e.strerror}"]
            return
        if stat.S_ISDIR(st.st_mode):
            self.kind = "dir"
            try:
                self.entries = sorted_entries(path, show_hidden=True)
                self.info = f"{len(self.entries)} entries"
            except OSError as e:
                self.kind = "message"
                self.message = [f"Cannot read directory: {e.strerror}"]
            return
        if not stat.S_ISREG(st.st_mode):
            self.message = ["Not a regular file", "(device, socket, or FIFO — not previewed)"]
            return
        self.info = human(st.st_size)
        try:
            if sniff_binary(path):
                self.message = ["Binary file — no preview", f"size: {human(st.st_size)}"]
                return
            self.file = LazyFile(path)
        except OSError as e:
            self.message = [f"Cannot read file: {e.strerror}"]
            return
        if st.st_size == 0:
            self.message = ["(empty file)"]
            return
        self.kind = "text"
        sample = "\n".join(self.file.lines(0, 50))
        self.lexer = hl.lexer_for(path, sample)
        if self.lexer is not None:
            self.info += f" · {self.lexer.name}"

    def close(self):
        if self.file:
            self.file.close()


# ---------------------------------------------------------------- directory model

def sorted_entries(path, show_hidden):
    items = []
    with os.scandir(path) as it:
        for e in it:
            if not show_hidden and e.name.startswith("."):
                continue
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            items.append((not is_dir, e.name.lower(), e.name, is_dir, e.is_symlink()))
    items.sort()
    return [(name, is_dir, is_link) for _, _, name, is_dir, is_link in items]


# ---------------------------------------------------------------- the app

class App:
    def __init__(self, scr, start):
        self.scr = scr
        self.cwd = os.path.abspath(start)
        self.show_hidden = False
        self.entries = []
        self.sel = 0
        self.top = 0
        self.ptop = 0             # preview scroll
        self.preview = None
        self.hl = Highlighter()
        self.error = ""
        self.init_colors()
        self.load_dir()

    def init_colors(self):
        curses.curs_set(0)
        self.has_color = curses.has_colors()
        if self.has_color:
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            pairs = [curses.COLOR_WHITE, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                     curses.COLOR_BLUE, curses.COLOR_MAGENTA, curses.COLOR_CYAN,
                     curses.COLOR_CYAN, curses.COLOR_RED]
            for i, fg in enumerate(pairs, start=1):
                curses.init_pair(i, fg, bg)
            curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_CYAN)    # selection
            curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_WHITE)  # bars

    def attr(self, pair, extra=0):
        if not self.has_color or pair == 0:
            return extra
        a = curses.color_pair(pair) | extra
        if pair == 4:
            a |= curses.A_BOLD
        if pair == 6:
            a |= curses.A_DIM
        return a

    # ---- state changes

    def load_dir(self, focus=None):
        try:
            self.entries = sorted_entries(self.cwd, self.show_hidden)
            self.error = ""
        except OSError as e:
            self.entries = []
            self.error = f"Cannot open directory: {e.strerror}"
        self.sel = 0
        if focus:
            for i, (name, _, _) in enumerate(self.entries):
                if name == focus:
                    self.sel = i
                    break
        self.top = 0
        self.refresh_preview()

    def refresh_preview(self):
        if self.preview:
            self.preview.close()
        self.ptop = 0
        if self.entries:
            self.preview = Preview(os.path.join(self.cwd, self.entries[self.sel][0]), self.hl)
        else:
            self.preview = None

    def move(self, delta):
        if not self.entries:
            return
        new = max(0, min(len(self.entries) - 1, self.sel + delta))
        if new != self.sel:
            self.sel = new
            self.refresh_preview()

    def enter(self):
        if not self.entries:
            return
        name, is_dir, _ = self.entries[self.sel]
        if is_dir:
            target = os.path.join(self.cwd, name)
            if not os.access(target, os.R_OK | os.X_OK):
                self.error = f"Permission denied: {name}"
                return
            self.cwd = os.path.normpath(target)
            self.load_dir()

    def parent(self):
        parent = os.path.dirname(self.cwd)
        if parent != self.cwd:
            came_from = os.path.basename(self.cwd)
            self.cwd = parent
            self.load_dir(focus=came_from)

    def preview_lines_total(self):
        p = self.preview
        if not p:
            return 0
        if p.kind == "text":
            return p.file.count()
        if p.kind == "dir":
            return len(p.entries)
        return len(p.message)

    def scroll_preview(self, delta, ph):
        p = self.preview
        if not p:
            return
        if delta == "end":
            if p.kind == "text":
                p.file.index_all()
            self.ptop = max(0, self.preview_lines_total() - ph)
            return
        new = max(0, self.ptop + delta)
        if p.kind == "text":
            p.file._index_until(new + ph)
        self.ptop = max(0, min(new, self.preview_lines_total() - ph)) if delta > 0 else new

    # ---- drawing

    def put(self, y, x, text, width, attr=0):
        if width <= 0:
            return
        try:
            self.scr.addnstr(y, x, text, width, attr)
        except curses.error:
            pass  # writing into the bottom-right cell raises; harmless

    def layout(self):
        h, w = self.scr.getmaxyx()
        lw = max(16, min(w // 3, 48))
        if w < 40:
            lw = w  # too narrow for two panes: list only
        return h, w, lw

    def draw(self):
        self.scr.erase()
        h, w, lw = self.layout()
        body = h - 2
        if body < 1:
            self.scr.refresh()
            return

        # header
        hdr = f" {self.cwd}"
        self.put(0, 0, hdr.ljust(w), w, self.attr(10, curses.A_BOLD))

        # left pane
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + body:
            self.top = self.sel - body + 1
        if not self.entries:
            self.put(1, 1, self.error or "(empty)", lw - 1, self.attr(8 if self.error else 6))
        for row, i in enumerate(range(self.top, min(len(self.entries), self.top + body))):
            name, is_dir, is_link = self.entries[i]
            label = name + ("/" if is_dir else "") + ("@" if is_link and not is_dir else "")
            if i == self.sel:
                a = self.attr(9, curses.A_BOLD) if self.has_color else curses.A_REVERSE
                self.put(1 + row, 0, (" " + label).ljust(lw - 1), lw - 1, a)
            else:
                a = self.attr(4) if is_dir else 0
                self.put(1 + row, 1, label, lw - 2, a)

        # divider + right pane
        if lw < w:
            for y in range(1, h - 1):
                try:
                    self.scr.addch(y, lw - 1, curses.ACS_VLINE)
                except curses.error:
                    pass
            self.draw_preview(1, lw + 1, body, w - lw - 1)

        # footer
        info = ""
        if self.preview:
            total = self.preview_lines_total()
            more = "" if (self.preview.kind != "text" or self.preview.file.eof) else "+"
            info = f"{self.preview.info}  ln {self.ptop + 1}/{total}{more}"
        left = f" {self.sel + 1 if self.entries else 0}/{len(self.entries)}"
        if self.error and self.entries:
            left += f"  {self.error}"
        keys = "↑↓ move  ⏎ open  ⌫ up  j/k J/K spc/b g/G scroll  . hidden  q quit "
        self.put(h - 1, 0, " " * w, w, self.attr(10))
        self.put(h - 1, 0, left, w, self.attr(10))
        mid = f"  {info}  "
        self.put(h - 1, len(left), mid, max(0, w - len(left)), self.attr(10, curses.A_BOLD))
        start = w - len(keys)
        if start > len(left) + len(mid):
            self.put(h - 1, start, keys, len(keys) - 1, self.attr(10))
        self.scr.refresh()

    def draw_preview(self, y0, x0, ph, pw):
        p = self.preview
        if pw <= 0 or p is None:
            return
        if p.kind == "message":
            for i, line in enumerate(p.message[self.ptop:self.ptop + ph]):
                self.put(y0 + 1 + i, x0, line, pw,
                         self.attr(6, curses.A_BOLD) if i == 0 else self.attr(6))
            return
        if p.kind == "dir":
            for i, (name, is_dir, _) in enumerate(p.entries[self.ptop:self.ptop + ph]):
                self.put(y0 + i, x0, name + ("/" if is_dir else ""), pw,
                         self.attr(4) if is_dir else 0)
            if not p.entries:
                self.put(y0, x0, "(empty directory)", pw, self.attr(6))
            return

        # text: line-number gutter + (optionally highlighted) contents
        lines = p.file.lines(self.ptop, ph)
        gw = len(str(self.ptop + ph)) + 1
        cw = pw - gw - 1
        rendered = None
        if p.lexer is not None and lines:
            ctx = max(0, self.ptop - HL_CONTEXT)
            before = p.file.lines(ctx, self.ptop - ctx) if ctx < self.ptop else []
            try:
                rendered = self.hl.highlight(p.lexer, before + lines)[len(before):]
            except Exception:
                rendered = None
        for i, line in enumerate(lines):
            y = y0 + i
            self.put(y, x0, str(self.ptop + i + 1).rjust(gw - 1), gw - 1, self.attr(6))
            if cw <= 0:
                continue
            segs = rendered[i] if rendered else [(line, 0)]
            x = x0 + gw
            col = 0
            for text, pair in segs:
                text = sanitize(text, col)
                if not text:
                    continue
                room = cw - col
                if room <= 0:
                    break
                self.put(y, x + col, text, room, self.attr(pair))
                col += len(text)

    # ---- main loop

    def run(self):
        self.draw()
        while True:
            k = self.scr.get_wch()
            h, _, _ = self.layout()
            ph = max(1, h - 2)
            if k in ("q", "Q"):
                break
            elif k == curses.KEY_UP:
                self.move(-1)
            elif k == curses.KEY_DOWN:
                self.move(1)
            elif k == curses.KEY_PPAGE:
                self.move(-ph)
            elif k == curses.KEY_NPAGE:
                self.move(ph)
            elif k == curses.KEY_HOME:
                self.move(-len(self.entries))
            elif k == curses.KEY_END:
                self.move(len(self.entries))
            elif k in ("\n", "\r", curses.KEY_ENTER, curses.KEY_RIGHT):
                self.enter()
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\b", curses.KEY_LEFT):
                self.parent()
            elif k == "j":
                self.scroll_preview(1, ph)
            elif k == "k":
                self.scroll_preview(-1, ph)
            elif k == "J":
                self.scroll_preview(10, ph)
            elif k == "K":
                self.scroll_preview(-10, ph)
            elif k == " ":
                self.scroll_preview(ph, ph)
            elif k == "b":
                self.scroll_preview(-ph, ph)
            elif k == "g":
                self.ptop = 0
            elif k == "G":
                self.scroll_preview("end", ph)
            elif k == ".":
                self.show_hidden = not self.show_hidden
                cur = self.entries[self.sel][0] if self.entries else None
                self.load_dir(focus=cur)
            elif k == curses.KEY_RESIZE:
                curses.update_lines_cols()
            self.draw()
        if self.preview:
            self.preview.close()


def sanitize(text, col):
    """Expand tabs relative to the current column and drop control chars."""
    if "\t" in text:
        out = []
        c = col
        for ch in text:
            if ch == "\t":
                n = TAB - (c % TAB)
                out.append(" " * n)
                c += n
            else:
                out.append(ch)
                c += 1
        text = "".join(out)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        text = "".join(ch if 32 <= ord(ch) != 127 else "·" for ch in text)
    return text


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__.strip())
        print("\nusage: fv [DIRECTORY]")
        return
    start = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    if not os.path.isdir(start):
        sys.exit(f"fv: not a directory: {start}")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(lambda scr: App(scr, start).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
