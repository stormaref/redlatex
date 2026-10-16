# Security policy

redlatex's web app compiles LaTeX uploaded by users, so reports about unpacking, compilation or file access
are taken seriously.

## Reporting a vulnerability

Please report it privately through GitHub: **Security → Report a vulnerability** on
<https://github.com/stormaref/redlatex/security/advisories/new>. Do not open a public issue for a
vulnerability. I aim to reply within a week.

## Scope

In scope: path traversal or resource exhaustion through uploaded archives, escaping the TeX file-access
restrictions or shell-escape protection, and anything that exposes other users' files.

The hosted instance at <https://redlatex.onrender.com> is a small demo; please do not load-test it.
