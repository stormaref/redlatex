import pymupdf

from pdf_revision_diff.cli import main

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
