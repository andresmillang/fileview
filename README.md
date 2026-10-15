# fv

A terminal file explorer with a live preview pane. Move the selection and the right
pane immediately shows the highlighted file's contents — and you can edit and save it.

![fv: two marked entries and a text selection in the live preview](docs/screenshot.svg)

- Pure stdlib (`curses`); [Pygments](https://pygments.org) optional for syntax highlighting
- Large files are read lazily — only the visible lines are loaded
- Friendly messages for binary, empty, unreadable, and special files
- Type-to-navigate: type the start of a name to jump to it, like a graphical file browser
- `../` at the top of every folder to go up one level; Ctrl+N creates a new file, Ctrl+D (or F7) a new folder
- Find text in the previewed file with Ctrl+F (F3 or Ctrl+G / Shift+F3 for the next / previous match)
- Run scripts and programs with Ctrl+R (Python, shell, Node, Perl, anything with a `#!` line, executables): the output streams into the right panel (Esc shows the file again)
- Folders in bright blue, runnable files in bright green
- Mark several files with Space; copy/paste them with Ctrl+C/Ctrl+V, move them with Ctrl+X/Ctrl+V, delete them with Del (always asks first;
  non-empty folders are never deleted), rename with Ctrl+E (or F2)
- Text selection inside the preview only: drag with the mouse (auto-scrolls past the edges) or
  Ctrl+A for the whole file; Ctrl+C copies the exact file text, line endings and indentation included
- Lightweight editing in the preview: text cursor, Shift+arrow selection, typing, Ctrl+V paste,
  Ctrl+S save. Files are only written when you press Ctrl+S; leaving or quitting with unsaved changes asks
  "Save changes? y/n" (Esc cancels).
  Editing is limited to UTF-8 text files up to 10 MB (others stay view-only so they can't be corrupted)
- Ctrl+H (or F1) shows every key in the right panel
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
| Ctrl+F | find text in the file (case-insensitive, wraps around); the match is selected. Pre-filled with the last search; Ctrl+U clears it |
| F3 or Ctrl+G / Shift+F3 | next / previous match |
| Esc | back to the file list (with unsaved changes it asks: y = save, n = discard, Esc = cancel) |

**Anywhere**

| Key | Action |
|---|---|
| Ctrl+A | select the whole previewed file (and move focus to the preview, so Ctrl+C copies its text) |
| Ctrl+C | in the preview: copy the selected text (quits if nothing is selected); in the file list: copy files |
| Esc | clear the selection, marks or a typed name, and return to the file list |
| Ctrl+N | create a new empty file in the current folder (asks for the name; Esc cancels) |
| Ctrl+R | run the highlighted file in its folder: `.py` with `python3` — or `uv run` when the script declares inline dependencies (`# /// script`) or sits in a `pyproject.toml` project — `.sh` with `bash`, `.js`/`.mjs`/`.cjs` with `node`, `.pl` with `perl`, any file whose first line is `#!…` with that interpreter, and executable files directly. The output, errors included, appears live in the right panel with the exit code at the end. Ctrl+R again while it runs stops it (child processes too); Esc switches back to the file's contents (stopping it if it is still running); moving to another file or quitting also stops it. The program gets no keyboard input |
| Ctrl+D / F7 | create a new folder in the current folder (asks for the name; Esc cancels). On many laptops a bare F7 is a hardware key (e.g. display switch) — use Ctrl+D or Fn+F7 |
| Ctrl+E / F2 | rename the highlighted file or folder (pre-filled; Ctrl+U clears; never overwrites an existing name) |
| Ctrl+C / Ctrl+V | copy the marked (or highlighted) files and folders / paste them into the current folder |
| Ctrl+X | cut the marked (or highlighted) files and folders; the next Ctrl+V moves them into the current folder |
| | when pasting over an existing name it asks: **r** replace, **k** keep both (`name (copy).ext` for copies, `name (2).ext` for moves), **s** skip, Esc cancel; **R/K/S** answer for all the remaining ones |
| Ctrl+H / F1 | show the list of keys in the right panel (Ctrl+H again or Esc closes it) |
| Ctrl+T | show/hide hidden files |
| Ctrl+L | redraw the screen |
| mouse wheel | scroll the panel under the pointer |
| Ctrl+Q | quit (with unsaved changes it asks: y = save and quit, n = quit without saving, Esc = cancel) |

Copying uses `wl-copy`, `xclip`, `xsel`, `pbcopy` or `clip.exe` when available, and also sends
OSC 52 (up to 1 MB) so it works over SSH and in tmux, if the terminal allows clipboard writes.
Copies larger than 100 MB are refused. Your terminal's own selection still works with **Shift+drag**.
