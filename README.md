# fv

A read-only terminal file explorer with a live preview pane. Move the selection
and the right pane immediately shows the highlighted file's contents.

![fv: live preview with a mouse selection in progress](docs/screenshot.svg)

- Pure stdlib (`curses`); [Pygments](https://pygments.org) optional for syntax highlighting
- Large files are read lazily — only the visible lines are loaded
- Friendly messages for binary, empty, unreadable, and special files
- Type-to-navigate: type the start of a name to jump to it, like a graphical file browser
- Text selection inside the preview only: drag with the mouse (auto-scrolls past the edges) or
  Ctrl+A for the whole file; Ctrl+C copies the exact file text, line endings and indentation included
- Adapts to terminal resizing; never modifies anything

## Install

```bash
uv tool install 'git+https://github.com/andresmillang/fileview[highlight]'
# or just run it:
python3 fv.py [DIRECTORY]
```

## Keys

**Tab** or a mouse click chooses which panel the keys act on.

**File list**

| Key | Action |
|---|---|
| type letters | jump to the first name starting with them; keep typing to refine, repeat a letter to cycle (case-insensitive, resets after 1 s) |
| ↑ ↓ PgUp PgDn Home End | move the selection; the preview follows |
| Enter / → | open a directory, or move focus into the preview for a file |
| Backspace / ← | parent directory (Backspace deletes a typed letter while searching) |

**Preview**

| Key | Action |
|---|---|
| ↑ ↓ PgUp PgDn Home End | scroll |
| ← | back to the file list |
| mouse drag | select text; hold at or past the top/bottom edge to auto-scroll |

**Anywhere**

| Key | Action |
|---|---|
| Ctrl+A | select the whole previewed file |
| Ctrl+C | copy the selection (quits if nothing is selected) |
| Esc | clear the selection or cancel a typed name, and return to the file list |
| Ctrl+T | show/hide hidden files |
| Ctrl+L | redraw the screen |
| mouse wheel | scroll the panel under the pointer |
| Ctrl+Q | quit |

Copying uses `wl-copy`, `xclip`, `xsel`, `pbcopy` or `clip.exe` when available, and also sends
OSC 52 (up to 1 MB) so it works over SSH and in tmux, if the terminal allows clipboard writes.
Copies larger than 100 MB are refused. Your terminal's own selection still works with **Shift+drag**.
