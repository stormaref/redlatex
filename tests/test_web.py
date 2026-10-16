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
