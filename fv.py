#!/usr/bin/env python3
"""fv — a read-only terminal file explorer with a live preview pane.

Left: directory listing.  Right: contents of the highlighted entry.
Only the stdlib is required; Pygments (if installed) adds syntax highlighting.

Keys (Tab or a mouse click picks the panel the keys act on)
  File list
    type letters                 jump to the first name starting with them (repeat a letter to cycle)
    Up/Down, PgUp/PgDn, Home/End move the selection; the preview follows
    Enter / Right                open directory, or focus the preview for a file
    Backspace / Left             parent directory (Backspace edits the typed name while searching)
  Preview
    Up/Down, PgUp/PgDn, Home/End scroll
    Left                         back to the file list
    mouse drag                   select text (auto-scrolls at the top/bottom edge)
  Anywhere
    Ctrl+A                       select the whole previewed file
    Ctrl+C                       copy the selection (quits if nothing is selected)
    Esc                          clear the selection / typed name; back to the file list
    Ctrl+T                       toggle hidden files
    Ctrl+L                       redraw the screen
    mouse wheel                  scroll the panel under the pointer
    Ctrl+Q                       quit
"""
import array
import base64
import curses
import locale
import os
import shutil
import stat
import subprocess
import sys
import termios
import time

CHUNK = 64 * 1024
SNIFF = 8192
MAX_LINE = 4096          # bytes of a single line we ever render
HL_CONTEXT = 200         # lines of context fed to the lexer above the viewport
TAB = 4
EOL = 1 << 60            # selection column meaning "past the end of the line" (includes newline)
AUTOSCROLL_S = 0.04      # auto-scroll tick while dragging at/beyond the preview edge
OSC52_MAX = 1 << 20      # largest selection (bytes) also sent to the terminal clipboard
TYPEAHEAD_S = 1.0        # type-to-navigate buffer resets after this much inactivity
EOF_LINE = 1 << 60       # selection line meaning "end of file" (Ctrl+A), never indexed
COPY_MAX = 100 << 20     # refuse to copy selections larger than this
# keys that scroll the preview when it has focus
PREVIEW_SCROLL = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_PPAGE: "pgup",
                  curses.KEY_NPAGE: "pgdn", curses.KEY_HOME: "home", curses.KEY_END: "end"}


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

    def raw_line(self, i):
        """Full line i (no MAX_LINE cap) as (text, original line ending)."""
        self._index_until(i + 1)
        begin = self.offsets[i]
        end = self.offsets[i + 1] if i + 1 < len(self.offsets) else self.size
        self.f.seek(begin)
        raw = self.f.read(end - begin).decode("utf-8", "replace")
        text = raw.rstrip("\r\n")
        return text, raw[len(text):]

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
        self.focus = "list"       # "list" | "preview": which panel the keys act on
        self.find_buf = ""        # type-to-navigate prefix (casefolded)
        self.find_time = 0.0
        self.folded = []          # casefolded entry names, parallel to self.entries
        self.preview = None
        self.hl = Highlighter()
        self.error = ""
        self.status = ""          # transient footer message (e.g. "copied ...")
        self.sel_anchor = None    # selection endpoints as (line, char col) in the FILE
        self.sel_cursor = None
        self.dragging = False
        self.drag_yx = (0, 0)
        self.last_autoscroll = 0.0
        self.autoscroll_ticks = 0
        self.pgeom = None         # last drawn text-preview geometry (y0, x0, ph, gw, cw)
        self.init_colors()
        self.init_mouse()
        self.init_tty()
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

    def init_mouse(self):
        try:
            curses.mouseinterval(0)   # deliver press/release immediately, no click synthesis
            avail, _ = curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            avail = 0
        self.mouse = bool(avail)
        if self.mouse:
            # ncurses only enables mode 1000 (clicks); 1002 adds motion while a button is held
            term_write("\x1b[?1002h")

    def init_tty(self):
        """Let Ctrl+Q/Ctrl+S reach the app (the tty normally eats them for flow control)."""
        try:
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            curses.def_prog_mode()    # keep it across suspend/resume; endwin restores the shell's
        except (termios.error, OSError, curses.error):
            pass

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
        self.focus = "list"
        try:
            self.entries = sorted_entries(self.cwd, self.show_hidden)
            self.error = ""
        except OSError as e:
            self.entries = []
            self.error = f"Cannot open directory: {e.strerror}"
        self.folded = [name.casefold() for name, _, _ in self.entries]
        self.find_buf = ""
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
        self.clear_selection()
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
        if not is_dir:
            self.set_focus("preview")
        if is_dir:
            target = os.path.join(self.cwd, name)
            if not os.access(target, os.R_OK | os.X_OK):
                self.error = f"Permission denied: {name}"
                return
            self.cwd = os.path.normpath(target)
            self.load_dir()

    # ---- type-to-navigate (list focus)

    def find_active(self):
        return bool(self.find_buf) and time.monotonic() - self.find_time <= TYPEAHEAD_S

    def find_from(self, prefix, start):
        """Index of the first entry at/after start (wrapping) whose name starts with prefix."""
        n = len(self.folded)
        for i in range(n):
            j = (start + i) % n
            if self.folded[j].startswith(prefix):
                return j
        return None

    def type_ahead(self, ch):
        if not self.entries:
            return
        if not self.find_active():
            self.find_buf = ""
        self.find_time = time.monotonic()
        c = ch.casefold()
        new = self.find_buf + c
        if len(new) == 1:
            # fresh letter: jump to the first match, or cycle on if already on one
            start = self.sel + 1 if self.folded[self.sel].startswith(c) else 0
            idx = self.find_from(c, start)
        else:
            idx = self.find_from(new, self.sel)            # refine, staying put if still matching
            if idx is None and new == c * len(new):
                idx = self.find_from(c, self.sel + 1)       # same letter repeated: cycle
        if idx is None:
            self.status = f"no match: {new}"
            return
        self.find_buf = new
        self.move(idx - self.sel)

    def find_backspace(self):
        self.find_buf = self.find_buf[:-1]
        self.find_time = time.monotonic()
        if self.find_buf:
            idx = self.find_from(self.find_buf, 0)
            if idx is not None:
                self.move(idx - self.sel)

    def set_focus(self, panel):
        if panel == "preview" and self.layout()[2] >= self.layout()[1]:
            return                    # too narrow: preview isn't shown
        self.focus = panel

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
                if self.focus == "list":
                    a = self.attr(9, curses.A_BOLD) if self.has_color else curses.A_REVERSE
                else:
                    a = self.attr(10) if self.has_color else curses.A_UNDERLINE
                self.put(1 + row, 0, (" " + label).ljust(lw - 1), lw - 1, a)
            else:
                a = self.attr(4) if is_dir else 0
                self.put(1 + row, 1, label, lw - 2, a)

        # divider + right pane
        if lw < w:
            div = self.attr(9, curses.A_BOLD) if self.focus == "preview" else 0
            for y in range(1, h - 1):
                try:
                    self.scr.addch(y, lw - 1, curses.ACS_VLINE, div)
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
        rng = self.selection_range()
        if self.find_active():
            left += f"  find: {self.find_buf}"
        if self.status:
            left += f"  {self.status}"
        elif rng and rng[1][0] == EOF_LINE:
            f = self.preview.file
            left += f"  sel all ({f.count()} lines)" if f.eof else f"  sel all ({human(f.size)})"
        elif rng:
            left += f"  sel {rng[1][0] - rng[0][0] + 1} lines"
        if self.focus == "list":
            keys = "type to find  ↑↓ move  ⏎ open  ⌫ up  Tab preview  ^T hidden  ^Q quit "
        else:
            keys = "↑↓ PgUp/PgDn Home/End scroll  ← Tab files  ^A all  ^C copy  ^Q quit "
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
        self.pgeom = (y0, x0, ph, gw, cw)
        rng = self.selection_range()
        sel_attr = self.attr(9) if self.has_color else curses.A_REVERSE
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
            if rng:
                self.paint_selection(y, x, cw, self.ptop + i, line, rng, sel_attr)

    # ---- mouse selection (application-managed, in file coordinates)

    def clear_selection(self):
        self.sel_anchor = self.sel_cursor = None
        self.dragging = False

    def selection_range(self):
        """((line, col), (line, col)) inclusive, ordered; None if nothing selected."""
        if self.sel_anchor is None or self.sel_cursor is None:
            return None
        a, b = sorted((self.sel_anchor, self.sel_cursor))
        return a, b

    def in_preview(self, y, x):
        g = self.pgeom
        return g is not None and g[0] <= y < g[0] + g[2] and x >= g[1]

    def file_pos(self, y, x):
        """Map a screen cell (clamped into the preview) to a (line, char col) in the file."""
        y0, x0, ph, gw, cw = self.pgeom
        p = self.preview
        row = max(0, min(ph - 1, y - y0))
        line_no = self.ptop + row
        lines = p.file.lines(line_no, 1)
        if not lines:                        # pointer below the last line of the file
            line_no = max(0, p.file.count() - 1)
            return line_no, EOL
        text = lines[0]
        if y < y0:
            dcol = 0                         # above the panel: start of the top line
        elif y >= y0 + ph:
            return line_no, EOL              # below the panel: end of the bottom line
        else:
            dcol = x - (x0 + gw)
        if dcol < 0:
            return line_no, 0
        cols = display_cols(text)
        if dcol >= cw - 1 and cols[-1] > cw:  # right edge of a clipped line: take all of it
            return line_no, EOL
        for i in range(len(text)):
            if cols[i + 1] > dcol:
                return line_no, i
        return line_no, EOL

    def paint_selection(self, y, x, cw, line_no, text, rng, attr):
        (l0, c0), (l1, c1) = rng
        if not (l0 <= line_no <= l1):
            return
        cols = display_cols(text)
        start = c0 if line_no == l0 else 0
        end = c1 if line_no == l1 else EOL   # inclusive char index
        d0 = cols[min(start, len(text))]
        d1 = cols[min(end + 1, len(text))] + (1 if end >= len(text) else 0)  # +1 cell for newline
        d0, d1 = min(d0, cw), min(d1, cw)
        if d1 > d0:
            try:
                self.scr.chgat(y, x + d0, d1 - d0, attr)
            except curses.error:
                pass

    def select_all(self):
        p = self.preview
        if p is None or p.kind != "text":
            self.status = "nothing to select"
            return
        self.dragging = False
        self.sel_anchor, self.sel_cursor = (0, 0), (EOF_LINE, EOL)

    def selection_size(self):
        """Byte size of the selection (upper bound for partial first/last lines)."""
        (l0, _), (l1, _) = self.selection_range()
        f = self.preview.file
        if l1 == EOF_LINE:
            return f.size
        f._index_until(l1 + 1)
        end = f.offsets[l1 + 1] if l1 + 1 < len(f.offsets) else f.size
        return end - f.offsets[l0]

    def selection_text(self):
        rng = self.selection_range()
        if not rng or self.preview is None or self.preview.kind != "text":
            return ""
        (l0, c0), (l1, c1) = rng
        f = self.preview.file
        out = []
        for n in range(l0, l1 + 1):
            text, ending = f.raw_line(n)
            start = c0 if n == l0 else 0
            end = c1 if n == l1 else EOL
            out.append(text[start:end + 1])
            if end >= len(text):
                out.append(ending)
        return "".join(out)

    def copy_selection(self):
        rng = self.selection_range()
        if not rng or self.preview is None or self.preview.kind != "text":
            return
        size = self.selection_size()
        if size > COPY_MAX:
            self.status = f"not copied: selection is {human(size)} (limit {human(COPY_MAX)})"
            return
        if rng[1][0] == EOF_LINE:                 # whole file: copy the bytes exactly as stored
            f = self.preview.file
            f.f.seek(0)
            data = f.f.read(COPY_MAX + 1)
            nlines = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
        else:
            data = self.selection_text().encode("utf-8")
            nlines = rng[1][0] - rng[0][0] + 1
        if not data:
            return
        how = copy_to_clipboard(data)
        what = f"{nlines} line{'s' * (nlines != 1)} ({human(len(data))})"
        if how:
            self.status = f"copied {what} → {how}"
        else:
            self.status = (f"not copied: {what} exceeds the {human(OSC52_MAX)} terminal-clipboard "
                           "limit and no clipboard tool (wl-copy/xclip/xsel) was found")

    def handle_mouse(self):
        try:
            _, x, y, _, bstate = curses.getmouse()
        except curses.error:
            return
        p = self.preview
        text_preview = p is not None and p.kind == "text"
        if bstate & curses.BUTTON1_PRESSED:
            self.set_focus("preview" if x >= self.layout()[2] else "list")
            if text_preview and self.in_preview(y, x):
                self.status = ""
                self.dragging = True
                self.drag_yx = (y, x)
                self.autoscroll_ticks = 0
                self.sel_anchor = self.sel_cursor = self.file_pos(y, x)
                self.press_pos = self.sel_anchor
                self.moved = False
            elif x < self.layout()[2] - 1 and 1 <= y < self.layout()[0] - 1:
                idx = self.top + y - 1
                if idx < len(self.entries):
                    self.move(idx - self.sel)
        elif bstate & curses.BUTTON1_RELEASED:
            if self.dragging:
                self.dragging = False
                self.drag_yx = (y, x)
                self.sel_cursor = self.file_pos(y, x)
                if not self.moved and self.sel_cursor == self.press_pos:
                    self.clear_selection()   # plain click: no selection
        elif bstate & curses.REPORT_MOUSE_POSITION:
            if self.dragging:
                self.drag_yx = (y, x)
                self.moved = True
                self.sel_cursor = self.file_pos(y, x)
        elif bstate & (curses.BUTTON4_PRESSED | getattr(curses, "BUTTON5_PRESSED", 0)):
            step = -3 if bstate & curses.BUTTON4_PRESSED else 3
            h, _, lw = self.layout()
            if x >= lw:
                self.scroll_preview(step, max(1, h - 2))
            else:
                self.move(step)

    def autoscroll(self):
        """While dragging with the pointer at/beyond the top or bottom edge, scroll + extend."""
        if not self.dragging or self.pgeom is None:
            return
        now = time.monotonic()
        if now - self.last_autoscroll < AUTOSCROLL_S:
            return
        y0, _, ph, _, _ = self.pgeom
        y, x = self.drag_yx
        if y < y0:
            direction, speed = -1, 3          # beyond the top edge (header row)
        elif y == y0:
            direction, speed = -1, 1
        elif y >= y0 + ph:
            direction, speed = 1, 3           # beyond the bottom edge (footer row)
        elif y == y0 + ph - 1:
            direction, speed = 1, 1
        else:
            self.autoscroll_ticks = 0
            return
        self.last_autoscroll = now
        self.autoscroll_ticks += 1
        if speed > 1:                         # accelerate the longer it is held beyond the edge
            speed = min(60, speed + self.autoscroll_ticks // 6)
        before = self.ptop
        self.scroll_preview(direction * speed, ph)
        if self.ptop != before:
            self.moved = True
        self.sel_cursor = self.file_pos(y, x)

    # ---- main loop

    def run(self):
        self.draw()
        while True:
            if self.dragging:
                self.scr.timeout(int(AUTOSCROLL_S * 1000))
            elif self.find_buf:               # wake up to clear the find prompt when it expires
                left = TYPEAHEAD_S - (time.monotonic() - self.find_time)
                self.scr.timeout(max(10, int(left * 1000) + 10))
            else:
                self.scr.timeout(-1)
            try:
                k = self.scr.get_wch()
            except curses.error:
                k = None                      # timeout tick while dragging
            except KeyboardInterrupt:
                k = "\x03"                    # Ctrl-C: copy if something is selected
            h, _, _ = self.layout()
            ph = max(1, h - 2)
            if k not in (None, curses.KEY_MOUSE):
                self.status = ""
            if self.find_buf and not self.find_active():
                self.find_buf = ""
            if k is None:
                pass
            elif k == curses.KEY_MOUSE:
                self.handle_mouse()
            elif k == "\x11":                 # Ctrl+Q
                break
            elif k == "\t":
                self.set_focus("preview" if self.focus == "list" else "list")
            elif (self.focus == "list" and isinstance(k, str) and k.isprintable()
                  and (k != " " or self.find_buf)):
                self.type_ahead(k)
            elif self.focus == "list" and self.find_buf and k in (
                    curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.find_backspace()
            elif self.find_buf and k == "\x1b":
                self.find_buf = ""
            elif self.focus == "preview" and k in PREVIEW_SCROLL:
                action = PREVIEW_SCROLL[k]
                if action == "home":
                    self.ptop = 0
                else:
                    step = {"up": -1, "down": 1, "pgup": -ph, "pgdn": ph, "end": "end"}[action]
                    self.scroll_preview(step, ph)
            elif self.focus == "preview" and k == curses.KEY_LEFT:
                self.set_focus("list")
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
                if self.focus == "list":
                    self.enter()
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\b", curses.KEY_LEFT):
                self.parent()
            elif k == "\x14":                 # Ctrl+T: toggle hidden files
                self.show_hidden = not self.show_hidden
                cur = self.entries[self.sel][0] if self.entries else None
                self.load_dir(focus=cur)
            elif k == curses.KEY_RESIZE:
                curses.update_lines_cols()
            elif k == "\x0c":             # Ctrl-L: full repaint
                self.scr.clear()
            elif k == "\x01":                 # Ctrl+A: select the whole file
                self.select_all()
            elif k == "\x03":
                if not self.selection_range():
                    break                     # nothing selected: Ctrl-C quits as before
                self.copy_selection()
            elif k == "\x1b":
                self.clear_selection()
                self.set_focus("list")            # back to the file list to pick another file
            self.autoscroll()
            self.draw()
        if self.preview:
            self.preview.close()


def display_cols(text):
    """cols[i] = display column where char i starts (same tab rule as sanitize); cols[-1] = width."""
    cols = [0]
    c = 0
    for ch in text:
        c += TAB - (c % TAB) if ch == "\t" else 1
        cols.append(c)
    return cols


def term_write(seq):
    try:
        os.write(sys.stdout.fileno(), seq.encode() if isinstance(seq, str) else seq)
    except OSError:
        pass


def copy_to_clipboard(data):
    """Copy bytes via a system clipboard tool if present, and via OSC 52 (works over SSH/tmux).

    Returns how it was copied, or "" if no route could take it."""
    tools = []
    if os.environ.get("WAYLAND_DISPLAY"):
        tools.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        tools += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    tools += [["pbcopy"], ["clip.exe"]]
    used = []
    for cmd in tools:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=data, timeout=2, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                used.append(cmd[0])
                break
            except (OSError, subprocess.SubprocessError):
                continue
    if len(data) <= OSC52_MAX:
        osc = b"\x1b]52;c;" + base64.b64encode(data) + b"\x07"
        if os.environ.get("TMUX"):
            osc = b"\x1bPtmux;\x1b" + osc + b"\x1b\\"
        term_write(osc)
        used.append("OSC 52")
    return " + ".join(used)


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
    finally:
        term_write("\x1b[?1002l\x1b[?1006l\x1b[?1000l")


if __name__ == "__main__":
    main()
