# Contributing to redlatex

Thanks for helping out. Bug reports, small fixes and ideas are all welcome.

## Setup

```bash
git clone https://github.com/stormaref/redlatex
cd redlatex
uv sync --extra web --group dev
uv run pytest
```

The web app needs `latexmk` on your `PATH` to compile uploads, or use the Docker image.

## Reporting bugs

Open an issue with:

- what you expected and what happened (a screenshot of the diff page helps);
- how you ran it (command line, web app, Docker);
- if possible, a minimal pair of `.tex` projects or PDFs that shows the problem. Please only share documents
  you are allowed to share.

## Pull requests

- Keep a change focused; open an issue first for anything large.
- Add or update tests in `tests/` (`uv run pytest` must pass; CI also builds the Docker image).
- Match the style of the surrounding code and keep the README in sync with user-visible changes.
- Use `uv` for dependencies (`uv add`, `uv sync`), not `pip`.

## Layout

| Path | What it does |
| --- | --- |
| `src/redlatex/diff.py` | word extraction, anchored matching, moved-text and figure detection, PDF rendering |
| `src/redlatex/build.py` | compiling git revisions and project trees with `latexmk` |
| `src/redlatex/cli.py` | command-line interface |
| `src/redlatex/web.py`, `index.html` | upload/GitHub service with background jobs, and its page |
| `action.yml` | the GitHub Action (tested by `.github/workflows/action-test.yml` on `examples/`) |

## Releasing

1. Bump the version: `uv version 0.2.1` (also update `__version__` in `src/redlatex/__init__.py`).
2. Commit, push, and create a GitHub release with the tag `v0.2.1`.
3. `publish.yml` builds the package and uploads it to PyPI through trusted publishing; it refuses to run if the
   tag and the package version differ. Point the README's action example at the new tag.

The example paper in `examples/` is used by the action test and to build the sample diff and screenshots on the
website and in this README.

By contributing you agree that your work is released under the [MIT License](LICENSE).
