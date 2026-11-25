"""Tiny dependency-free SVG chart helpers for the report (theme-aware via CSS vars)."""
from __future__ import annotations

import math
from html import escape


def _nice_ticks(lo, hi, n=5):
    span = hi - lo or 1
    step = 10 ** math.floor(math.log10(span / n))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= n:
            step *= m
            break
    t = math.floor(lo / step) * step
    out = []
    while t <= hi + 1e-12:
        out.append(round(t, 10))
        t += step
    return out


def line_chart(series, *, xs_log=True, x_labels=None, y_fmt=lambda v: f"{v:.0%}", y_max=None,
               width=720, height=320, title="", x_title="", y_title=""):
    """series: list of (name, color_var, [(x, y), ...]). Direct labels at line ends."""
    L, R, T, B = 56, 150, 16, 44
    W, H = width - L - R, height - T - B
    allx = [x for _, _, pts in series for x, _ in pts]
    ally = [y for _, _, pts in series for _, y in pts]
    x0, x1 = min(allx), max(allx)
    y1 = y_max if y_max is not None else max(ally) * 1.08
    fx = (lambda x: L + W * (math.log10(x) - math.log10(x0)) / (math.log10(x1) - math.log10(x0))) if xs_log \
        else (lambda x: L + W * (x - x0) / (x1 - x0))
    fy = lambda y: T + H * (1 - min(y, y1) / y1)
    g = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" class="chart">']
    for t in _nice_ticks(0, y1):
        if t > y1:
            continue
        y = fy(t)
        g.append(f'<line x1="{L}" x2="{L + W}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        g.append(f'<text x="{L - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{y_fmt(t)}</text>')
    for x in sorted(set(allx)):
        lab = x_labels(x) if x_labels else str(x)
        g.append(f'<text x="{fx(x):.1f}" y="{T + H + 18}" class="tick" text-anchor="middle">{lab}</text>')
    if x_title:
        g.append(f'<text x="{L + W / 2}" y="{height - 6}" class="axis-title" text-anchor="middle">{escape(x_title)}</text>')
    if y_title:
        g.append(f'<text x="12" y="{T + H / 2}" class="axis-title" text-anchor="middle" transform="rotate(-90 12 {T + H / 2})">{escape(y_title)}</text>')
    label_ys = []
    for name, color, pts in series:
        d = " ".join(f"{'M' if i == 0 else 'L'}{fx(x):.1f},{fy(y):.1f}" for i, (x, y) in enumerate(pts))
        g.append(f'<path d="{d}" fill="none" stroke="var({color})" stroke-width="2" stroke-linejoin="round"/>')
        for x, y in pts:
            g.append(f'<circle cx="{fx(x):.1f}" cy="{fy(y):.1f}" r="4" fill="var({color})" stroke="var(--surface)" stroke-width="2">'
                     f'<title>{escape(name)} · {x_labels(x) if x_labels else x}: {y_fmt(y)}</title></circle>')
        ly = fy(pts[-1][1])
        while any(abs(ly - o) < 14 for o in label_ys):
            ly += 14
        label_ys.append(ly)
        g.append(f'<text x="{fx(pts[-1][0]) + 10:.1f}" y="{ly + 4:.1f}" class="dlabel">{escape(name)}</text>')
    g.append(f'<line x1="{L}" x2="{L + W}" y1="{T + H}" y2="{T + H}" class="axis"/>')
    g.append("</svg>")
    return "".join(g)


def bar_chart(cats, groups, *, y_fmt=lambda v: f"${v / 1000:.0f}k", width=720, height=300, title="",
              value_labels=True):
    """cats: category labels; groups: list of (name, color_var, [values per cat])."""
    L, R, T, B = 56, 16, 16, 48
    W, H = width - L - R, height - T - B
    vmax = max(v for _, _, vs in groups for v in vs) * 1.12
    fy = lambda v: T + H * (1 - v / vmax)
    band = W / len(cats)
    gap = 2
    bw = min(34, (band * 0.7 - gap * (len(groups) - 1)) / len(groups))
    g = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" class="chart">']
    for t in _nice_ticks(0, vmax):
        if t > vmax:
            continue
        g.append(f'<line x1="{L}" x2="{L + W}" y1="{fy(t):.1f}" y2="{fy(t):.1f}" class="grid"/>')
        g.append(f'<text x="{L - 8}" y="{fy(t) + 4:.1f}" class="tick" text-anchor="end">{y_fmt(t)}</text>')
    for i, c in enumerate(cats):
        cx = L + band * (i + 0.5)
        total = len(groups) * bw + (len(groups) - 1) * gap
        for j, (name, color, vs) in enumerate(groups):
            v = vs[i]
            x = cx - total / 2 + j * (bw + gap)
            y = fy(max(v, 0))
            h = T + H - y
            r = min(4, h / 2, bw / 2)
            path = (f"M{x:.1f},{T + H:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                    f"H{x + bw - r:.1f} Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} V{T + H:.1f} Z")
            g.append(f'<path d="{path}" fill="var({color})"><title>{escape(c)} · {escape(name)}: {y_fmt(v)}</title></path>')
            if value_labels:
                g.append(f'<text x="{x + bw / 2:.1f}" y="{y - 5:.1f}" class="vlabel" text-anchor="middle">{y_fmt(v)}</text>')
        g.append(f'<text x="{cx:.1f}" y="{T + H + 18}" class="tick" text-anchor="middle">{escape(c)}</text>')
    g.append(f'<line x1="{L}" x2="{L + W}" y1="{T + H}" y2="{T + H}" class="axis"/>')
    g.append("</svg>")
    return "".join(g)


def legend(items):
    return '<div class="legend">' + "".join(
        f'<span><i style="background:var({c})"></i>{escape(n)}</span>' for n, c in items) + "</div>"
