import io
import zipfile

from fastapi.testclient import TestClient

from redlatex.web import app

client = TestClient(app)


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_index_and_health():
    assert 'redlatex' in client.get('/').text
    assert client.get('/healthz').json() == {'ok': True}


def test_rejects_non_zip():
    r = client.post('/diff', files={'old': ('a.zip', b'nope'), 'new': ('b.zip', b'nope')})
    assert r.status_code == 400


def test_rejects_zip_slip():
    bad = make_zip({'../evil.tex': 'x'})
    r = client.post('/diff', files={'old': ('a.zip', bad), 'new': ('b.zip', bad)})
    assert r.status_code == 400 and 'unsafe' in r.text


def test_requires_main_tex():
    z = make_zip({'notes.txt': 'hi'})
    r = client.post('/diff', files={'old': ('a.zip', z), 'new': ('b.zip', z)})
    assert r.status_code == 400 and 'no main .tex' in r.text


def test_hung_compile_is_killed_with_children(tmp_path):
    import shutil
    import subprocess
    import pytest
    from redlatex.build import compile_tree
    if not shutil.which('latexmk'):
        pytest.skip('latexmk not installed')
    (tmp_path / 'main.tex').write_text(r'\documentclass{article}\begin{document}\def\a{\a}\a\end{document}')
    with pytest.raises(SystemExit, match='timed out'):
        compile_tree(tmp_path, 'main.tex', tmp_path / 'out', timeout=3)
    assert subprocess.run(['pgrep', '-f', f'outdir={tmp_path}'], capture_output=True).returncode == 1


def test_oversized_body_rejected_before_reading():
    r = client.post('/diff', content=b'x', headers={'content-length': str(300 * 1024 * 1024),
                                                    'content-type': 'multipart/form-data; boundary=x'})
    assert r.status_code == 413


def test_page_cap_and_render_subprocess(monkeypatch):
    import shutil
    import pytest
    if not shutil.which('latexmk'):
        pytest.skip('latexmk not installed')
    import redlatex.web as web
    tex = lambda body: make_zip({'main.tex': r'\documentclass{article}\begin{document}' + body + r'\end{document}'})
    old, new = tex('Hello world, the first version of this text.'), tex('Hello brave world, the second version of this text.')
    r = client.post('/diff', files={'old': ('a.zip', old), 'new': ('b.zip', new)})
    assert r.status_code == 200 and r.content.startswith(b'%PDF')
    monkeypatch.setattr(web, 'MAX_PAGES', 0)
    r = client.post('/diff', files={'old': ('a.zip', old), 'new': ('b.zip', new)})
    assert r.status_code == 413 and 'limit is 0' in r.text


def _tex(body):
    return (r'\documentclass{article}\begin{document}' + body + r'\end{document}').encode()


def _need_latexmk():
    import shutil
    import pytest
    if not shutil.which('latexmk'):
        pytest.skip('latexmk not installed')


def test_security_headers():
    r = client.get('/')
    assert r.headers['x-content-type-options'] == 'nosniff'
    assert r.headers['x-frame-options'] == 'DENY'
    csp = r.headers['content-security-policy']
    assert "default-src 'none'" in csp and 'sha256-' in csp and "'unsafe-inline'" not in csp
    assert 'docs' not in client.get('/docs').text.lower() or client.get('/docs').status_code == 404


def test_single_tex_files_and_counter(tmp_path, monkeypatch):
    _need_latexmk()
    import redlatex.web as web
    from redlatex.counter import Counter
    monkeypatch.setattr(web, 'counter', Counter(tmp_path / 'c.json'))
    assert client.get('/api/stats').json() == {'documents': 0}
    r = client.post('/diff', files={'old': ('draft1.tex', _tex('Hello world, the first version of this text.')),
                                    'new': ('draft2.tex', _tex('Hello brave world, the second version of this text.'))})
    assert r.status_code == 200 and r.content.startswith(b'%PDF')
    assert client.get('/api/stats').json() == {'documents': 1}
    assert Counter(tmp_path / 'c.json').value == 1          # persisted
    bad = client.post('/diff', files={'old': ('x.txt', b'hi'), 'new': ('y.tex', _tex('x'))})
    assert bad.status_code == 400 and client.get('/api/stats').json() == {'documents': 1}


def test_compile_error_does_not_leak_paths():
    _need_latexmk()
    broken = make_zip({'main.tex': r'\documentclass{article}\begin{document}\undefinedmacro\end{document}'.replace('\\undefinedmacro', '\\input{nothere.tex}')})
    r = client.post('/diff', files={'old': ('a.zip', broken), 'new': ('b.zip', broken)})
    assert r.status_code == 422 and 'redlatex-' not in r.text and '/tmp' not in r.text and '/var' not in r.text


def test_hourly_rate_limit(monkeypatch):
    import redlatex.web as web
    monkeypatch.setattr(web, 'RATE_PER_HOUR', 2)
    web.recent.clear()
    z = make_zip({'notes.txt': 'hi'})
    codes = [client.post('/diff', files={'old': ('a.zip', z), 'new': ('b.zip', z)}).status_code for _ in range(3)]
    assert codes == [400, 400, 429]
    web.recent.clear()


def test_compile_runs_in_sandbox_env(tmp_path):
    from redlatex.build import sandbox_env
    env = sandbox_env(tmp_path)
    assert env['openin_any'] == 'p' and env['shell_escape'] == 'f' and 'AWS_SECRET_ACCESS_KEY' not in env
