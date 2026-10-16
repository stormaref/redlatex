"""Obtaining the two PDFs: compile a git revision (or the working tree) with latexmk."""
import os
import signal
import subprocess
import sys
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


def _limits(cpu=None, mem_mb=None, fsize_mb=None):
    """preexec hook applying resource limits to a child process (POSIX only)."""
    def apply():
        import resource
        if cpu:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        if fsize_mb:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_mb << 20, fsize_mb << 20))
        if mem_mb and sys.platform == 'linux':
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20))
    return apply


def run_killing(cmd, cwd, timeout=None, env=None, cpu=None, mem_mb=None, fsize_mb=None):
    """Run `cmd` in its own session with optional resource limits.

    On timeout the whole process group is killed and TimeoutExpired is raised.
    """
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            preexec_fn=_limits(cpu, mem_mb, fsize_mb) if os.name == 'posix' else None)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise


def sandbox_env(home):
    """Minimal environment for compiling untrusted LaTeX: no inherited secrets, no shell escape, TeX file
    access restricted to the project tree ('paranoid' mode)."""
    keep = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}
    return {**keep, 'HOME': str(home), 'openin_any': 'p', 'openout_any': 'p', 'shell_escape': 'f'}


def latex_errors(log, hide=(), limit=3):
    """First few '!' error lines of a LaTeX log, with local paths removed."""
    try:
        lines = Path(log).read_text(errors='ignore').splitlines()
    except OSError:
        return ''
    found = []
    for i, line in enumerate(lines):
        if line.startswith('!'):
            ctx = next((l for l in lines[i + 1:i + 4] if l.startswith('l.')), '')
            found.append((line + ' ' + ctx).strip())
            if len(found) == limit:
                break
    text = ' | '.join(found)[:600]
    for h in hide:
        text = text.replace(str(h), '.')
    return text


def compile_tree(root, tex, out, timeout=None):
    """Compile `tex` (relative to the source tree `root`) with latexmk into `out`; return the PDF path.

    Meant for untrusted input: sandboxed environment, resource limits, and a hard timeout that kills the
    whole process tree.
    """
    tex_path = Path(tex)
    out.mkdir(parents=True, exist_ok=True)
    cmd = ['latexmk', '-pdf', '-interaction=nonstopmode', '-no-shell-escape', f'-outdir={out}', tex_path.name]
    try:
        run_killing(cmd, root / tex_path.parent, timeout, env=sandbox_env(out), cpu=int(timeout or 60) + 15,
                    mem_mb=1536, fsize_mb=200)
    except subprocess.TimeoutExpired:
        raise SystemExit(f'compiling {tex} timed out after {timeout}s')
    pdf = out / tex_path.with_suffix('.pdf').name
    if not pdf.exists():
        detail = latex_errors(pdf.with_suffix('.log'), hide=(root.resolve(), out.resolve(), root, out))
        raise SystemExit(f'LaTeX could not compile {tex}' + (f': {detail}' if detail else ''))
    return pdf


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
