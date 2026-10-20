"""Small numeric helpers shared by the analyses."""


def bps(a, b):
    """Relative difference of a vs b in basis points."""
    return (a / b - 1) * 1e4


def pct(xs, p):
    """Linear-interpolated percentile, None on empty input."""
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"
