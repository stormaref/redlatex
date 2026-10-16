"""Word-level visual diff of two PDFs.

Words are extracted from both compiled PDFs and compared with an anchored word diff (body text and small
print such as captions, tables and footnotes as separate streams); text that only moved is not marked.
Revised pages are paired with the earlier page that shares the most words, changed words and changed
figures are highlighted on copies of the pages, and the pairs are laid out side by side, preceded by a
cover page with an index of changes per section.
"""
import bisect
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
MIN_PAIR_WORDS = 25    # matched words needed before two pages count as counterparts (less for short pages)
MOVE_MIN = 6           # words in a row that make removed + added text count as moved, not edited
FIG_DIFF = 0.002       # share of differing pixels that makes a figure count as changed
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


def _anchors(a, b, alo, ahi, blo, bhi, k=3):
    """Positions (i, j) of word k-grams that occur exactly once in both ranges, longest increasing chain."""
    def grams(seq, lo, hi):
        seen = {}
        for i in range(lo, hi - k + 1):
            g = tuple(seq[i:i + k])
            seen[g] = -1 if g in seen else i
        return seen
    ga, gb = grams(a, alo, ahi), grams(b, blo, bhi)
    pairs = sorted((i, gb[g]) for g, i in ga.items() if i >= 0 and gb.get(g, -1) >= 0)
    # longest chain increasing in both i and j (patience sorting)
    tails, prev, idx = [], [None] * len(pairs), []
    for n, (_, j) in enumerate(pairs):
        pos = bisect.bisect_left(tails, j)
        if pos == len(tails):
            tails.append(j)
            idx.append(n)
        else:
            tails[pos] = j
            idx[pos] = n
        prev[n] = idx[pos - 1] if pos else None
    chain, n = [], idx[-1] if idx else None
    while n is not None:
        chain.append(pairs[n])
        n = prev[n]
    return chain[::-1]


def matching_blocks(a, b, small=250_000):
    """Like difflib's get_matching_blocks, but anchored on unique word 3-grams so that long documents take
    near-linear time; difflib only runs on the short stretches between anchors."""
    blocks = []

    def rec(alo, ahi, blo, bhi, depth):
        n = 0
        while alo + n < ahi and blo + n < bhi and a[alo + n] == b[blo + n]:
            n += 1
        if n:
            blocks.append((alo, blo, n))
            alo, blo = alo + n, blo + n
        m = 0
        while ahi - m > alo and bhi - m > blo and a[ahi - m - 1] == b[bhi - m - 1]:
            m += 1
        tail = (ahi - m, bhi - m, m) if m else None
        ahi, bhi = ahi - m, bhi - m
        if alo < ahi and blo < bhi:
            chain = _anchors(a, b, alo, ahi, blo, bhi) if (ahi - alo) * (bhi - blo) > small and depth < 30 else []
            if chain:
                pa, pb = alo, blo
                for i, j in chain:
                    if i >= pa and j >= pb:
                        rec(pa, i, pb, j, depth + 1)
                        pa, pb = i, j
                rec(pa, ahi, pb, bhi, depth + 1)
            else:
                sm = difflib.SequenceMatcher(None, a[alo:ahi], b[blo:bhi],
                                             autojunk=(ahi - alo) * (bhi - blo) > 50 * small)
                blocks.extend((alo + i, blo + j, size) for i, j, size in sm.get_matching_blocks() if size)
        if tail:
            blocks.append(tail)

    rec(0, len(a), 0, len(b), 0)
    merged = []
    for i, j, n in sorted(blocks):
        if merged and merged[-1][0] + merged[-1][2] == i and merged[-1][1] + merged[-1][2] == j:
            merged[-1] = (merged[-1][0], merged[-1][1], merged[-1][2] + n)
        else:
            merged.append((i, j, n))
    return merged + [(len(a), len(b), 0)]


def opcodes(a, b):
    """difflib-style opcodes from matching_blocks."""
    ops, i, j = [], 0, 0
    for ai, bj, n in matching_blocks(a, b):
        tag = 'replace' if i < ai and j < bj else 'delete' if i < ai else 'insert' if j < bj else ''
        if tag:
            ops.append([tag, i, ai, j, bj])
        if n:
            ops.append(['equal', ai, ai + n, bj, bj + n])
        i, j = ai + n, bj + n
    return ops


def word_ops(a, b):
    """Opcodes with tiny unchanged islands (<= 2 words) between two changes folded into the change."""
    ops = opcodes(a, b)
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


def moved(old_t, new_t, rem, add):
    """Text that was removed in one place and added in another (a float or paragraph that moved): runs of at
    least MOVE_MIN words shared by the removed and the added tokens. Returns matched (old, new) index pairs."""
    r, d = sorted(rem), sorted(add)
    pairs = []
    for i, j, n in matching_blocks([old_t[x].text for x in r], [new_t[y].text for y in d]):
        if n >= MOVE_MIN:
            pairs += [(r[i + k], d[j + k]) for k in range(n)]
    return pairs


def runs(indices):
    """Consecutive index runs of a set."""
    out = []
    for i in sorted(indices):
        if out and out[-1][-1] == i - 1:
            out[-1].append(i)
        else:
            out.append([i])
    return out


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
    """Where text is missing on this side: a caret before token k, or after token k-1 when token k starts a
    different line (the gap then belongs to the end of the previous text, not to whatever comes next)."""
    if not toks:
        return None
    if k < len(toks) and (k == 0 or toks[k].line == toks[k - 1].line):
        pno, r, _ = toks[k].rects[0]
        return pno, r.x0 - 1.2, r.y0, r.y1
    pno, r, _ = toks[min(k, len(toks)) - 1].rects[-1]
    return pno, r.x1 + 1.2, r.y0, r.y1


# ---------------------------------------------------------------- pairing pages

def pair_pages(n_old, n_new, matches, words_old, words_new):
    """Spreads (old page or None, new page or None) in reading order of both documents.

    `matches` are (old page, new page) of every matched word; two pages are counterparts when they share
    MIN_PAIR_WORDS words, or a third of the words of the shorter page if that is less (short last pages).
    """
    weight = {}
    for key in matches:
        weight[key] = weight.get(key, 0) + 1
    need = {(p, q): max(4, min(MIN_PAIR_WORDS, min(words_old.get(p, 0), words_new.get(q, 0)) // 3))
            for p, q in weight}
    weight = {k: w for k, w in weight.items() if w >= need[k]}
    best_old = {}
    for (p, q), w in weight.items():
        if w > weight.get((best_old.get(q), q), 0):
            best_old[q] = p
    best_new = {}
    for (p, q), w in weight.items():
        if w > weight.get((p, best_new.get(p)), 0):
            best_new[p] = q
    spreads, shown, last = [], set(), -1
    for q in range(n_new):
        p = best_old.get(q)
        if p is not None and p < last:
            p = last if (last, q) in weight else None
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


# ---------------------------------------------------------------- figures

def graphics(page):
    """Rectangles of figures on a page: embedded images and dense clusters of vector drawings (plots, TikZ).
    Sparse drawings and grids of straight horizontal/vertical lines (table rules) are ignored."""
    regions = [pymupdf.Rect(i['bbox']) & page.rect for i in page.get_image_info()]
    draws = page.get_drawings()
    if draws:
        try:
            clusters = page.cluster_drawings(drawings=draws)
        except Exception:
            clusters = []
        for c in clusters:
            members = [d for d in draws if pymupdf.Rect(d['rect']) in c + (-1, -1, 1, 1)]
            if len(members) >= 6 and not all(ruled(d) for d in members):
                regions.append(pymupdf.Rect(c))
    regions = [r for r in regions if r.width > 20 and r.height > 12 and r.width * r.height > 900]
    return [r for r in regions if not any(o != r and r in o for o in regions)]


def ruled(drawing):
    """True for a drawing made only of horizontal or vertical straight lines and unfilled boxes."""
    for item in drawing['items']:
        if item[0] == 'l':
            (a, b) = item[1], item[2]
            if abs(a.x - b.x) > 0.5 and abs(a.y - b.y) > 0.5:
                return False
        elif item[0] != 're' or drawing.get('fill') is not None:
            return False
    return True


def fingerprint(page, rect, size=96):
    """Greyscale thumbnail of a page region, `size` x `size` pixels."""
    zoom = max(1.0, 2 * size / max(rect.width, rect.height))
    pix = page.get_pixmap(clip=rect, matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return pymupdf.Pixmap(pix, size, size, None).samples


def pixel_change(a, b):
    """Share of thumbnail pixels that differ visibly."""
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 48) / max(1, len(a))


def changed_figures(old_doc, new_doc):
    """Figures with no near-identical counterpart in the other document: ([(page, rect)] old, [...] new)."""
    def collect(doc):
        return [(pno, r, fingerprint(doc[pno], r), r.width / r.height) for pno in range(len(doc)) for r in graphics(doc[pno])]
    old, new = collect(old_doc), collect(new_doc)
    exact = {}
    for k, (_, _, fp, _) in enumerate(old):
        exact.setdefault(fp, []).append(k)
    same_old, pending = set(), []
    for q, rq, fq, aq in new:                  # most unchanged figures render to identical pixels
        if exact.get(fq):
            same_old.add(exact[fq].pop())
        else:
            pending.append((q, rq, fq, aq))
    changed_new = []
    for q, rq, fq, aq in pending:
        cands = [k for k, (p, rp, fp, ap) in enumerate(old) if k not in same_old and abs(ap - aq) <= 0.2 * aq]
        best = min(((pixel_change(fq, old[k][2]), k) for k in cands), default=(1.0, None))
        if best[0] <= FIG_DIFF:
            same_old.add(best[1])
        else:
            changed_new.append((q, rq))
    return [(p, r) for k, (p, r, _, _) in enumerate(old) if k not in same_old], changed_new


def frame(doc, figs, color, label):
    for pno, r in figs:
        page = doc[pno]
        box = r + (-3, -3, 3, 3)
        page.draw_rect(box, color=color, width=1.6, dashes='[5 3] 0')
        # a solid tag on the top-right corner stays legible over axis labels (usually top-left)
        w = pymupdf.get_text_length(label, fontname='hebo', fontsize=6.5) + 6
        page.draw_rect(pymupdf.Rect(box.x1 - w, box.y0 - 9, box.x1, box.y0), color=None, fill=color)
        page.insert_text((box.x1 - w + 3, box.y0 - 2.2), label, fontsize=6.5, fontname='hebo', color=(1, 1, 1))
        page.draw_rect(pymupdf.Rect(28, box.y0, 31.5, box.y1), color=None, fill=color)


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
    removed, added, old_markers, new_markers, per_sec, matches, n_moved = [], [], [], [], {}, [], 0

    def sec_at(tok):
        pno, r, _ = tok.rects[0]
        return section_of(sections, pno, r.y0)

    def bump(sec, rm=0, ad=0):
        r0, a0 = per_sec.get(sec, (0, 0))
        per_sec[sec] = (r0 + rm, a0 + ad)

    for old_t, new_t, ops in streams:
        rem = {i for tag, i1, i2, _, _ in ops if tag != 'equal' for i in range(i1, i2)}
        add = {j for tag, _, _, j1, j2 in ops if tag != 'equal' for j in range(j1, j2)}
        mv = moved(old_t, new_t, rem, add)      # moved text is not an edit
        n_moved += len(mv)
        mv_old, mv_new = {i for i, _ in mv}, {j for _, j in mv}
        rem -= mv_old
        add -= mv_new
        matches += [(old_t[i].page, new_t[j].page) for i, j in mv]
        for tag, i1, i2, j1, j2 in ops:
            if tag == 'equal':
                matches += [(old_t[i].page, new_t[j].page) for i, j in zip(range(i1, i2), range(j1, j2))]
                continue
            r_left = [i for i in range(i1, i2) if i in rem]
            a_left = [j for j in range(j1, j2) if j in add]
            # carets show where text went missing; not for edits inside moved text, which have no fixed spot
            if r_left and not a_left and not mv_old.intersection(range(i1, i2)) and (m := marker_at(new_t, j1)):
                new_markers.append(m)
            if a_left and not r_left and not mv_new.intersection(range(j1, j2)) and (m := marker_at(old_t, i1)):
                old_markers.append(m)
            # removed words count towards the section they were in: the one holding the word before the gap
            # (the word after it may already be the next section's heading)
            if r_left and new_t:
                bump(sec_at(new_t[j1 - 1] if j1 > 0 else new_t[0]), rm=len(r_left))
            for j in a_left:
                bump(sec_at(new_t[j]), ad=1)
        removed += [[old_t[i] for i in run] for run in runs(rem)]
        added += [[new_t[j] for j in run] for run in runs(add)]
    words_old, words_new = {}, {}
    for toks, counts in ((old_all, words_old), (new_all, words_new)):
        for t in toks:
            counts[t.page] = counts.get(t.page, 0) + 1
    spreads = pair_pages(len(old_doc), len(new_doc), matches, words_old, words_new)
    n_removed, n_added = sum(map(len, removed)), sum(map(len, added))
    figs_old, figs_new = changed_figures(old_doc, new_doc)

    mark(old_doc, removed, RED_FILL, RED, old_markers)
    mark(new_doc, added, GREEN_FILL, GREEN, new_markers)
    frame(old_doc, figs_old, RED, 'FIGURE CHANGED')
    frame(new_doc, figs_new, GREEN, 'FIGURE CHANGED')

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
    figs = f'{len(figs_new)} figures changed or added, {len(figs_old)} changed or removed (dashed frames). ' \
        if figs_new or figs_old else ''
    moves = f'{n_moved} words moved without changes (not marked). ' if n_moved else ''
    text(c, 40, 128 + 14, figs + moves + 'Changes are detected on the words and figures of the compiled PDFs.'
         + (f'  Title pages and front matter: -{pre[0]} +{pre[1]} words.' if any(pre) else ''), 9, GREY)
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
    print(f'{out_path}: {len(spreads)} spreads, -{n_removed} +{n_added} words, '
          f'{len(figs_old) + len(figs_new)} changed figures')
