"""Web front end: upload two zipped LaTeX projects, get back the diff PDF."""
import os
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

from .build import compile_tree
from .diff import render

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000
COMPILE_TIMEOUT = int(os.environ.get('REDLATEX_COMPILE_TIMEOUT', 60))
MAX_JOBS = int(os.environ.get('REDLATEX_MAX_JOBS', 2))
QUEUE_WAIT = 20
jobs = threading.BoundedSemaphore(MAX_JOBS)

app = FastAPI(title='redlatex', docs_url=None, redoc_url=None)

PAGE = (Path(__file__).parent / 'index.html').read_text()


@app.get('/', response_class=HTMLResponse)
def index():
    return PAGE


@app.get('/healthz')
def healthz():
    return {'ok': True}


def unpack(upload, dest):
    """Safely extract an uploaded zip into `dest` and return the project root (a lone top folder is unwrapped)."""
    zpath = dest.with_suffix('.zip')
    size = 0
    with zpath.open('wb') as f:
        while chunk := upload.file.read(1 << 20):
            size += len(chunk)
            if size > MAX_ZIP_BYTES:
                raise HTTPException(413, f'{upload.filename}: zip is larger than {MAX_ZIP_BYTES >> 20} MB')
            f.write(chunk)
    try:
        with zipfile.ZipFile(zpath) as z:
            infos = z.infolist()
            if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
                raise HTTPException(413, f'{upload.filename}: archive is too large when unpacked')
            dest.mkdir(parents=True)
            base = dest.resolve()
            for info in infos:
                target = (base / info.filename).resolve()
                if base != target and base not in target.parents:
                    raise HTTPException(400, f'{upload.filename}: unsafe path {info.filename!r}')
            z.extractall(dest)
    except zipfile.BadZipFile:
        raise HTTPException(400, f'{upload.filename} is not a valid zip file')
    finally:
        zpath.unlink(missing_ok=True)
    entries = [p for p in dest.iterdir() if p.name != '__MACOSX']
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


def find_main(root, requested):
    """Relative path of the main .tex file: the requested one, else main.tex, else the shallowest with \\documentclass."""
    if requested:
        path = (root / requested).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            raise HTTPException(400, f'main file {requested!r} not found in the archive')
        return path.relative_to(root.resolve())
    if (root / 'main.tex').is_file():
        return Path('main.tex')
    cands = sorted((p for p in root.rglob('*.tex') if '__MACOSX' not in p.parts),
                   key=lambda p: (len(p.relative_to(root).parts), str(p)))
    for p in cands:
        if re.search(r'^\s*\\documentclass', p.read_text(errors='ignore'), re.M):
            return p.relative_to(root)
    raise HTTPException(400, 'no main .tex file with \\documentclass found; enter its name explicitly')


@app.post('/diff')
def diff(old: UploadFile, new: UploadFile, tex: str = Form(''), title: str = Form('')):
    if not jobs.acquire(timeout=QUEUE_WAIT):
        raise HTTPException(503, 'The server is busy compiling other documents. Please try again in a minute.',
                            headers={'Retry-After': '30'})
    try:
        return make_diff(old, new, tex, title)
    finally:
        jobs.release()


def make_diff(old, new, tex, title):
    work = Path(tempfile.mkdtemp(prefix='redlatex-'))
    cleanup = BackgroundTask(shutil.rmtree, work, ignore_errors=True)
    try:
        pdfs, new_source = [], None
        for name, upload in (('old', old), ('new', new)):
            root = unpack(upload, work / name)
            main = find_main(root, tex.strip())
            if name == 'new':
                new_source = (root / main).read_text(errors='ignore')
            try:
                pdfs.append(compile_tree(root, main, work / f'{name}-out', timeout=COMPILE_TIMEOUT))
            except SystemExit as e:
                raise HTTPException(422, f'{name} version: {e}')
        out = work / 'redlatex-diff.pdf'
        render(pdfs[0], pdfs[1], out, title=title.strip() or 'Document changes',
               old_label=Path(old.filename or 'old').stem, new_label=Path(new.filename or 'new').stem,
               old_info='uploaded zip', new_info='uploaded zip', new_source=new_source)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return FileResponse(out, media_type='application/pdf', filename='redlatex-diff.pdf', background=cleanup)
