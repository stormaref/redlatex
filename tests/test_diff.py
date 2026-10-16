import pymupdf

from redlatex.cli import main

OLD = ["The quick brown fox jumps over the lazy dog. " * 12,
       "A second paragraph that stays exactly the same in both versions of the document. " * 6]
NEW = ["The quick brown fox leaps over the sleepy dog. " * 12,
       "A second paragraph that stays exactly the same in both versions of the document. " * 6,
       "An entirely new closing paragraph appears only in the revised version of the text. " * 6]


def make_pdf(path, paragraphs):
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for para in paragraphs:
        page.insert_textbox(pymupdf.Rect(72, y, 523, y + 200), para, fontsize=11)
        y += 210
    doc.save(path)


def test_diff_of_two_pdfs(tmp_path):
    old, new, out = tmp_path / "old.pdf", tmp_path / "new.pdf", tmp_path / "diff.pdf"
    make_pdf(old, OLD)
    make_pdf(new, NEW)
    main(["--old-pdf", str(old), "--new-pdf", str(new), "--out", str(out), "--repo", str(tmp_path)])
    assert out.exists()
    with pymupdf.open(out) as doc:
        assert len(doc) >= 2  # cover page plus at least one spread


def test_matching_blocks_agrees_with_difflib_on_long_input():
    import difflib
    import random
    from redlatex.diff import matching_blocks
    r = random.Random(3)
    vocab = [f'w{i}' for i in range(400)]
    a = [r.choice(vocab) for _ in range(6000)]
    b = list(a)
    for k in range(0, 6000, 250):
        b[k:k + 3] = ['edit', 'here']
    ours = sum(n for _, _, n in matching_blocks(a, b))
    ref = sum(n for _, _, n in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    assert ours >= 0.99 * ref


def test_moved_paragraph_is_not_marked(tmp_path):
    from redlatex.diff import render
    para = ('This paragraph moves to a different place in the revised document but its words stay '
            'exactly the same so nothing in it should be marked. ')
    other = 'A steady paragraph that stays where it is in both versions of this short test document. ' * 3
    make_pdf(tmp_path / 'a.pdf', [para, other])
    make_pdf(tmp_path / 'b.pdf', [other, para])
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        render(tmp_path / 'a.pdf', tmp_path / 'b.pdf', tmp_path / 'd.pdf', 't', 'a', 'b', 'x', 'y')
    assert '-0 +0 words' in out.getvalue()


def test_changed_figure_is_detected(tmp_path):
    from redlatex.diff import changed_figures

    def plot(path, k):
        doc = pymupdf.open()
        page = doc.new_page()
        pts = [(100 + 20 * i, 300 - (i * i) * k) for i in range(10)]
        for p0, p1 in zip(pts, pts[1:]):
            page.draw_line(p0, p1, color=(0, 0, 1))
        doc.save(path)
    plot(tmp_path / 'a.pdf', 1)
    plot(tmp_path / 'b.pdf', 1)
    plot(tmp_path / 'c.pdf', 2)
    assert changed_figures(pymupdf.open(tmp_path / 'a.pdf'), pymupdf.open(tmp_path / 'b.pdf')) == ([], [])
    old, new = changed_figures(pymupdf.open(tmp_path / 'a.pdf'), pymupdf.open(tmp_path / 'c.pdf'))
    assert len(old) == 1 and len(new) == 1
