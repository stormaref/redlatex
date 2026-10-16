"""Obtaining the two PDFs: compile a git revision (or the working tree) with latexmk."""
import subprocess
from datetime import date
from pathlib import Path

WORKTREE = 'WORKTREE'


def git(repo, *args, **kw):
    """Run a git command in `repo` and return its output."""
    return subprocess.check_output(['git', *args], cwd=repo, **kw)


def describe(repo, tex, rev, pdf=None):
    """Short human-readable description of a version: 'abc1234, 2026-01-31' or the PDF file's name and date."""
    if pdf is not None:
        return f'{pdf.name}, {date.fromtimestamp(pdf.stat().st_mtime).isoformat()}'
    if rev == WORKTREE:
        h, d = git(repo, 'log', '-1', '--format=%h|%ad', '--date=short', text=True).strip().split('|')
        dirty = git(repo, 'status', '--porcelain', '--', tex, text=True).strip()
        return f'{h}+edits, {d}' if dirty else f'{h}, {d}'
    h, d = git(repo, 'log', '-1', '--format=%h|%ad', '--date=short', rev, text=True).strip().split('|')
    return f'{h}, {d}'


def source(repo, tex, rev):
    """LaTeX source of the main file at a revision, or None if it cannot be read."""
    try:
        if rev == WORKTREE:
            return (repo / tex).read_text()
        return git(repo, 'show', f'{rev}:{tex}', text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None


def compile_rev(repo, tex, rev, work):
    """Compile the main .tex file of a revision (or the working tree) and return the PDF path."""
    tex_path = Path(tex)
    out = (work / ('worktree' if rev == WORKTREE else rev.replace('/', '_'))).resolve()
    out.mkdir(parents=True)
    if rev == WORKTREE:
        root = repo
    else:
        root = out / 'src'
        root.mkdir()
        subprocess.run(['tar', '-x', '-C', str(root)], input=git(repo, 'archive', rev), check=True)
    cmd = ['latexmk', '-pdf', '-interaction=nonstopmode', f'-outdir={out}', tex_path.name]
    subprocess.run(cmd, cwd=root / tex_path.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pdf = out / tex_path.with_suffix('.pdf').name
    if not pdf.exists():
        raise SystemExit(f'compiling {rev} failed; see {pdf.with_suffix(".log")}')
    return pdf
