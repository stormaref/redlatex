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
