#!/usr/bin/env python3
"""fv — a terminal file explorer with a live, editable preview pane.

Left: directory listing.  Right: contents of the highlighted entry.
Only the stdlib is required; Pygments (if installed) adds syntax highlighting.

Keys (Tab or a mouse click picks the panel the keys act on)
  File list
    type letters                 jump to the first name starting with them (repeat a letter to cycle)
    Up/Down, PgUp/PgDn, Home/End move the selection; the preview follows
    Enter / Right                open directory, or focus the preview for a file
    Backspace / Left             parent directory (Backspace edits the typed name while searching)
    Space / Shift+Up/Down        mark entries (Ctrl+click too); Esc clears the marks
    Del                          delete the marked entries (or the highlighted one) after a y/n question;
                                 folders are only deleted when empty
  Preview (text cursor)
    arrows, PgUp/PgDn            move the cursor; Home/End = line start/end, Ctrl+Home/End = file
    Shift + any of those         select text
    mouse click / drag           place the cursor / select text (auto-scrolls at the edges)
    type, Enter, Tab             insert text      Backspace / Delete   delete
    Ctrl+V                       paste            Ctrl+S               save
    Ctrl+Z                       undo (typing is undone a run at a time)
    Ctrl+F                       find text (case-insensitive); F3 or Ctrl+G = next match,
                                 Shift+F3 = previous match
    Ctrl+R                       run the highlighted .py file with python3; its output replaces the
                                 preview (Ctrl+R again while it runs: stop it)
    Esc                          back to the file list (asks to save if there are unsaved changes)
  Anywhere
    Ctrl+A                       select the whole previewed file
    Ctrl+C                       copy the selected text (in the preview; quits if nothing is selected)
    Esc                          clear the selection / typed name; back to the file list
    Ctrl+N                       create a new file in this folder
    Ctrl+D / F7                  create a new folder in this folder
    Ctrl+E / F2                  rename the highlighted file or folder (Ctrl+U clears the name)
    Ctrl+C / Ctrl+V (file list)  copy the marked (or highlighted) entries / paste them into this folder
    Ctrl+X (file list)           cut: the next Ctrl+V moves the entries into that folder
                                 if a name already exists it asks: replace, keep both ("name (copy)" /
                                 "name (2)" for moves), skip, or cancel
    Ctrl+H / F1                  show this list of keys in the preview (again: close it)
    Ctrl+T                       toggle hidden files
    Ctrl+L                       redraw the screen
    mouse wheel                  scroll the panel under the pointer
    Ctrl+Q                       quit
"""
import array
import base64
import codecs
import curses
import errno
import locale
import os
import re
import shutil
import signal
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
EDIT_MAX = 10 << 20      # files up to this size (UTF-8 text) can be edited
OUTPUT_MAX = 10 << 20    # keep at most this much output of a program run with Ctrl+R
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

    def span_size(self, l0, l1):
        self._index_until(l1 + 1)
        end = self.offsets[l1 + 1] if l1 + 1 < len(self.offsets) else self.size
        return end - self.offsets[l0]

    def read_all(self):
        self.f.seek(0)
        return self.f.read(COPY_MAX + 1)


class EditBuffer:
    """Editable in-memory copy of a UTF-8 text file, with the same read API as LazyFile.

    Each line keeps its own original ending (\\n, \\r\\n or none) so saving is byte-faithful."""

    def __init__(self, lazy):
        text = lazy.read_all().decode("utf-8")          # UnicodeDecodeError -> not editable
        parts = text.split("\n")
        self.text, self.ends = [], []
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last and part == "" and i > 0:
                break                                  # file ended with a newline
            body = part[:-1] if part.endswith("\r") and not last else part
            self.text.append(body)
            self.ends.append("" if last else part[len(body):] + "\n")
        self.newline = "\r\n" if self.ends and self.ends[0] == "\r\n" else "\n"
        self.eof = True
        self.dirty = False
        self.history = []        # undo groups: {"before": cursor, "after": cursor, "recs": [...], "typing": bool}
        self.group = None
        self.save_point = None   # history top at the last save (None = unedited file)

    size = property(lambda self: len(self.to_bytes()))

    def close(self):
        pass

    def _index_until(self, n):
        pass

    def index_all(self):
        pass

    def count(self):
        return len(self.text)

    def lines(self, start, n):
        return [t[:MAX_LINE] for t in self.text[start:start + n]]

    def raw_line(self, i):
        return self.text[i], self.ends[i]

    def span_size(self, l0, l1):
        return sum(len((self.text[i] + self.ends[i]).encode()) for i in range(l0, l1 + 1))

    def to_bytes(self):
        return "".join(t + e for t, e in zip(self.text, self.ends)).encode("utf-8")

    read_all = to_bytes

    def insert(self, pos, s):
        """Insert s at (line, col); returns the position just after it."""
        l, c = pos
        pieces = s.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        before, after, end = self.text[l][:c], self.text[l][c:], self.ends[l]
        self.record(l, l, len(pieces))
        self.dirty = True
        if len(pieces) == 1:
            self.text[l] = before + pieces[0] + after
            return l, c + len(pieces[0])
        nl = end or self.newline       # new line breaks match this line's (or the file's) style
        self.text[l:l + 1] = [before + pieces[0]] + pieces[1:-1] + [pieces[-1] + after]
        self.ends[l:l + 1] = [nl] * (len(pieces) - 1) + [end]
        return l + len(pieces) - 1, len(pieces[-1])

    def begin(self, cursor, typing):
        """Start an undo step; consecutive typing at the cursor joins the previous step."""
        top = self.history[-1] if self.history else None
        if typing and top and top["typing"] and top["after"] == cursor and top is not self.save_point:
            self.group = top
        else:
            self.group = {"before": cursor, "after": cursor, "recs": [], "typing": typing}
            self.history.append(self.group)

    def end(self, cursor):
        self.group["after"] = cursor
        if not self.group["recs"]:
            self.history.remove(self.group)           # nothing changed: no empty undo step
        self.group = None

    def record(self, l0, l1, new_count):
        """Remember lines l0..l1 before they are replaced by new_count lines."""
        if self.group is not None:
            self.group["recs"].append((l0, self.text[l0:l1 + 1], self.ends[l0:l1 + 1], new_count))

    def undo(self):
        """Revert the last step exactly; returns the cursor position before it."""
        g = self.history.pop()
        for l0, text, ends, n in reversed(g["recs"]):
            self.text[l0:l0 + n] = text
            self.ends[l0:l0 + n] = ends
        self.dirty = (self.history[-1] if self.history else None) is not self.save_point
        return g["before"]

    def delete(self, lo, hi):
        (l0, c0), (l1, c1) = lo, hi
        self.record(l0, l1, 1)
        self.text[l0:l1 + 1] = [self.text[l0][:c0] + self.text[l1][c1:]]
        self.ends[l0:l1 + 1] = [self.ends[l1]]
        self.dirty = True


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

class OutputText:
    """Output of a program run with Ctrl+R, growing as it arrives; same read API as EditBuffer."""

    def __init__(self):
        self.text = [""]
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.size = 0
        self.eof = True

    def feed(self, data):
        self.size += len(data)
        parts = self.decoder.decode(data).replace("\r\n", "\n").split("\n")
        self.text[-1] += parts[0]
        self.text.extend(parts[1:])

    def close(self):
        pass

    def _index_until(self, n):
        pass

    def index_all(self):
        pass

    def count(self):
        return len(self.text) - (1 if len(self.text) > 1 and self.text[-1] == "" else 0)

    def lines(self, start, n):
        return [t[:MAX_LINE] for t in self.text[start:min(start + n, self.count())]]

    def raw_line(self, i):
        return self.text[i], "\n" if i < len(self.text) - 1 else ""

    def span_size(self, l0, l1):
        return sum(len((t + e).encode()) for t, e in map(self.raw_line, range(l0, l1 + 1)))

    def read_all(self):
        return "\n".join(self.text).encode("utf-8")


class Preview:
    """What the right pane shows for one path."""

    output = False             # True: showing what the program printed (Ctrl+R), not the file
    readonly = None            # why this view can't be edited (program output, key help)

    @classmethod
    def text_view(cls, path, info, readonly):
        """A read-only text view that isn't the file at path (program output, key help)."""
        p = cls.__new__(cls)
        p.path, p.kind, p.message, p.entries, p.lexer = path, "text", [], [], None
        p.file, p.info, p.readonly = OutputText(), info, readonly
        return p

    @classmethod
    def program_output(cls, path):
        p = cls.text_view(path, f"running {os.path.basename(path)}",
                          "this is the program's output (read-only); Ctrl+R runs it again")
        p.output = True
        return p

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
        self.kind = "text"
        if st.st_size == 0:
            self.info += " (empty)"
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
        self.cur = (0, 0)         # text cursor in the preview, (line, col) in the file
        self.kanchor = None       # where a Shift+arrow / mouse selection started
        self.last_find = ""       # Ctrl+F text, reused by F3 / Shift+F3
        self.proc = None          # Python program started with Ctrl+R, while it runs
        self.goal = None          # column Up/Down try to keep
        self.confirm = None       # action (key) waiting on the "save changes? y/n" answer
        self.prompt = None        # footer text input: [label, text, on_enter] (Ctrl+N, F2)
        self.marked = set()       # names marked in the file list (Space / Shift+arrows / Ctrl+click)
        self.pending_delete = None  # names waiting on the "Delete …? y/n" answer
        self.file_clip = []       # absolute paths copied with Ctrl+C (or cut with Ctrl+X) in the file list
        self.clip_cut = False     # True: the next paste moves file_clip instead of copying it
        self.pasting = None       # paste in progress, waiting on "already exists: r/k/s" (see paste_next)
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
            # questions to the user: pure black on bright yellow (palette 16/226 when available —
            # themes don't remap those); no bold, which many terminals turn into grey text
            if curses.COLORS >= 256:
                curses.init_pair(11, 16, 226)
            else:
                curses.init_pair(11, curses.COLOR_BLACK, curses.COLOR_YELLOW)

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
        """Let Ctrl+Q/Ctrl+S/Ctrl+V/Ctrl+Z reach the app (the tty normally eats them)."""
        try:
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~termios.IXON
            attrs[3] &= ~termios.IEXTEN
            try:
                vdisable = os.fpathconf(fd, "PC_VDISABLE")
            except (OSError, ValueError):
                vdisable = 0
            attrs[6][termios.VSUSP] = bytes([vdisable & 0xFF])   # Ctrl+Z = undo, not suspend
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
        if os.path.dirname(self.cwd) != self.cwd:
            self.entries.insert(0, ("..", True, False))   # "../" = go one folder up
        self.folded = [name.casefold() for name, _, _ in self.entries]
        self.find_buf = ""
        self.marked = set()
        self.sel = 1 if len(self.entries) > 1 and self.entries[0][0] == ".." else 0
        if focus:
            for i, (name, _, _) in enumerate(self.entries):
                if name == focus:
                    self.sel = i
                    break
        self.top = 0
        self.refresh_preview()

    def refresh_preview(self):
        self.stop_program()
        if self.preview:
            self.preview.close()
        self.ptop = 0
        self.clear_selection()
        self.cur, self.kanchor, self.goal = (0, 0), None, None
        if self.entries:
            self.preview = Preview(os.path.join(self.cwd, self.entries[self.sel][0]), self.hl)
        else:
            self.preview = None

    def unsaved(self):
        p = self.preview
        return p is not None and isinstance(p.file, EditBuffer) and p.file.dirty

    def blocked(self):
        if self.unsaved():
            self.status = "unsaved changes: Ctrl+S to save, or Esc to save or discard them"
            return True
        return False

    def move(self, delta):
        if not self.entries or self.blocked():
            return
        new = max(0, min(len(self.entries) - 1, self.sel + delta))
        if new != self.sel:
            self.sel = new
            self.refresh_preview()

    def enter(self):
        if not self.entries or self.blocked():
            return
        name, is_dir, _ = self.entries[self.sel]
        if name == "..":
            return self.parent()
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
        if self.blocked():
            return
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
            if name in self.marked:
                label = "✓ " + label
            if i == self.sel:
                if self.focus == "list":
                    a = self.attr(9, curses.A_BOLD) if self.has_color else curses.A_REVERSE
                else:
                    a = self.attr(10) if self.has_color else curses.A_UNDERLINE
                self.put(1 + row, 0, (" " + label).ljust(lw - 1), lw - 1, a)
            else:
                a = self.attr(5, curses.A_BOLD) if name in self.marked else (self.attr(4) if is_dir else 0)
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
            if self.unsaved():
                info += "  [modified]"
        left = f" {self.sel + 1 if self.entries else 0}/{len(self.entries)}"
        if self.error and self.entries:
            left += f"  {self.error}"
        rng = self.selection_range()
        if self.marked:
            left += f"  {len(self.marked)} marked"
        if self.find_active():
            left += f"  find: {self.find_buf}"
        if self.status:
            left += f"  {self.status}"
        elif rng and rng[1][0] == EOF_LINE:
            f = self.preview.file
            left += f"  sel all ({f.count()} lines)" if f.eof else f"  sel all ({human(f.size)})"
        elif rng:
            left += f"  sel {rng[1][0] - rng[0][0] + 1} lines"
        run = "^R stop  " if self.proc else "^R run  " if self.preview and self.preview.path.endswith(".py") else ""
        if self.focus == "list":
            keys = run + "Spc mark  ^C copy  ^X cut  ^V paste  Del delete  ^E rename  ^N new  ^D folder  ^H help  ^Q quit "
        else:
            keys = run + "^S save  ⇧ select  ^A all  ^F find  ^C copy  ^V paste  ^Z undo  Esc back  ^H help  ^Q quit "
        self.put(h - 1, 0, " " * w, w, self.attr(10))
        self.put(h - 1, 0, left, w, self.attr(10))
        mid = f"  {info}  "
        self.put(h - 1, len(left), mid, max(0, w - len(left)), self.attr(10, curses.A_BOLD))
        hints = keys.split("  ")              # too narrow: drop hints right to left, keeping help + quit
        while len(hints) > 2 and w - len(keys) <= len(left) + len(mid):
            del hints[-3]
            keys = "  ".join(hints)
        start = w - len(keys)
        if start > len(left) + len(mid):
            self.put(h - 1, start, keys, len(keys) - 1, self.attr(10))
        if self.prompt is not None or self.confirm is not None or self.pending_delete or self.pasting:  # a question
            ask = self.prompt[0] + self.prompt[1] if self.prompt is not None else self.status
            loud = curses.color_pair(11) if self.has_color else curses.A_REVERSE | curses.A_BOLD
            self.put(h - 1, 0, (" " + ask).ljust(w), w, loud)
        self.place_cursor()
        self.scr.refresh()

    def place_cursor(self):
        show = False
        p, g = self.preview, self.pgeom
        if self.prompt is not None:
            h, w = self.scr.getmaxyx()
            try:
                self.scr.move(h - 1, min(w - 1, 1 + len(self.prompt[0] + self.prompt[1])))
                show = True
            except curses.error:
                pass
        elif self.focus == "preview" and p is not None and p.kind == "text" and g:
            y0, x0, ph, gw, cw = g
            l, c = self.cur
            if self.ptop <= l < self.ptop + ph and cw > 0:
                text = p.file.lines(l, 1)[0] if p.file.lines(l, 1) else ""
                x = x0 + gw + min(display_cols(text)[min(c, len(text))], cw - 1)
                try:
                    self.scr.move(y0 + l - self.ptop, x)
                    show = True
                except curses.error:
                    pass
        try:
            curses.curs_set(1 if show else 0)
        except curses.error:
            pass

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
        self.set_focus("preview")      # so Ctrl+C next copies the text (in the list it copies files)

    def selection_size(self):
        """Byte size of the selection (upper bound for partial first/last lines)."""
        (l0, _), (l1, _) = self.selection_range()
        f = self.preview.file
        return f.size if l1 == EOF_LINE else f.span_size(l0, l1)

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
            data = self.preview.file.read_all()
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

    # ---- text cursor, Shift+arrow selection, editing

    def line_len(self, l):
        return len(self.preview.file.raw_line(l)[0])

    def has_line(self, l):
        return l >= 0 and bool(self.preview.file.lines(l, 1))

    def clamp(self, pos):
        l, c = pos
        f = self.preview.file
        if not self.has_line(l):
            f.index_all()
            l = max(0, f.count() - 1)
        return l, max(0, min(c, self.line_len(l)))

    def set_cursor(self, pos, ph, extend=False):
        if extend:
            if self.kanchor is None or not self.selection_range():
                self.kanchor = self.cur
        else:
            self.clear_selection()
        self.cur = self.clamp(pos)
        if extend:
            lo, hi = sorted((self.kanchor, self.cur))
            if lo == hi:
                self.clear_selection()
            else:                      # selection stores inclusive chars: end = char before hi
                self.sel_anchor = lo
                self.sel_cursor = (hi[0], hi[1] - 1) if hi[1] else (hi[0] - 1, EOL)
        l = self.cur[0]
        if l < self.ptop:
            self.ptop = l
        elif l >= self.ptop + ph:
            self.ptop = l - ph + 1

    def cursor_key(self, k, ph):
        """Move the cursor for an arrow/page/home/end key (+Shift extends). True if handled."""
        name = curses.keyname(k).decode() if isinstance(k, int) else ""
        moves = {"KEY_LEFT": "left", "KEY_SLEFT": "left", "KEY_RIGHT": "right", "KEY_SRIGHT": "right",
                 "KEY_UP": "up", "KEY_SR": "up", "KEY_DOWN": "down", "KEY_SF": "down",
                 "KEY_PPAGE": "pgup", "KEY_SPREVIOUS": "pgup", "KEY_NPAGE": "pgdn", "KEY_SNEXT": "pgdn",
                 "KEY_HOME": "home", "KEY_SHOME": "home", "KEY_END": "end", "KEY_SEND": "end",
                 "kHOM5": "top", "kHOM6": "top", "kEND5": "bottom", "kEND6": "bottom"}
        if name not in moves:
            return False
        extend = name.startswith("KEY_S") or name.endswith("6")
        l, c = self.cur
        m = moves[name]
        if m in ("up", "down", "pgup", "pgdn"):
            self.goal = c if self.goal is None else self.goal
            dl = {"up": -1, "down": 1, "pgup": -ph, "pgdn": ph}[m]
            if m in ("pgup", "pgdn"):
                self.scroll_preview(dl, ph)
            pos = (max(0, l + dl), self.goal)
        else:
            self.goal = None
            if m == "left":
                pos = (l, c - 1) if c > 0 else ((l - 1, EOL) if l > 0 else (0, 0))
            elif m == "right":
                pos = (l, c + 1) if c < self.line_len(l) else ((l + 1, 0) if self.has_line(l + 1) else (l, c))
            elif m == "home":
                pos = (l, 0)
            elif m == "end":
                pos = (l, EOL)
            elif m == "top":
                pos = (0, 0)
            else:
                pos = (EOF_LINE, EOL)
        self.set_cursor(pos, ph, extend)
        return True

    def edit_buffer(self):
        """The preview's EditBuffer, created on first edit; None (with a reason) if not editable."""
        p = self.preview
        if p is None or p.kind != "text":
            self.status = "nothing to edit here"
            return None
        if isinstance(p.file, EditBuffer):
            return p.file
        if p.readonly:
            self.status = p.readonly
            return None
        if p.file.size > EDIT_MAX:
            self.status = f"too large to edit ({human(p.file.size)}, limit {human(EDIT_MAX)})"
        elif not os.access(p.path, os.W_OK):
            self.status = "read-only: no permission to write this file"
        else:
            try:
                buf = EditBuffer(p.file)
            except UnicodeDecodeError:
                self.status = "not UTF-8 text: editing disabled to avoid corrupting it"
                return None
            p.file.close()
            p.file = buf
            return buf
        return None

    def selection_span(self):
        """Selection as half-open positions (lo, hi) in the buffer, or None."""
        rng = self.selection_range()
        if not rng:
            return None
        f = self.preview.file
        (l0, c0), (l1, c1) = rng
        if l1 == EOF_LINE:
            l1 = f.count() - 1
        lo = (l0, min(c0, self.line_len(l0)))
        if c1 < self.line_len(l1):
            hi = (l1, c1 + 1)
        else:                          # through the end of the line: include its newline
            hi = (l1 + 1, 0) if l1 + 1 < f.count() else (l1, self.line_len(l1))
        return lo, hi

    def edit(self, ph, text=None, back=False, forward=False):
        """Insert text, or delete back/forward; a selection is replaced/deleted first."""
        buf = self.edit_buffer()
        if buf is None:
            return
        span = self.selection_span()
        self.clear_selection()
        self.kanchor = self.goal = None
        l, c = self.cur = self.clamp(self.cur)
        typing = bool(text) and len(text) == 1 and text != "\n" and not span and not (back or forward)
        buf.begin(self.cur, typing)
        if span and span[0] != span[1]:
            buf.delete(*span)
            self.cur = span[0]
        elif back:
            if c > 0:
                buf.delete((l, c - 1), (l, c)); self.cur = (l, c - 1)
            elif l > 0:
                p = (l - 1, len(buf.text[l - 1])); buf.delete(p, (l, 0)); self.cur = p
        elif forward:
            if c < len(buf.text[l]):
                buf.delete((l, c), (l, c + 1))
            elif l + 1 < buf.count():
                buf.delete((l, c), (l + 1, 0))
        if text:
            self.cur = buf.insert(self.cur, text)
        buf.end(self.cur)
        self.set_cursor(self.cur, ph)

    def undo(self, ph):
        p = self.preview
        buf = p.file if p is not None and isinstance(p.file, EditBuffer) else None
        if buf is None or not buf.history:
            self.status = "nothing to undo"
            return
        pos = buf.undo()
        self.clear_selection()
        self.kanchor = self.goal = None
        self.set_cursor(pos, ph)
        left = len(buf.history)
        self.status = f"undone ({left} more step{'s' * (left != 1)})" if left else "undone (back to the original)"

    def save(self):
        p = self.preview
        if not self.unsaved():
            self.status = "no changes to save"
            return
        data = p.file.to_bytes()
        try:
            with open(p.path, "wb") as out:      # in place: keeps owner, mode and links
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
        except OSError as e:
            self.status = f"save failed: {e.strerror}"
            return
        p.file.dirty = False
        p.file.save_point = p.file.history[-1] if p.file.history else None
        p.info = human(len(data)) + (f" · {p.lexer.name}" if p.lexer else "")
        self.status = f"saved {os.path.basename(p.path)} ({human(len(data))})"

    def toggle_mark(self, i):
        name = self.entries[i][0]
        if name != "..":
            self.marked ^= {name}

    def mark_move(self, delta):
        """Shift+↑/↓ in the list: mark the current entry, move, mark the new one."""
        if self.entries and self.entries[self.sel][0] != "..":
            self.marked.add(self.entries[self.sel][0])
        self.move(delta)
        if self.entries and self.entries[self.sel][0] != "..":
            self.marked.add(self.entries[self.sel][0])

    def ask_delete(self):
        names = sorted(self.marked) if self.marked else (
            [self.entries[self.sel][0]] if self.entries and self.entries[self.sel][0] != ".." else [])
        if not names:
            self.status = "nothing to delete"
            return
        kinds = {n: d for n, d, _ in self.entries}
        shown = [n + ("/" if kinds.get(n) else "") for n in names]
        what = shown[0] if len(shown) == 1 else f"{len(shown)} items ({', '.join(shown[:3])}{', …' if len(shown) > 3 else ''})"
        self.pending_delete = names
        self.status = f"Delete {what} permanently? y = yes, n = no"

    def delete_files(self, names):
        """Delete files / symlinks / empty folders; non-empty folders are left alone."""
        done, failed = 0, []
        for name in names:
            path = os.path.join(self.cwd, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    os.rmdir(path)
                else:
                    os.remove(path)
                done += 1
            except OSError as e:
                failed.append(f"{name}: {'folder not empty' if e.errno == errno.ENOTEMPTY else e.strerror}")
        keep = self.sel
        self.load_dir()
        if self.entries:
            self.sel = min(keep, len(self.entries) - 1)
            self.refresh_preview()
        self.status = f"deleted {done}" + (f"; not deleted: {'; '.join(failed)}" if failed else "")

    def copy_files(self, cut=False):
        names = sorted(self.marked) if self.marked else (
            [self.entries[self.sel][0]] if self.entries and self.entries[self.sel][0] != ".." else [])
        if not names:
            self.status = "nothing to cut" if cut else "nothing to copy"
            return
        self.file_clip = [os.path.join(self.cwd, n) for n in names]
        self.clip_cut = cut
        what = names[0] if len(names) == 1 else f"{len(names)} items"
        self.status = f"{'cut' if cut else 'copied'} {what} — Ctrl+V in a folder to {'move it' if cut else 'paste'}"

    def paste_files(self):
        if not self.file_clip:
            self.status = "nothing to paste: copy files with Ctrl+C or cut them with Ctrl+X first"
            return
        if self.blocked():
            return
        self.pasting = {"queue": list(self.file_clip), "done": 0, "failed": [], "skipped": 0,
                        "first": None, "left": [], "all": None, "ask": None}
        self.paste_next()

    def paste_next(self, answer=None):
        """Paste the queued entries; stop to ask when a name already exists (answer = r / k / s)."""
        p, cut = self.pasting, self.clip_cut
        while p["queue"]:
            src = p["queue"][0]
            name = os.path.basename(src)
            dest = os.path.join(self.cwd, name)
            problem = None
            if cut and os.path.realpath(os.path.dirname(src)) == os.path.realpath(self.cwd):
                problem = "already in this folder"
            elif os.path.isdir(src) and not os.path.islink(src):
                real = os.path.realpath(src)
                if os.path.realpath(self.cwd) == real or os.path.realpath(self.cwd).startswith(real + os.sep):
                    problem = f"can't {'move' if cut else 'paste'} a folder into itself"
            choice = "k"
            if not problem and os.path.lexists(dest) and not same_entry(src, dest):
                choice = answer or p["all"]
                if choice is None:
                    p["ask"] = (f"{name} already exists here: r = replace, k = keep both, s = skip"
                                f"  (R/K/S = for all), Esc = cancel")
                    self.status = p["ask"]
                    return
                answer = None
                if choice == "r" and os.path.realpath(src).startswith(os.path.realpath(dest) + os.sep):
                    problem = "can't replace the folder it is in"
            p["queue"].pop(0)
            if problem or choice == "s":
                if problem:
                    p["failed"].append(f"{name}: {problem}")
                else:
                    p["skipped"] += 1
                p["left"].append(src)
                continue
            try:
                if choice == "r":
                    tmp = os.path.join(self.cwd, unique_name(self.cwd, f".{name}.fv-partial"))
                    try:
                        transfer(src, tmp, cut)        # old entry is only removed once the new one is complete
                    except BaseException:
                        if os.path.lexists(tmp):
                            remove_entry(tmp)
                        raise
                    try:
                        remove_entry(dest)
                        os.rename(tmp, dest)
                    except OSError:
                        if cut:
                            shutil.move(tmp, src)      # put it back where it was cut from
                        else:
                            remove_entry(tmp)
                        raise
                else:
                    dest = os.path.join(self.cwd, unique_name(self.cwd, name, move=cut))
                    transfer(src, dest, cut)
                p["done"] += 1
                p["first"] = p["first"] or os.path.basename(dest)
            except (OSError, shutil.Error) as e:
                p["failed"].append(f"{name}: {getattr(e, 'strerror', None) or e}")
                p["left"].append(src)
        self.finish_paste()

    def finish_paste(self, cancelled=False):
        p, self.pasting = self.pasting, None
        verb = "moved" if self.clip_cut else "pasted"
        if self.clip_cut:
            self.file_clip = p["left"] + p["queue"]   # moved entries no longer exist where they were cut
        self.load_dir(focus=p["first"])
        done, first = p["done"], p["first"]
        self.status = ("cancelled: " if cancelled else "") + f"{verb} {done}" + (
            f" ({first}{', …' if done > 1 else ''})" if done else "") + (
            f"; skipped {p['skipped']}" if p["skipped"] else "") + (
            f"; not {verb}: {'; '.join(p['failed'])}" if p["failed"] else "")

    def ask_rename(self):
        if not self.entries or self.entries[self.sel][0] == "..":
            self.status = "nothing to rename"
        elif not self.blocked():
            old = self.entries[self.sel][0]
            self.prompt = [f"Rename {old} to: ", old, lambda new: self.rename(old, new)]

    def rename(self, old, new):
        new = new.strip()
        if new == old:
            self.status = "name unchanged"
            return
        if not new or "/" in new or new in (".", ".."):
            self.status = f"not a valid name: {new!r}"
            return
        target = os.path.join(self.cwd, new)
        src = os.path.join(self.cwd, old)
        same = new.casefold() == old.casefold() and os.path.lexists(target) and os.path.samefile(src, target)
        if os.path.lexists(target) and not same:   # os.rename would silently replace it
            self.status = f"{new} already exists"
            return
        try:
            os.rename(src, target)
        except OSError as e:
            self.status = f"cannot rename {old}: {e.strerror}"
            return
        self.load_dir(focus=new)
        self.status = f"renamed {old} → {new}"

    def create_file(self, name):
        name = name.strip()
        if not name or "/" in name or name in (".", ".."):
            self.status = f"not a valid file name: {name!r}"
            return
        try:
            os.close(os.open(os.path.join(self.cwd, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
        except FileExistsError:
            self.status = f"{name} already exists"
            return
        except OSError as e:
            self.status = f"cannot create {name}: {e.strerror}"
            return
        self.load_dir(focus=name)
        self.set_focus("preview")
        self.status = f"created {name}"

    def create_folder(self, name):
        name = name.strip()
        if not name or "/" in name or name in (".", ".."):
            self.status = f"not a valid folder name: {name!r}"
            return
        try:
            os.mkdir(os.path.join(self.cwd, name))
        except FileExistsError:
            self.status = f"{name} already exists"
            return
        except OSError as e:
            self.status = f"cannot create {name}: {e.strerror}"
            return
        self.load_dir(focus=name)
        self.status = f"created folder {name}"

    def find(self, text, back=False):
        """Select the next (or previous) case-insensitive match of text in the preview, wrapping around."""
        p = self.preview
        if p is None or p.kind != "text":
            self.status = "nothing to search: open a text file"
            return
        if not text:
            self.status = "type something to find (Ctrl+F)"
            return
        self.last_find = text
        pat = re.compile(re.escape(text), re.IGNORECASE)
        f, rng = p.file, self.selection_range()
        start = rng[0] if back and rng else self.cur
        f.index_all()                             # need the line count to wrap around
        n = f.count()
        for step in range(n + 1):
            l = (start[0] - step if back else start[0] + step) % n
            line = f.raw_line(l)[0]
            hits = list(pat.finditer(line))
            if step == 0:                        # the cursor's own line: only past (or before) the cursor
                                                 # (step n revisits it after wrapping, all matches allowed)
                hits = [m for m in hits if (m.start() < start[1] if back else m.start() >= start[1])]
            if hits:
                m = hits[-1] if back else hits[0]
                wrapped = step == n or (l > start[0] if back else l < start[0])
                h, _, _ = self.layout()
                ph = max(1, h - 2)
                self.set_focus("preview")
                self.set_cursor((l, m.start()), ph)
                self.set_cursor((l, m.end()), ph, extend=True)
                self.status = f"found {text!r} at line {l + 1}" + (" (wrapped around)" if wrapped else "")
                return
        self.status = f"{text!r} not found"

    def show_help(self):
        """Ctrl+H / F1: the key list in the preview; pressed again, back to the file."""
        if self.preview and self.preview.readonly and not self.preview.output:
            self.refresh_preview()
            self.set_focus("list")
            return
        path = self.preview.path if self.preview else self.cwd
        self.refresh_preview()
        if self.preview:
            self.preview.close()
        self.preview = Preview.text_view(path, "keys", "this is the key list; Ctrl+H or Esc closes it")
        self.preview.file.feed(__doc__[__doc__.index("Keys"):].rstrip().encode() + b"\n")
        self.set_focus("preview")

    def run_program(self):
        """Ctrl+R: run the highlighted .py file; its output replaces the preview. Again: stop it."""
        if self.proc:
            self.stop_program()
            self.status = "stopped"
            return
        if not self.entries:
            return
        path = os.path.join(self.cwd, self.entries[self.sel][0])
        if not (path.endswith(".py") and os.path.isfile(path)):
            self.status = "Ctrl+R runs Python files (.py)"
            return
        python = shutil.which("python3") or shutil.which("python") or sys.executable
        try:
            proc = subprocess.Popen([python, "-u", path], cwd=self.cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)    # its own group: stop kills its children too
        except OSError as e:
            self.status = f"cannot run {python}: {e.strerror}"
            return
        self.refresh_preview()
        self.preview.close()
        self.preview = Preview.program_output(path)
        self.preview.file.feed(f"$ {os.path.basename(python)} {os.path.basename(path)}\n".encode())
        os.set_blocking(proc.stdout.fileno(), False)
        self.proc, self.proc_start, self.proc_kept = proc, time.monotonic(), 0
        self.set_focus("preview")

    def stop_program(self):
        if self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
            self.poll_program(stopped=True)

    def poll_program(self, stopped=False):
        """Show whatever the running program printed; finish up once it has exited."""
        p, proc = self.preview, self.proc
        out = p.file
        h, _, _ = self.layout()
        ph = max(1, h - 2)
        follow = self.ptop + ph >= out.count()      # end of output visible: keep showing the end
        while True:
            try:
                data = proc.stdout.read(CHUNK)
            except (BlockingIOError, OSError, ValueError):
                data = None
            if not data:
                break
            room = OUTPUT_MAX - self.proc_kept
            if room > 0:
                out.feed(data[:room])
            self.proc_kept += len(data)
        if follow:
            self.ptop = max(0, out.count() - ph)
        if data is None and not stopped:
            return                                   # nothing more yet, still running
        if proc.poll() is None and not stopped:
            return                                   # output closed but it hasn't exited yet
        code = proc.wait()
        proc.stdout.close()
        self.proc = None
        secs = time.monotonic() - self.proc_start
        if self.proc_kept > OUTPUT_MAX:
            out.feed(f"\n[output cut at {human(OUTPUT_MAX)}]".encode())
        end = "stopped" if stopped else f"exit code {code}"
        out.feed(f"{'' if out.text[-1] == '' else chr(10)}[{end} · {secs:.1f} s]\n".encode())
        p.info = f"output of {os.path.basename(p.path)} · {end}"
        if follow:
            self.ptop = max(0, out.count() - ph)

    def prompt_key(self, k):
        """Keys while the footer asks for a file name."""
        if k in ("\n", "\r", curses.KEY_ENTER):
            (_, name, on_enter), self.prompt = self.prompt, None
            on_enter(name)
        elif k == "\x1b":
            self.prompt = None
            self.status = "cancelled"
        elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.prompt[1] = self.prompt[1][:-1]
        elif k == "\x15":                     # Ctrl+U: clear the text
            self.prompt[1] = ""
        elif isinstance(k, str) and k.isprintable():
            self.prompt[1] += k

    def paste(self, ph):
        text = read_clipboard()
        if text is None:
            self.status = "no clipboard tool to paste from; use your terminal's paste (Ctrl+Shift+V)"
        elif text:
            self.edit(ph, text=text)

    def preview_key(self, k, ph):
        """Handle a key while the preview has focus. Returns True if consumed."""
        p = self.preview
        if p is None or p.kind != "text":
            if k in PREVIEW_SCROLL:            # directory / message previews just scroll
                action = PREVIEW_SCROLL[k]
                if action == "home":
                    self.ptop = 0
                else:
                    self.scroll_preview({"up": -1, "down": 1, "pgup": -ph, "pgdn": ph, "end": "end"}[action], ph)
                return True
            return False
        if self.cursor_key(k, ph):
            return True
        if k in ("\n", "\r", curses.KEY_ENTER):
            self.edit(ph, text="\n")
        elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.edit(ph, back=True)
        elif k == curses.KEY_DC:
            self.edit(ph, forward=True)
        elif k == "\x1a":                     # Ctrl+Z
            self.undo(ph)
        elif k == "\x16":                     # Ctrl+V
            self.paste(ph)
        elif isinstance(k, str) and (k.isprintable() or k == "\t"):
            self.edit(ph, text=k)
        else:
            return False
        return True

    def handle_mouse(self):
        try:
            _, x, y, _, bstate = curses.getmouse()
        except curses.error:
            return
        p = self.preview
        text_preview = p is not None and p.kind == "text"
        if bstate & curses.BUTTON1_PRESSED:
            if x < self.layout()[2] and self.blocked():
                return
            self.set_focus("preview" if x >= self.layout()[2] else "list")
            if text_preview and self.in_preview(y, x):
                self.status = ""
                self.dragging = True
                self.drag_yx = (y, x)
                self.autoscroll_ticks = 0
                self.sel_anchor = self.sel_cursor = self.file_pos(y, x)
                self.press_pos = self.sel_anchor
                self.moved = False
                self.cur = self.kanchor = self.clamp(self.sel_anchor)
                self.goal = None
            elif x < self.layout()[2] - 1 and 1 <= y < self.layout()[0] - 1:
                idx = self.top + y - 1
                if idx < len(self.entries):
                    if bstate & curses.BUTTON_CTRL:
                        self.toggle_mark(idx)
                    self.move(idx - self.sel)
        elif bstate & curses.BUTTON1_RELEASED:
            if self.dragging:
                self.dragging = False
                self.drag_yx = (y, x)
                self.sel_cursor = self.file_pos(y, x)
                if not self.moved and self.sel_cursor == self.press_pos:
                    self.clear_selection()   # plain click: no selection
                else:
                    self.cur = self.clamp(self.sel_cursor)
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
            elif self.proc:                   # a program is running: pick up its output
                self.scr.timeout(100)
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
            if self.proc:
                self.poll_program()
            h, _, _ = self.layout()
            ph = max(1, h - 2)
            if k not in (None, curses.KEY_MOUSE):
                self.status = ""
            if self.find_buf and not self.find_active():
                self.find_buf = ""
            if self.pending_delete and k not in (None, curses.KEY_MOUSE, curses.KEY_RESIZE):
                names, self.pending_delete = self.pending_delete, None
                if k in ("y", "Y"):
                    self.delete_files(names)
                else:
                    self.status = "not deleted"
                self.draw()
                continue
            if self.pasting and k not in (None, curses.KEY_MOUSE, curses.KEY_RESIZE):
                if k in ("r", "k", "s", "R", "K", "S"):
                    if k.isupper():
                        self.pasting["all"] = k.lower()
                    self.paste_next(k.lower())
                elif k == "\x1b":
                    self.finish_paste(cancelled=True)
                else:
                    self.status = self.pasting["ask"]          # keep asking
                self.draw()
                continue
            if self.prompt is not None and k not in (None, curses.KEY_MOUSE, curses.KEY_RESIZE):
                self.prompt_key(k)
                self.draw()
                continue
            if self.confirm is not None and k not in (None, curses.KEY_MOUSE, curses.KEY_RESIZE):
                action, self.confirm = self.confirm, None    # answer to "Save changes? y/n"
                if k in ("y", "Y"):
                    self.save()
                    if self.unsaved():                       # save failed: status says why
                        self.draw()
                        continue
                elif k in ("n", "N"):
                    self.preview.file.dirty = False          # drop the edits
                    self.refresh_preview()
                else:
                    self.status = "cancelled: still editing"
                    self.draw()
                    continue
                k = action                                   # now carry out quit / leave / reload
            discards = k in ("\x11", "\x14", "\x0e", "\x12", "\x04", curses.KEY_F7) or (
                k in ("\x1b", "\x03") and not self.selection_range() and not (k == "\x03" and self.focus == "list"))
            if discards and self.unsaved():
                self.confirm = k
                name = os.path.basename(self.preview.path)
                self.status = f"Save changes to {name}? y = yes, n = no, Esc = cancel"
                self.draw()
                continue
            if k is None:
                pass
            elif k == curses.KEY_MOUSE:
                self.handle_mouse()
            elif k == "\x11":                 # Ctrl+Q
                break
            elif k == "\x13":                 # Ctrl+S
                self.save()
            elif k == "\x0e":                 # Ctrl+N: new file in this folder
                self.prompt = ["New file name: ", "", self.create_file]
            elif self.focus == "list" and k == "\x03":    # Ctrl+C in the list: copy files
                self.copy_files()
            elif self.focus == "list" and k == "\x18":    # Ctrl+X in the list: cut files (Ctrl+V moves them)
                self.copy_files(cut=True)
            elif self.focus == "list" and k == "\x16":    # Ctrl+V in the list: paste files here
                self.paste_files()
            elif k in ("\x08", curses.KEY_F1):   # Ctrl+H / F1: key help (Backspace sends ^?, not ^H)
                self.show_help()
            elif k == "\x12":                 # Ctrl+R: run the .py file (again: stop it)
                self.run_program()
            elif k == "\x06":                 # Ctrl+F: find text in the preview
                self.prompt = ["Find: ", self.last_find, self.find]
            elif k in (curses.KEY_F3, "\x07") or (isinstance(k, int) and curses.keyname(k) == b"KEY_F(15)"):
                self.find(self.last_find, back=k == curses.KEY_F15)   # F3 / Ctrl+G next, Shift+F3 previous
            elif k in (curses.KEY_F7, "\x04"):   # F7 / Ctrl+D: new folder here (laptops often eat bare F7)
                self.prompt = ["New folder name: ", "", self.create_folder]
            elif k in (curses.KEY_F2, "\x05"):   # F2 / Ctrl+E: rename the highlighted entry
                self.ask_rename()
            elif self.focus == "preview" and self.preview_key(k, ph):
                pass
            elif k == "\t":
                self.set_focus("preview" if self.focus == "list" else "list")
            elif (self.focus == "list" and isinstance(k, str) and k.isprintable()
                  and (k != " " or self.find_buf)):
                self.type_ahead(k)
            elif self.focus == "list" and k == " " and self.entries:
                self.toggle_mark(self.sel)                       # Space: mark / unmark, move on
                self.move(1)
            elif self.focus == "list" and k in (curses.KEY_SR, curses.KEY_SF):
                self.mark_move(-1 if k == curses.KEY_SR else 1)
            elif self.focus == "list" and k == curses.KEY_DC:
                self.ask_delete()
            elif self.focus == "list" and self.find_buf and k in (
                    curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.find_backspace()
            elif self.find_buf and k == "\x1b":
                self.find_buf = ""
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
                if self.focus == "list":
                    self.marked = set()
                if not self.unsaved():            # with unsaved edits Esc only drops the selection
                    if self.preview and self.preview.readonly and not self.preview.output:
                        self.refresh_preview()    # close the key help
                    self.set_focus("list")        # back to the file list to pick another file
            self.autoscroll()
            self.draw()
        self.stop_program()
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


def unique_name(folder, name, move=False):
    """name, or the first free 'base (copy).ext', 'base (copy 2).ext', ... (moves: 'base (2).ext', ...)."""
    if not os.path.lexists(os.path.join(folder, name)):
        return name
    base, ext = os.path.splitext(name)
    n = 2 if move else 1
    while True:
        candidate = f"{base} ({n}){ext}" if move else f"{base} (copy{'' if n == 1 else f' {n}'}){ext}"
        if not os.path.lexists(os.path.join(folder, candidate)):
            return candidate
        n += 1


def same_entry(a, b):
    """True when two paths are the same directory entry (symlinks not followed)."""
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def transfer(src, dest, move):
    """Move, or copy (folders recursively, symlinks as links, metadata kept), src to the free path dest."""
    if move:
        shutil.move(src, dest)
    elif os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copy2(src, dest, follow_symlinks=False)


def remove_entry(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def read_clipboard():
    """Clipboard text via a system tool, or None if no tool is available."""
    tools = []
    if os.environ.get("WAYLAND_DISPLAY"):
        tools.append(["wl-paste", "--no-newline"])
    if os.environ.get("DISPLAY"):
        tools += [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]
    tools += [["pbpaste"], ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Raw"]]
    for cmd in tools:
        if shutil.which(cmd[0]):
            try:
                out = subprocess.run(cmd, capture_output=True, timeout=2, check=True).stdout
                return out.decode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError):
                continue
    return None


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
