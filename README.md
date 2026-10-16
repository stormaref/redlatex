# redlatex

Render a side-by-side, word-level visual diff of two compiled versions of a LaTeX document.

Every page of the revised PDF is shown next to the matching page of the earlier version. Removed text is
highlighted in red on the left page, added text in green on the right page, with a margin bar beside every
changed line and a caret where text is missing on one side. The output PDF starts with a cover page that
lists the number of removed and added words per section (each entry links to the matching spread), and
it carries a bookmark per section.

## Requirements

- A TeX distribution with `latexmk` on `PATH` (only needed when compiling from git revisions)
- `git` on `PATH`
- Python 3.10+, managed with [uv](https://docs.astral.sh/uv/); the only dependency is
  [PyMuPDF](https://pymupdf.readthedocs.io/)

## Install

```
git clone <this repository> redlatex
cd redlatex
uv sync
```

## Usage

Run it from the root of the git repository that holds your LaTeX project (or pass `--repo`). From the
tool's own directory, point uv at it with `--project`:

```
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

Revisions are compiled with `latexmk -pdf` in temporary directories; a revision is exported with
`git archive`, and the working tree is compiled in place with build files sent to a temporary directory,
so the repository is not modified.

## Web app

`redlatex.web` serves a small upload page: choose the earlier and the revised version of a LaTeX project as
`.zip` files (the main `.tex` file is auto-detected, or name it) and download the diff PDF.

```
uv sync --extra web
uv run uvicorn redlatex.web:app
```

Or with Docker (includes a TeX Live install):

```
docker build -t redlatex .
docker run -p 10000:10000 redlatex
```

Uploads are size-limited, unpacked with path checks, compiled with shell escape off and TeX file access
restricted to the project (`openin_any=p`), and deleted after the response.

### Deploying to Render

1. Create a Render web service from this repository (`render.yaml` is a ready blueprint; Docker runtime).
2. In the service settings, copy the **Deploy Hook** URL.
3. In the GitHub repository, add it as the secret `RENDER_DEPLOY_HOOK_URL`.

`ci.yml` runs the tests and builds the image on every push and pull request; `deploy.yml` triggers the
Render deploy after CI succeeds on `main`.

## How the matching works

Words are extracted from both compiled PDFs and compared with a word-level diff, so the tool sees what a
reader sees: numbers, resolved references, captions and bibliography entries, not LaTeX source. Body text
and small print (captions, tables, footnotes) are diffed as separate streams so that a float moving
elsewhere in the text flow does not look like an edit. Words hyphenated across a line break are joined,
and page numbers are ignored. Each revised page is paired with the earlier page that shares the most
matched words (at least 25); earlier pages without a counterpart get a spread of their own. Section
counts use the PDF bookmarks, so the document should be built with `hyperref`.

## Limitations

- Changes inside figures that contain no text (plots, photos, drawings) are not detected.
- Only text is compared; purely visual changes such as colours or spacing are not marked.
- Appendix sections are lettered only when the LaTeX source of the revised version is readable
  (a git revision or the working tree) and contains `\appendix`.
- Compilation is a plain `latexmk -pdf` run; documents needing other engines or special options should be
  compiled by hand and passed with `--old-pdf` / `--new-pdf`.

## Development

```
uv sync
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
