"""Command-line interface of pdf-revision-diff."""
import argparse
import tempfile
from pathlib import Path

from .build import WORKTREE, compile_rev, describe, source
from .diff import render

EPILOG = """examples:
  pdf-revision-diff --old v1 --new HEAD
  pdf-revision-diff --old-pdf before.pdf --new-pdf after.pdf --out changes.pdf
  pdf-revision-diff                       # HEAD against the working tree
"""


def main(argv=None):
    p = argparse.ArgumentParser(prog='pdf-revision-diff', description=__doc__.split('\n')[0] + '.\n\n' + (
        'Every page of the revised PDF is shown next to the matching page of the earlier version, with '
        'removed text highlighted on the left and added text on the right. Versions are compiled with '
        'latexmk from git revisions (or the working tree) unless PDFs are given.'),
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--repo', default='.', help='git repository root (default: current directory)')
    p.add_argument('--tex', default='main.tex', help='main .tex file, relative to the repo (default: main.tex)')
    p.add_argument('--old', default='HEAD', help='earlier revision, or WORKTREE (default: HEAD)')
    p.add_argument('--new', default=WORKTREE, help='revised version: a revision or WORKTREE (default)')
    p.add_argument('--old-pdf', type=Path, help='use this PDF instead of compiling --old')
    p.add_argument('--new-pdf', type=Path, help='use this PDF instead of compiling --new')
    p.add_argument('--old-label', help='label of the earlier version (default: the revision or PDF name)')
    p.add_argument('--new-label', help='label of the revised version (default: the revision or PDF name)')
    p.add_argument('--title', default='Document changes', help='title of the cover page')
    p.add_argument('--out', type=Path, default=Path('revision-diff.pdf'), help='output PDF (default: ./revision-diff.pdf)')
    args = p.parse_args(argv)

    repo = Path(args.repo).resolve()
    with tempfile.TemporaryDirectory(prefix='pdfdiff-') as tmp:
        work = Path(tmp)
        old_pdf = args.old_pdf or compile_rev(repo, args.tex, args.old, work)
        new_pdf = args.new_pdf or compile_rev(repo, args.tex, args.new, work)
        render(old_pdf, new_pdf, args.out, title=args.title,
               old_label=args.old_label or (args.old_pdf.name if args.old_pdf else args.old),
               new_label=args.new_label or (args.new_pdf.name if args.new_pdf else args.new),
               old_info=describe(repo, args.tex, args.old, args.old_pdf),
               new_info=describe(repo, args.tex, args.new, args.new_pdf),
               new_source=source(repo, args.tex, args.new))


if __name__ == '__main__':
    main()
