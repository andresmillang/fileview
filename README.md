# fv

A read-only terminal file explorer with a live preview pane. Move the selection
and the right pane immediately shows the highlighted file's contents.

- Pure stdlib (`curses`); [Pygments](https://pygments.org) optional for syntax highlighting
- Large files are read lazily — only the visible lines are loaded
- Friendly messages for binary, empty, unreadable, and special files
- Adapts to terminal resizing; never modifies anything

## Install

```bash
uv tool install 'git+https://github.com/andresmillang/fileview[highlight]'
# or just run it:
python3 fv.py [DIRECTORY]
```

## Keys

| Key | Action |
|---|---|
| ↑ ↓ PgUp PgDn Home End | move selection (preview updates live) |
| Enter / → | open directory |
| Backspace / ← | parent directory |
| `j`/`k` `J`/`K` space/`b` `g`/`G` | scroll preview: line / 10 lines / page / top-bottom |
| `.` | toggle hidden files |
| `q` / `Q` | quit |
