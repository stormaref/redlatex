FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # paranoid TeX file access: no reading or writing outside the project tree
    openin_any=p \
    openout_any=p

RUN apt-get update && apt-get install -y --no-install-recommends \
        latexmk \
        texlive-latex-base texlive-latex-recommended texlive-latex-extra \
        texlive-fonts-recommended texlive-fonts-extra \
        texlive-science texlive-pictures texlive-publishers \
        texlive-bibtex-extra texlive-xetex texlive-lang-european biber \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra web

RUN useradd --create-home --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH" PORT=10000
EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request as u;u.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["sh", "-c", "exec uvicorn redlatex.web:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-server-header --proxy-headers --forwarded-allow-ips='*'"]
