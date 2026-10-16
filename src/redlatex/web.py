"""Web front end: diff two versions of a LaTeX document given as uploads (.zip project or single .tex) or as
two refs of a public GitHub repository, with progress reporting."""
import base64
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path

import httpx
import pymupdf
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .build import compile_tree, run_killing

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_TEX_BYTES = 5 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000
COMPILE_TIMEOUT = int(os.environ.get('REDLATEX_COMPILE_TIMEOUT', 45))     # per version
COMPILE_BUDGET = int(os.environ.get('REDLATEX_COMPILE_BUDGET', 90))       # both versions together
RENDER_TIMEOUT = int(os.environ.get('REDLATEX_RENDER_TIMEOUT', 90))
MAX_JOBS = int(os.environ.get('REDLATEX_MAX_JOBS', 2))
MAX_PAGES = int(os.environ.get('REDLATEX_MAX_PAGES', 300))
RATE_PER_HOUR = int(os.environ.get('REDLATEX_RATE_PER_HOUR', 20))
MAX_WAITING = 10            # jobs waiting for a slot before new ones are refused
QUEUE_WAIT = 20             # seconds the synchronous /diff endpoint waits for a slot
JOB_QUEUE_WAIT = 300        # seconds a background job waits for a slot
JOB_TTL = 15 * 60           # finished jobs and their files are dropped after this
GITHUB_TIMEOUT = 30
MAX_BODY = 2 * MAX_ZIP_BYTES + (1 << 20)

active = set()
active_lock = threading.Lock()
recent = defaultdict(deque)
slots = threading.BoundedSemaphore(MAX_JOBS)

app = FastAPI(title='redlatex', docs_url=None, redoc_url=None, openapi_url=None)
STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')

# absolute URL of the site, for link previews (Open Graph / Twitter cards need absolute image URLs)
PUBLIC_URL = os.environ.get('REDLATEX_PUBLIC_URL', 'https://redlatex.onrender.com').rstrip('/')
PAGE = (Path(__file__).parent / 'index.html').read_text().replace('{{BASE}}', PUBLIC_URL)


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
    path = request.url.path
    if path.startswith('/static/') or path in ('/favicon.ico', '/apple-touch-icon.png'):
        response.headers.setdefault('Cache-Control', 'public, max-age=86400')
    if path == '/':
        response.headers['Content-Security-Policy'] = PAGE_CSP
    else:
        response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
    return response


@app.get('/', response_class=HTMLResponse)
def index():
    return PAGE


@app.get('/favicon.ico', include_in_schema=False)
def favicon():
    return FileResponse(STATIC / 'favicon-32.png', media_type='image/png')


@app.get('/apple-touch-icon.png', include_in_schema=False)
def touch_icon():
    return FileResponse(STATIC / 'apple-touch-icon.png', media_type='image/png')


@app.get('/robots.txt', response_class=PlainTextResponse)
def robots():
    return 'User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /diff\n'


@app.get('/healthz')
def healthz():
    return {'ok': True}


# ---------------------------------------------------------------- getting the sources

def read_stream(chunks, dest_file, limit, label):
    """Write byte chunks to disk, refusing anything over `limit` bytes."""
    size = 0
    with dest_file.open('wb') as f:
        for chunk in chunks:
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f'{label}: file is larger than {limit >> 20} MB')
            f.write(chunk)


def unpack(upload, dest):
    """Store an upload in `dest` and return (project root, is_single_file).

    A .zip is extracted safely (a lone top folder is unwrapped); a single .tex file is used as is.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix('.upload')
    read_stream(iter(lambda: upload.file.read(1 << 20), b''), raw, MAX_ZIP_BYTES, upload.filename)
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


GITHUB_REPO = re.compile(r'^(?:https?://(?:www\.)?github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/'
                         r'([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?(?:[?#].*)?$')
GITHUB_REF = re.compile(r'^[A-Za-z0-9._/-]{1,200}$')


def parse_github(repo, ref):
    """Validated (owner, repo, ref); only public github.com repositories are supported."""
    m = GITHUB_REPO.match(repo.strip())
    if not m or m.group(2) in ('.', '..'):
        raise HTTPException(400, 'enter the repository as owner/name or https://github.com/owner/name')
    ref = ref.strip()
    if not GITHUB_REF.match(ref) or '..' in ref or ref.startswith(('/', '-')) or ref.endswith('/'):
        raise HTTPException(400, f'{ref!r} is not a valid branch, tag or commit')
    return m.group(1), m.group(2), ref


def fetch_github(owner, repo, ref, dest):
    """Download a public repository at `ref` from codeload.github.com (the only host ever contacted)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix('.zip')
    url = f'https://codeload.github.com/{owner}/{repo}/zip/{ref}'
    label = f'{owner}/{repo}@{ref}'
    try:
        with httpx.stream('GET', url, timeout=GITHUB_TIMEOUT, follow_redirects=False) as r:
            if r.status_code == 404:
                raise HTTPException(400, f'{label} not found (only public repositories are supported)')
            if r.status_code != 200:
                raise HTTPException(502, f'GitHub answered {r.status_code} for {label}')
            read_stream(r.iter_bytes(1 << 20), raw, MAX_ZIP_BYTES, label)
    except httpx.HTTPError:
        raise HTTPException(502, f'could not download {label} from GitHub')
    try:
        return extract_zip(raw, dest, label)
    finally:
        raw.unlink(missing_ok=True)


def find_main(root, requested, single):
    """Relative path of the main .tex file: the requested one, else main.tex, else the shallowest with \\documentclass."""
    texes = [p for p in root.rglob('*.tex') if '__MACOSX' not in p.parts]
    if single:
        return texes[0].relative_to(root)
    if requested:
        path = (root / requested).resolve()
        if root.resolve() not in path.parents or path.suffix != '.tex' or not path.is_file():
            raise HTTPException(400, f'main file {requested!r} not found')
        return path.relative_to(root.resolve())
    if (root / 'main.tex').is_file():
        return Path('main.tex')
    for p in sorted(texes, key=lambda p: (len(p.relative_to(root).parts), str(p))):
        if re.search(r'^\s*\\documentclass', p.read_text(errors='ignore'), re.M):
            return p.relative_to(root)
    raise HTTPException(400, 'no main .tex file with \\documentclass found; enter its path under Options')


# ---------------------------------------------------------------- limits

def client_of(request):
    return request.client.host if request.client else '?'


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


def claim(client):
    """One diff at a time per client."""
    with active_lock:
        if client in active:
            raise HTTPException(429, 'You already have a diff running. Wait for it to finish first.')
        active.add(client)


def release(client):
    with active_lock:
        active.discard(client)


# ---------------------------------------------------------------- the pipeline

def label(name, fallback):
    return re.sub(r'[^A-Za-z0-9._@/ -]', '_', name or fallback)[:60] or fallback


def build_diff(work, sources, tex, title, labels, progress):
    """Compile both versions and build the diff PDF in `work`; `sources` are callables returning
    (root, single). Returns the output path; raises HTTPException with a user-facing message."""
    deadline = time.monotonic() + COMPILE_BUDGET
    pdfs, new_source = [], None
    for name, get in zip(('old', 'new'), sources):
        progress(f'fetch-{name}')
        root, single = get()
        main = find_main(root, tex, single)
        if name == 'new':
            new_source = (root / main).read_text(errors='ignore')
        left = int(deadline - time.monotonic())
        if left < 5:
            raise HTTPException(422, f'compiling both versions exceeded the {COMPILE_BUDGET}s limit')
        progress(f'compile-{name}')
        try:
            pdfs.append(compile_tree(root, main, work / f'{name}-out', timeout=min(COMPILE_TIMEOUT, left)))
        except SystemExit as e:
            raise HTTPException(422, f'{"earlier" if name == "old" else "revised"} version: {e}')
    for name, pdf in zip(('earlier', 'revised'), pdfs):
        with pymupdf.open(pdf) as doc:
            if doc.page_count > MAX_PAGES:
                raise HTTPException(413, f'{name} version has {doc.page_count} pages; the limit is {MAX_PAGES}')
    progress('diff')
    out = work / 'redlatex-diff.pdf'
    (work / 'new-source.tex').write_text(new_source or '')
    cmd = [sys.executable, '-m', 'redlatex.cli', '--old-pdf', str(pdfs[0]), '--new-pdf', str(pdfs[1]),
           '--new-source', str(work / 'new-source.tex'), '--out', str(out),
           f'--title={title or "Document changes"}', f'--old-label={labels[0]}', f'--new-label={labels[1]}']
    try:
        code = run_killing(cmd, work, RENDER_TIMEOUT, env={k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ},
                           cpu=RENDER_TIMEOUT + 15, fsize_mb=200)
    except subprocess.TimeoutExpired:
        raise HTTPException(422, f'building the diff timed out after {RENDER_TIMEOUT}s; try a shorter document')
    if code or not out.exists():
        raise HTTPException(500, 'building the diff failed')
    return out


def upload_sources(old, new, work):
    """Unpack both uploads now (they are closed once the request ends) and return source callables."""
    got = [unpack(old, work / 'old'), unpack(new, work / 'new')]
    return [lambda: got[0], lambda: got[1]], [label(Path(old.filename or '').stem, 'old'),
                                              label(Path(new.filename or '').stem, 'new')]


def github_sources(repo, old_ref, new_ref, work):
    (owner, name, a), (_, _, b) = parse_github(repo, old_ref), parse_github(repo, new_ref)
    return ([lambda: (fetch_github(owner, name, a, work / 'old'), False),
             lambda: (fetch_github(owner, name, b, work / 'new'), False)], [label(a, 'old'), label(b, 'new')])


@app.post('/diff')
def diff(request: Request, old: UploadFile, new: UploadFile, tex: str = Form(''), title: str = Form('')):
    """Synchronous API: upload two versions, receive the diff PDF in the response."""
    client = client_of(request)
    check_rate(client)
    claim(client)
    work = Path(tempfile.mkdtemp(prefix='redlatex-'))
    try:
        sources, labels = upload_sources(old, new, work)
        if not slots.acquire(timeout=QUEUE_WAIT):
            raise HTTPException(503, 'The server is busy compiling other documents. Please try again in a minute.',
                                headers={'Retry-After': '30'})
        try:
            out = build_diff(work, sources, tex.strip()[:200], title.strip()[:120], labels, lambda stage: None)
        finally:
            slots.release()
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        release(client)
    return FileResponse(out, media_type='application/pdf', filename='redlatex-diff.pdf',
                        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))


# ---------------------------------------------------------------- background jobs with progress

jobs = {}
jobs_lock = threading.Lock()


def sweep():
    """Drop jobs (and their files) that finished more than JOB_TTL ago."""
    now = time.monotonic()
    with jobs_lock:
        for jid in [j for j, job in jobs.items() if job['state'] in ('done', 'error') and now - job['ended'] > JOB_TTL]:
            shutil.rmtree(jobs.pop(jid)['work'], ignore_errors=True)


def run_job(jid, client, sources, tex, title, labels):
    job = jobs[jid]

    def progress(stage):
        job['state'] = stage

    try:
        if not slots.acquire(timeout=JOB_QUEUE_WAIT):
            raise HTTPException(503, 'The server stayed busy for too long. Please try again later.')
        try:
            job['out'] = build_diff(job['work'], sources, tex, title, labels, progress)
            job['state'] = 'done'
        finally:
            slots.release()
    except HTTPException as e:
        job['state'], job['error'] = 'error', e.detail
    except Exception:
        job['state'], job['error'] = 'error', 'building the diff failed'
    finally:
        job['ended'] = time.monotonic()
        release(client)
        if job['state'] == 'error':
            shutil.rmtree(job['work'], ignore_errors=True)


@app.post('/api/jobs')
def create_job(request: Request, mode: str = Form('upload'), old: UploadFile | None = None,
               new: UploadFile | None = None, repo: str = Form(''), old_ref: str = Form(''),
               new_ref: str = Form(''), tex: str = Form(''), title: str = Form('')):
    """Start a diff in the background; poll /api/jobs/{id} for progress, then fetch /api/jobs/{id}/pdf."""
    sweep()
    with jobs_lock:
        if sum(1 for j in jobs.values() if j['state'] == 'queued') >= MAX_WAITING:
            raise HTTPException(503, 'Too many diffs are waiting. Please try again in a few minutes.',
                                headers={'Retry-After': '120'})
    client = client_of(request)
    if mode == 'github':
        parse_github(repo, old_ref), parse_github(repo, new_ref)   # validate before counting the attempt
    elif not (old and new):
        raise HTTPException(400, 'choose both files')
    check_rate(client)
    claim(client)
    work = Path(tempfile.mkdtemp(prefix='redlatex-'))
    try:
        if mode == 'github':
            sources, labels = github_sources(repo, old_ref, new_ref, work)
        else:
            sources, labels = upload_sources(old, new, work)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        release(client)
        raise
    jid = secrets.token_urlsafe(18)
    with jobs_lock:
        jobs[jid] = {'state': 'queued', 'work': work, 'created': time.monotonic(), 'ended': 0, 'error': None}
    threading.Thread(target=run_job, args=(jid, client, sources, tex.strip()[:200], title.strip()[:120], labels),
                     daemon=True).start()
    return {'id': jid}


def get_job(jid):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, 'This diff has expired or does not exist.')
    return job


@app.get('/api/jobs/{jid}')
def job_status(jid: str):
    job = get_job(jid)
    body = {'state': job['state'], 'error': job['error']}
    if job['state'] == 'queued':
        with jobs_lock:
            body['ahead'] = sum(1 for j in jobs.values() if j['state'] == 'queued' and j['created'] < job['created'])
    return JSONResponse(body, headers={'Cache-Control': 'no-store'})


@app.get('/api/jobs/{jid}/pdf')
def job_pdf(jid: str):
    """The finished diff; it is deleted once sent."""
    job = get_job(jid)
    if job['state'] != 'done':
        raise HTTPException(409, 'The diff is not ready yet.')
    with jobs_lock:
        jobs.pop(jid, None)
    return FileResponse(job['out'], media_type='application/pdf', filename='redlatex-diff.pdf',
                        background=BackgroundTask(shutil.rmtree, job['work'], ignore_errors=True))
