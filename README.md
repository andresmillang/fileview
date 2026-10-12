# fv

A terminal file explorer with a live preview pane. Move the selection and the right
pane immediately shows the highlighted file's contents — and you can edit and save it.

![fv: live preview with a mouse selection in progress](docs/screenshot.svg)

- Pure stdlib (`curses`); [Pygments](https://pygments.org) optional for syntax highlighting
- Large files are read lazily — only the visible lines are loaded
- Friendly messages for binary, empty, unreadable, and special files
- Type-to-navigate: type the start of a name to jump to it, like a graphical file browser
- `../` at the top of every folder to go up one level; Ctrl+N creates a new file
- Mark several files with Space and delete them with Del (always asks first; non-empty folders are never deleted)
- Text selection inside the preview only: drag with the mouse (auto-scrolls past the edges) or
  Ctrl+A for the whole file; Ctrl+C copies the exact file text, line endings and indentation included
- Lightweight editing in the preview: text cursor, Shift+arrow selection, typing, Ctrl+V paste,
  Ctrl+S save. Files are only written when you press Ctrl+S; leaving or quitting with unsaved changes asks
  "Save changes? y/n" (Esc cancels).
  Editing is limited to UTF-8 text files up to 10 MB (others stay view-only so they can't be corrupted)
- Adapts to terminal resizing

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
| Enter / → | open a directory (or `../` to go one folder up), or move focus into the preview for a file |
| Backspace / ← | parent directory (Backspace deletes a typed letter while searching) |
| Space | mark / unmark the entry and move down (Shift+↑/↓ marks while moving; Ctrl+click toggles) |
| Del | delete the marked entries, or the highlighted one, after a "Delete …? y/n" question; folders are only deleted when empty |

**Preview**

| Key | Action |
|---|---|
| ← → ↑ ↓ PgUp PgDn | move the text cursor |
| Home / End | start / end of the line (Ctrl+Home / Ctrl+End: start / end of the file) |
| Shift + any of the above | select text |
| mouse click / drag | place the cursor / select text; hold at or past the top/bottom edge to auto-scroll |
| type, Enter, Tab | insert text (a selection is replaced) |
| Backspace / Delete | delete (a selection is deleted) |
| Ctrl+V | paste from the system clipboard (your terminal's own paste, e.g. Ctrl+Shift+V, also works) |
| Ctrl+Z | undo (a run of typing is undone in one step; undoing back to the last save clears `[modified]`) |
| Ctrl+S | save the file |
| Esc | back to the file list (with unsaved changes it asks: y = save, n = discard, Esc = cancel) |

**Anywhere**

| Key | Action |
|---|---|
| Ctrl+A | select the whole previewed file |
| Ctrl+C | copy the selection (quits if nothing is selected) |
| Esc | clear the selection, marks or a typed name, and return to the file list |
| Ctrl+N | create a new empty file in the current folder (asks for the name; Esc cancels) |
| F2 | rename the highlighted file or folder (pre-filled; Ctrl+U clears; never overwrites an existing name) |
| Ctrl+T | show/hide hidden files |
| Ctrl+L | redraw the screen |
| mouse wheel | scroll the panel under the pointer |
| Ctrl+Q | quit (with unsaved changes it asks: y = save and quit, n = quit without saving, Esc = cancel) |

Copying uses `wl-copy`, `xclip`, `xsel`, `pbcopy` or `clip.exe` when available, and also sends
OSC 52 (up to 1 MB) so it works over SSH and in tmux, if the terminal allows clipboard writes.
Copies larger than 100 MB are refused. Your terminal's own selection still works with **Shift+drag**.
