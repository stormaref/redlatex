<div align="center">

# redlatex

**See exactly what changed between two versions of your LaTeX paper.**<br>
A side-by-side, word-level visual diff of compiled PDFs, with changes highlighted in red and green.

[![CI](https://github.com/stormaref/redlatex/actions/workflows/ci.yml/badge.svg)](https://github.com/stormaref/redlatex/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)

[**Try it online**](https://redlatex.onrender.com) · [Command line](#command-line) · [Self-host](#self-hosting) · [How it works](#how-it-works)

</div>

![A redlatex spread: removed words in red on the left page, added words in green on the right page](docs/example.png)

## Why

`latexdiff` compares LaTeX *source*, which breaks on custom macros, `\input` trees and `\newcommand`s, and tells
you nothing about what a reader sees. Reviewers and co-authors want to see the change on the page.

redlatex compiles both versions and compares the **words in the rendered PDFs**. Numbers, resolved references,
captions and bibliography entries are all compared as the reader sees them.

- **Side by side.** Every revised page sits next to its matching earlier page.
- **Word-level.** Removed words are red on the left, added words green on the right, with a margin bar beside
  each changed line and a caret where text is missing on the other side.
- **Cover summary.** The first page counts removed and added words per section, each linking to its spread.
  The output also carries a bookmark per section.
- **Robust to layout.** Floats that move, words hyphenated across lines and page numbers do not show up as edits.
- **No LaTeX source parsing.** If it compiles, it can be diffed. You can also diff two existing PDFs.

## Use it online

Open **<https://redlatex.onrender.com>**, drop in the two versions of your project as `.zip` files, and download
the diff PDF. Files are deleted as soon as the diff is made. The hosted instance is small and may take a few
seconds to wake up; for large projects or private work, run it yourself.

## Command line

Requirements: Python 3.10+, [uv](https://docs.astral.sh/uv/), and, to compile from git revisions, `git` and a
TeX distribution with `latexmk`.

```bash
git clone https://github.com/stormaref/redlatex
cd redlatex
uv sync
```

Run it from the root of the git repository that holds your LaTeX project (or pass `--repo`):

```bash
# compare a tagged revision with HEAD
uv run redlatex --old v1 --new HEAD

# compare HEAD with the working tree (the default), main file in a subfolder
uv run redlatex --tex paper/main.tex --out changes.pdf

# compare two PDFs directly (no git, no latexmk)
uv run redlatex --old-pdf before.pdf --new-pdf after.pdf

# run against another repository
uv run --project /path/to/redlatex redlatex --repo /path/to/paper --old v1
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--repo` | `.` | git repository root |
| `--tex` | `main.tex` | main `.tex` file, relative to the repo |
| `--old` | `HEAD` | earlier git revision (or `WORKTREE`) |
| `--new` | `WORKTREE` | revised git revision (or `WORKTREE` for the working tree) |
| `--old-pdf`, `--new-pdf` | | use these PDFs instead of compiling |
| `--old-label`, `--new-label` | revision or PDF name | labels in the page headers |
| `--title` | `Document changes` | cover page title |
| `--out` | `./revision-diff.pdf` | output PDF |

Revisions are compiled with `latexmk -pdf` in temporary directories. A revision is exported with `git archive`
and the working tree is compiled in place with build files sent to a temporary directory, so your repository
is never modified.

## Self-hosting

The web app is a small [FastAPI](https://fastapi.tiangolo.com/) service. The Docker image bundles TeX Live.

```bash
docker build -t redlatex .
docker run -p 10000:10000 redlatex      # then open http://localhost:10000
```

Without Docker: `uv sync --extra web && uv run uvicorn redlatex.web:app` (needs `latexmk` on `PATH`).

**Limits and safety.** Uploads are capped at 50 MB per zip (200 MB unpacked, 5000 files) and each version
gets 60 seconds to compile (`REDLATEX_COMPILE_TIMEOUT`), and at most 2 documents compile at once (`REDLATEX_MAX_JOBS`; others wait up to 20 s, then get a 503). Archives are unpacked with path-traversal checks, LaTeX runs with shell escape off
and with TeX file access restricted to the project (`openin_any=p`, `openout_any=p`), the container runs as a
non-root user, and temporary files are removed after every request. Compiling is CPU- and memory-heavy and the
service runs a single worker, so size your instance for the documents and traffic you expect.

### Deploy to Render

1. Create a Render **Web Service** from your fork; the Docker runtime picks up the `Dockerfile` (`render.yaml`
   is a ready blueprint). Use at least the Starter instance: compiling LaTeX needs more than 512 MB.
2. Copy the service's **Deploy Hook** URL and save it as the repository secret `RENDER_DEPLOY_HOOK_URL`.
3. Turn off Render's own auto-deploy. `ci.yml` tests and builds on every push, and `deploy.yml` calls the hook
   once CI has passed on `main`.

## How it works

1. Both versions are compiled to PDF (or taken as given).
2. Words are extracted with [PyMuPDF](https://pymupdf.readthedocs.io/) and compared with a word-level diff.
3. Body text and small print (captions, tables, footnotes) are diffed as separate streams, so a float landing
   elsewhere in the flow does not look like an edit. Words hyphenated across a line break are joined and page
   numbers are ignored.
4. Each revised page is paired with the earlier page that shares the most matched words (at least 25); earlier
   pages without a counterpart get a spread of their own.
5. Section counts use the PDF bookmarks, so build your document with `hyperref`.

## Limitations

- Changes inside figures that contain no text (plots, photos, drawings) are not detected.
- Only text is compared; purely visual changes such as colours or spacing are not marked.
- Appendix sections are lettered only when the LaTeX source of the revised version is readable (a git
  revision, the working tree, or an uploaded zip) and contains `\appendix`.
- Compilation is a plain `latexmk -pdf` run. Documents that need another engine or special options should be
  compiled by hand and passed with `--old-pdf` / `--new-pdf`.

## Contributing

Bug reports, test documents that break the matching, and pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
