"""Web front end: upload two versions of a LaTeX document (a .zip project or a single .tex), get the diff PDF."""
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path

import pymupdf
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

from .build import compile_tree, run_killing
from .counter import default_counter

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_TEX_BYTES = 5 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000
COMPILE_TIMEOUT = int(os.environ.get('REDLATEX_COMPILE_TIMEOUT', 45))     # per version
COMPILE_BUDGET = int(os.environ.get('REDLATEX_COMPILE_BUDGET', 90))       # both versions together
RENDER_TIMEOUT = int(os.environ.get('REDLATEX_RENDER_TIMEOUT', 90))
MAX_JOBS = int(os.environ.get('REDLATEX_MAX_JOBS', 2))
MAX_PAGES = int(os.environ.get('REDLATEX_MAX_PAGES', 100))
RATE_PER_HOUR = int(os.environ.get('REDLATEX_RATE_PER_HOUR', 20))
QUEUE_WAIT = 20
MAX_BODY = 2 * MAX_ZIP_BYTES + (1 << 20)

active = set()
active_lock = threading.Lock()
recent = defaultdict(deque)
jobs = threading.BoundedSemaphore(MAX_JOBS)
counter = default_counter()

app = FastAPI(title='redlatex', docs_url=None, redoc_url=None, openapi_url=None)

PAGE = (Path(__file__).parent / 'index.html').read_text()


def _hashes(tag):
    """CSP hashes of the page's inline <style>/<script>, so no 'unsafe-inline' is needed."""
    return ' '.join("'sha256-%s'" % base64.b64encode(hashlib.sha256(m.encode()).digest()).decode()
                    for m in re.findall(rf'<{tag}[^>]*>(.*?)</{tag}>', PAGE, re.S))


PAGE_CSP = (f"default-src 'none'; script-src {_hashes('script')}; style-src {_hashes('style')}; "
            "img-src 'self' data:; connect-src 'self'; frame-src blob:; object-src blob:; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")
HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()',
    'Strict-Transport-Security': 'max-age=31536000',
    'Cross-Origin-Opener-Policy': 'same-origin',
}


@app.middleware('http')
async def guard(request: Request, call_next):
    """Refuse oversized uploads from the header alone (before the body is read) and add security headers."""
    try:
        too_big = int(request.headers.get('content-length') or 0) > MAX_BODY
    except ValueError:
        too_big = True
    if too_big:
        response = JSONResponse({'detail': f'upload is larger than {MAX_BODY >> 20} MB'}, status_code=413)
    else:
        response = await call_next(request)
    response.headers.update(HEADERS)
    if request.url.path == '/':
        response.headers['Content-Security-Policy'] = PAGE_CSP
    else:
        response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
    return response


@app.get('/', response_class=HTMLResponse)
def index():
    return PAGE


@app.get('/healthz')
def healthz():
    return {'ok': True}


@app.get('/api/stats')
def stats():
    return JSONResponse({'documents': counter.value}, headers={'Cache-Control': 'no-store'})


def read_upload(upload, dest_file, limit):
    """Copy an upload to disk, refusing anything over `limit` bytes."""
    size = 0
    with dest_file.open('wb') as f:
        while chunk := upload.file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f'{upload.filename}: file is larger than {limit >> 20} MB')
            f.write(chunk)


def unpack(upload, dest):
    """Store an upload in `dest` and return (project root, is_single_file).

    A .zip is extracted safely (a lone top folder is unwrapped); a single .tex file is used as is.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix('.upload')
    read_upload(upload, raw, MAX_ZIP_BYTES)
    try:
        if zipfile.is_zipfile(raw):
            return extract_zip(raw, dest, upload.filename), False
        if (upload.filename or '').lower().endswith('.tex'):
            if raw.stat().st_size > MAX_TEX_BYTES:
                raise HTTPException(413, f'{upload.filename}: .tex file is larger than {MAX_TEX_BYTES >> 20} MB')
            name = re.sub(r'[^A-Za-z0-9._-]', '_', Path(upload.filename).name)[-80:] or 'main.tex'
            dest.mkdir()
            shutil.copyfile(raw, dest / name)
            return dest, True
        raise HTTPException(400, f'{upload.filename}: upload a .zip of the project or a single .tex file')
    finally:
        raw.unlink(missing_ok=True)


def extract_zip(zpath, dest, label):
    try:
        with zipfile.ZipFile(zpath) as z:
            infos = z.infolist()
            if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
                raise HTTPException(413, f'{label}: archive is too large when unpacked')
            dest.mkdir()
            base = dest.resolve()
            for info in infos:
                name = info.filename
                bad = name.startswith(('/', '\\')) or '\\' in name or '\x00' in name or re.match(r'^[A-Za-z]:', name)
                target = (base / name).resolve()
                if bad or (base != target and base not in target.parents):
                    raise HTTPException(400, f'{label}: unsafe path in archive')
            z.extractall(dest)
    except zipfile.BadZipFile:
        raise HTTPException(400, f'{label} is not a valid zip file')
    entries = [p for p in dest.iterdir() if p.name != '__MACOSX']
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


def find_main(root, requested, single):
    """Relative path of the main .tex file: the requested one, else main.tex, else the shallowest with \\documentclass."""
    texes = [p for p in root.rglob('*.tex') if '__MACOSX' not in p.parts]
    if single:
        return texes[0].relative_to(root)
    if requested:
        path = (root / requested).resolve()
        if root.resolve() not in path.parents or path.suffix != '.tex' or not path.is_file():
            raise HTTPException(400, f'main file {requested!r} not found in the archive')
        return path.relative_to(root.resolve())
    if (root / 'main.tex').is_file():
        return Path('main.tex')
    for p in sorted(texes, key=lambda p: (len(p.relative_to(root).parts), str(p))):
        if re.search(r'^\s*\\documentclass', p.read_text(errors='ignore'), re.M):
            return p.relative_to(root)
    raise HTTPException(400, 'no main .tex file with \\documentclass found; enter its name explicitly')


def check_rate(client):
    """At most RATE_PER_HOUR attempts per client per hour."""
    now = time.monotonic()
    with active_lock:
        q = recent[client]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= RATE_PER_HOUR:
            wait = int(3600 - (now - q[0])) + 1
            raise HTTPException(429, f'Hourly limit of {RATE_PER_HOUR} diffs reached. Try again in {wait // 60 + 1} min.',
                                headers={'Retry-After': str(wait)})
        q.append(now)
        if len(recent) > 10000:
            for k in [k for k, v in recent.items() if not v or now - v[-1] > 3600]:
                del recent[k]


@app.post('/diff')
def diff(request: Request, old: UploadFile, new: UploadFile, tex: str = Form(''), title: str = Form('')):
    client = request.client.host if request.client else '?'
    check_rate(client)
    with active_lock:
        if client in active:
            raise HTTPException(429, 'You already have a diff running. Wait for it to finish first.')
        active.add(client)
    try:
        return limited_diff(old, new, tex.strip()[:200], title.strip()[:120])
    finally:
        with active_lock:
            active.discard(client)


def limited_diff(old, new, tex, title):
    if not jobs.acquire(timeout=QUEUE_WAIT):
        raise HTTPException(503, 'The server is busy compiling other documents. Please try again in a minute.',
                            headers={'Retry-After': '30'})
    try:
        return make_diff(old, new, tex, title)
    finally:
        jobs.release()


def label(upload, fallback):
    return re.sub(r'[^A-Za-z0-9._ -]', '_', Path(upload.filename or fallback).stem)[:60] or fallback


def make_diff(old, new, tex, title):
    work = Path(tempfile.mkdtemp(prefix='redlatex-'))
    cleanup = BackgroundTask(shutil.rmtree, work, ignore_errors=True)
    deadline = time.monotonic() + COMPILE_BUDGET
    try:
        pdfs, new_source = [], None
        for name, upload in (('old', old), ('new', new)):
            root, single = unpack(upload, work / name)
            main = find_main(root, tex, single)
            if name == 'new':
                new_source = (root / main).read_text(errors='ignore')
            left = int(deadline - time.monotonic())
            if left < 5:
                raise HTTPException(422, f'compiling both versions exceeded the {COMPILE_BUDGET}s limit')
            try:
                pdfs.append(compile_tree(root, main, work / f'{name}-out', timeout=min(COMPILE_TIMEOUT, left)))
            except SystemExit as e:
                raise HTTPException(422, f'{name} version: {e}')
        for name, pdf in zip(('old', 'new'), pdfs):
            with pymupdf.open(pdf) as doc:
                if doc.page_count > MAX_PAGES:
                    raise HTTPException(413, f'{name} version has {doc.page_count} pages; the limit is {MAX_PAGES}')
        out = work / 'redlatex-diff.pdf'
        (work / 'new-source.tex').write_text(new_source or '')
        cmd = [sys.executable, '-m', 'redlatex.cli', '--old-pdf', str(pdfs[0]), '--new-pdf', str(pdfs[1]),
               '--new-source', str(work / 'new-source.tex'), '--out', str(out),
               f'--title={title or "Document changes"}',
               f'--old-label={label(old, "old")}', f'--new-label={label(new, "new")}']
        try:
            code = run_killing(cmd, work, RENDER_TIMEOUT, env={k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ},
                               cpu=RENDER_TIMEOUT + 15, fsize_mb=200)
        except subprocess.TimeoutExpired:
            raise HTTPException(422, f'building the diff timed out after {RENDER_TIMEOUT}s; try a shorter document')
        if code or not out.exists():
            raise HTTPException(500, 'building the diff failed')
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    counter.increment()
    return FileResponse(out, media_type='application/pdf', filename='redlatex-diff.pdf', background=cleanup)
