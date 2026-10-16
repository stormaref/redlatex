"""Word-level visual diff of two PDFs.

Words are extracted from both compiled PDFs and compared with difflib (body text and small print such as
captions, tables and footnotes as separate streams). Revised pages are paired with the earlier page that
shares the most words, changed words are highlighted on copies of the pages, and the pairs are laid out
side by side, preceded by a cover page with an index of changes per section.
"""
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

RED_FILL = (1.0, 0.80, 0.78)
GREEN_FILL = (0.76, 0.93, 0.79)
RED = (0.75, 0.13, 0.11)
GREEN = (0.08, 0.50, 0.20)
GREY = (0.45, 0.45, 0.45)
MIN_PAIR_WORDS = 25    # matched words needed before two pages count as counterparts
SMALL_GAP = 0.9        # words this much smaller than body text (captions, tables, footnotes) are compared separately
HEADER, GUTTER = 34, 22
SECTION_RE = re.compile(r'\s*\\section\*?\{')


@dataclass
class Token:
    text: str
    page: int
    line: tuple
    small: bool                                   # footnote, caption, table or figure text
    rects: list = field(default_factory=list)   # [(page, Rect, line)]


def extract(doc):
    """Words in reading order with their font class; page numbers dropped."""
    toks, sizes = [], {}
    for pno, page in enumerate(doc):
        tp = page.get_textpage()
        span_size = {}
        for blk in page.get_text('dict', textpage=tp)['blocks']:
            for ln_no, ln in enumerate(blk.get('lines', [])):
                span_size[(blk['number'], ln_no)] = [(sp['bbox'][0], sp['bbox'][2], sp['size']) for sp in ln['spans']]
                for sp in ln['spans']:
                    sizes[round(sp['size'])] = sizes.get(round(sp['size']), 0) + len(sp['text'])
        words = page.get_text('words', textpage=tp)
        lines = {}
        for w in words:
            lines.setdefault((w[5], w[6]), []).append(w)
        footer = {k for k, ws in lines.items()
                  if min(w[1] for w in ws) > 0.86 * page.rect.height and all(w[4].isdigit() for w in ws)}
        for w in words:
            if (w[5], w[6]) in footer:
                continue
            cx = (w[0] + w[2]) / 2
            spans = span_size.get((w[5], w[6]), [])
            size = next((sz for x0, x1, sz in spans if x0 - 0.5 <= cx <= x1 + 0.5), spans[0][2] if spans else 0)
            line = (pno, w[5], w[6])
            toks.append(Token(w[4], pno, line, size, [(pno, pymupdf.Rect(w[:4]), line)]))
    body = max(sizes, key=sizes.get) if sizes else 12
    for t in toks:
        t.small = t.small < body - SMALL_GAP
    return toks


def rejoin(toks):
    """Glue words hyphenated across a line (or page) break back together."""
    out = []
    for t in toks:
        prev = out[-1] if out else None
        if prev and prev.text.endswith('-') and len(prev.text) > 1 and prev.line != t.line \
                and t.text[:1].islower() and prev.text[-2:-1] != '-':
            prev.text = prev.text[:-1] + t.text
            prev.rects += t.rects
        else:
            out.append(t)
    return out


def word_ops(a, b):
    """Opcodes with tiny unchanged islands (<= 2 words) between two changes folded into the change."""
    ops = [list(o) for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]
    for k in range(1, len(ops) - 1):
        tag, i1, i2, _, _ = ops[k]
        if tag == 'equal' and ops[k - 1][0] != 'equal' and ops[k + 1][0] != 'equal' and i2 - i1 <= 2:
            ops[k][0] = 'replace'
    merged = []
    for o in ops:
        if merged and o[0] != 'equal' and merged[-1][0] != 'equal':
            merged[-1] = ['replace', merged[-1][1], o[2], merged[-1][3], o[4]]
        else:
            merged.append(o)
    return merged


# ---------------------------------------------------------------- marking the pages

def mark(doc, runs, fill, bar_color, markers):
    """Highlight runs of changed tokens (one box per line of a run), add change bars and position markers."""
    bars = {}
    for run in runs:
        boxes = {}
        for t in run:
            for pno, r, line in t.rects:
                boxes[line] = boxes[line] | r if line in boxes else pymupdf.Rect(r)
        for (pno, *_), r in boxes.items():
            doc[pno].draw_rect(pymupdf.Rect(r.x0 - 0.8, r.y0 - 0.6, r.x1 + 0.8, r.y1 + 0.6),
                               color=None, fill=fill, overlay=False)
            bars.setdefault(pno, set()).add((round(r.y0, 1), round(r.y1, 1)))
    for pno, x, y0, y1 in markers:
        doc[pno].draw_line((x, y0 - 1), (x, y1 + 1), color=bar_color, width=1.4)
        bars.setdefault(pno, set()).add((round(y0, 1), round(y1, 1)))
    for pno, ys in bars.items():
        for y0, y1 in ys:
            doc[pno].draw_rect(pymupdf.Rect(28, y0 - 1, 31.5, y1 + 1), color=None, fill=bar_color)


def marker_at(toks, k):
    """Where text is missing on this side: a caret before token k (or after the last token)."""
    if not toks:
        return None
    if k < len(toks):
        pno, r, _ = toks[k].rects[0]
        return pno, r.x0 - 1.2, r.y0, r.y1
    pno, r, _ = toks[-1].rects[-1]
    return pno, r.x1 + 1.2, r.y0, r.y1


# ---------------------------------------------------------------- pairing pages

def pair_pages(n_old, n_new, streams):
    """Spreads (old page or None, new page or None) in reading order of both documents."""
    weight = {}
    for old_t, new_t, ops in streams:
        for tag, i1, i2, j1, j2 in ops:
            if tag == 'equal':
                for a, b in zip(range(i1, i2), range(j1, j2)):
                    key = (old_t[a].page, new_t[b].page)
                    weight[key] = weight.get(key, 0) + 1
    best_old = {}
    for (p, q), w in weight.items():
        if w >= MIN_PAIR_WORDS and w > weight.get((best_old.get(q), q), 0):
            best_old[q] = p
    best_new = {}
    for (p, q), w in weight.items():
        if w >= MIN_PAIR_WORDS and w > weight.get((p, best_new.get(p)), 0):
            best_new[p] = q
    spreads, shown, last = [], set(), -1
    for q in range(n_new):
        p = best_old.get(q)
        if p is not None and p < last:
            p = last if weight.get((last, q), 0) >= MIN_PAIR_WORDS else None
        if p is not None:
            # earlier old pages that no new page is paired with: show them before this spread
            for skipped in range(last + 1, p):
                if skipped not in shown:
                    partner = best_new.get(skipped)
                    spreads.append((skipped, partner if partner is not None and partner <= q else None))
                    shown.add(skipped)
            last = p
            shown.add(p)
        spreads.append((p, q))
    for skipped in range(last + 1, n_old):
        spreads.append((skipped, None))
    # an unmatched old page next to an unmatched new page (e.g. a page of figures): show them together
    merged = []
    for p, q in spreads:
        if merged and ((merged[-1][1] is None and p is None) or (merged[-1][0] is None and q is None)):
            prev = merged.pop()
            merged.append((prev[0] if prev[0] is not None else p, prev[1] if prev[1] is not None else q))
        else:
            merged.append((p, q))
    return merged


# ---------------------------------------------------------------- sections

def outline(doc, src):
    """[(level, number, title, page, y)] from the PDF bookmarks, numbered like the document.

    Level-1 sections after \\appendix in the LaTeX source `src` (if given) are lettered A, B, ...
    """
    toc = doc.get_toc(simple=False)
    lines = [l for l in (src or '').split('\n') if not l.lstrip().startswith('%')]
    k = next((n for n, l in enumerate(lines) if l.strip().startswith('\\appendix')), None)
    n_app = sum(bool(SECTION_RE.match(l)) for l in lines[k:]) if k is not None else 0
    n_top = sum(1 for e in toc if e[0] == 1)
    out, counters, top = [], [0, 0, 0, 0], 0
    for lvl, title, page, info in toc:
        lvl = min(lvl, 4)
        counters[lvl - 1] += 1
        counters[lvl:] = [0] * (4 - lvl)
        if lvl == 1:
            top += 1
        appendix = top > n_top - n_app
        head = chr(ord('A') + top - (n_top - n_app) - 1) if appendix else str(counters[0])
        num = '.'.join([head] + [str(c) for c in counters[1:lvl]])
        to = info.get('to')
        y = doc[page - 1].rect.height - to.y if to is not None else 0
        out.append((lvl, num, title, page - 1, y))
    return out


def section_of(sections, page, y):
    idx = None
    for k, (_, _, _, p, sy) in enumerate(sections):
        if (p, sy - 2) <= (page, y):
            idx = k
    return idx


def text(page, x, y, s, size=9, color=(0, 0, 0), bold=False):
    page.insert_text((x, y), s, fontsize=size, fontname='hebo' if bold else 'helv', color=color)


def swatch(page, x, y, fill, label, size=9):
    page.draw_rect(pymupdf.Rect(x, y - size + 1, x + 22, y + 2.5), color=None, fill=fill)
    text(page, x + 28, y, label, size)


def render(old_pdf, new_pdf, out_path, title, old_label, new_label, old_info, new_info, new_source=None):
    """Write the side-by-side diff of two PDFs to `out_path` and print a one-line summary.

    `old_info` / `new_info` describe each version on the cover page (e.g. 'abc1234, 2026-01-31');
    `new_source` is the LaTeX source of the revised version, used to letter appendix sections.
    """
    old_doc, new_doc = pymupdf.open(old_pdf), pymupdf.open(new_pdf)
    old_all, new_all = extract(old_doc), extract(new_doc)
    # body text and small print (captions, tables, footnotes) are compared as separate streams, so a
    # float or footnote that lands at a different point of the text flow does not look like an edit
    streams = []
    for small in (False, True):
        old_t = rejoin([t for t in old_all if t.small == small])
        new_t = rejoin([t for t in new_all if t.small == small])
        streams.append((old_t, new_t, word_ops([t.text for t in old_t], [t.text for t in new_t])))

    sections = outline(new_doc, new_source)
    removed, added, old_markers, new_markers, per_sec = [], [], [], [], {}
    for old_t, new_t, ops in streams:
        for tag, i1, i2, j1, j2 in ops:
            if tag == 'equal':
                continue
            removed.append(old_t[i1:i2])
            added.append(new_t[j1:j2])
            if i1 == i2 and (m := marker_at(old_t, i1)):
                old_markers.append(m)
            if j1 == j2 and (m := marker_at(new_t, j1)):
                new_markers.append(m)
            # words changed per section of the revised version (removals counted where they used to be)
            if new_t:
                pno, r, _ = new_t[min(j1, len(new_t) - 1)].rects[0]
                sec = section_of(sections, pno, r.y0)
                per_sec[sec] = (per_sec.get(sec, (0, 0))[0] + i2 - i1, per_sec.get(sec, (0, 0))[1])
            for t in new_t[j1:j2]:
                pno, r, _ = t.rects[0]
                sec = section_of(sections, pno, r.y0)
                per_sec[sec] = (per_sec.get(sec, (0, 0))[0], per_sec.get(sec, (0, 0))[1] + 1)
    spreads = pair_pages(len(old_doc), len(new_doc), streams)
    n_removed, n_added = sum(map(len, removed)), sum(map(len, added))

    mark(old_doc, removed, RED_FILL, RED, old_markers)
    mark(new_doc, added, GREEN_FILL, GREEN, new_markers)

    W, H = new_doc[0].rect.width, new_doc[0].rect.height
    PW, PH = 2 * W + GUTTER, H + HEADER
    out = pymupdf.open()

    # ---- cover page(s): legend, totals and the per-section index
    rows = [(s, *per_sec.get(k, (0, 0))) for k, s in enumerate(sections)]
    pre = per_sec.get(None, (0, 0))
    per_page = 34
    n_cover = max(1, -(-len(rows) // (2 * per_page)))
    first_spread = {}
    for k, (p, q) in enumerate(spreads):
        if q is not None:
            first_spread.setdefault(q, k)
    covers = [out.new_page(width=PW, height=PH) for _ in range(n_cover)]
    c = covers[0]
    text(c, 40, 62, title, 20, bold=True)
    text(c, 40, 86, f'Left: {old_label} ({old_info}), {len(old_doc)} pages.    '
                    f'Right: {new_label} ({new_info}), {len(new_doc)} pages.', 10)
    swatch(c, 40, 108, RED_FILL, 'removed or replaced text (left page)')
    swatch(c, 260, 108, GREEN_FILL, 'added or replacement text (right page)')
    c.draw_line((480, 99), (480, 110), color=RED, width=1.4)
    text(c, 488, 108, 'text removed here / added at this point on the other side', 9)
    c.draw_rect(pymupdf.Rect(800, 99, 803.5, 110), color=None, fill=GREY)
    text(c, 810, 108, 'margin bar: line with a change', 9)
    text(c, 40, 128, f'{n_removed} words removed, {n_added} words added. Each revised page is shown next to '
                     'the earlier page with the most text in common; earlier pages with no counterpart get a spread '
                     'of their own. Click a section to jump to it.', 9, GREY)
    text(c, 40, 128 + 14, 'Changes are detected on the words of the compiled PDFs; edits inside graphics without '
                          'text are not marked.' + (f'  Title pages and front matter: -{pre[0]} +{pre[1]} words.'
                                                    if any(pre) else ''), 9, GREY)
    col_x = [40, 40 + PW / 2]
    links = []  # inserted once the spreads exist
    y0 = 172
    for k, (sec, rm, ad) in enumerate(rows):
        page_i, col, row = k // (2 * per_page), (k % (2 * per_page)) // per_page, k % per_page
        if page_i >= len(covers):
            break
        pg = covers[page_i]
        lvl, num, title, p, _ = sec
        x, y = col_x[col] + 14 * (lvl - 1), (y0 if page_i == 0 else 60) + 17 * row
        changed = rm or ad
        label = f'{num}  {title}'
        if len(label) > 72 - 2 * lvl:
            label = label[:70 - 2 * lvl] + '…'
        text(pg, x, y, label, 9, (0, 0, 0) if changed else GREY, bold=lvl == 1)
        if changed:
            text(pg, col_x[col] + 470, y, f'-{rm}', 9, RED)
            text(pg, col_x[col] + 515, y, f'+{ad}', 9, GREEN)
        target = n_cover + first_spread.get(p, 0)
        text(pg, col_x[col] + 560, y, f'p. {p + 1}', 8, GREY)
        links.append((page_i, target, pymupdf.Rect(x, y - 10, col_x[col] + 590, y + 3)))

    # ---- spreads
    for k, (p, q) in enumerate(spreads):
        pg = out.new_page(width=PW, height=PH)
        left, right = pymupdf.Rect(0, HEADER, W, HEADER + H), pymupdf.Rect(W + GUTTER, HEADER, PW, HEADER + H)
        pg.draw_rect(pymupdf.Rect(0, 0, W, HEADER - 4), color=None, fill=(0.98, 0.91, 0.90))
        pg.draw_rect(pymupdf.Rect(W + GUTTER, 0, PW, HEADER - 4), color=None, fill=(0.89, 0.96, 0.90))
        text(pg, 14, 20, f'BEFORE  ·  {old_label}' + (f'  ·  page {p + 1} of {len(old_doc)}' if p is not None else ''),
             10, RED, bold=True)
        text(pg, W + GUTTER + 14, 20, f'AFTER  ·  {new_label}' +
             (f'  ·  page {q + 1} of {len(new_doc)}' if q is not None else ''), 10, GREEN, bold=True)
        text(pg, W - 60, 20, f'{k + 1} / {len(spreads)}', 8, GREY)
        for rect, doc, pno, note in ((left, old_doc, p, 'No earlier counterpart: new material'),
                                     (right, new_doc, q, 'No revised counterpart: text removed or rewritten')):
            if pno is None:
                pg.draw_rect(rect, color=None, fill=(0.96, 0.96, 0.96))
                text(pg, rect.x0 + 40, rect.y0 + H / 2, note, 12, GREY)
            else:
                pg.show_pdf_page(rect, doc, pno)
            pg.draw_rect(rect, color=(0.75, 0.75, 0.75), width=0.6)

    for page_i, target, rect in links:
        out[page_i].insert_link({'kind': pymupdf.LINK_GOTO, 'page': target, 'from': rect})

    # ---- bookmarks: one per section, pointing at the spread where it starts
    toc = [[1, 'Index of changes', 1]]
    for k, (lvl, num, title, p, _) in enumerate(sections):
        rm, ad = per_sec.get(k, (0, 0))
        toc.append([lvl, f'{num} {title}' + (f'  (-{rm} +{ad})' if rm or ad else ''),
                    n_cover + first_spread.get(p, 0) + 1])
    out.set_toc(toc)
    out.set_metadata({'title': title})
    out.save(out_path, garbage=4, deflate=True)
    print(f'{out_path}: {len(spreads)} spreads, -{n_removed} +{n_added} words')
