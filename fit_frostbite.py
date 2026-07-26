"""Try several candidate equations against the four observed points."""
import numpy as np

skills = np.array([51, 61, 65, 72])
durs   = np.array([3.7, 4.7, 6.8, 8.7])

def report(name, predict_fn):
    pred = np.array([predict_fn(s) for s in skills])
    resid = pred - durs
    print(f"{name}")
    print(f"  predictions: {[f'{p:.2f}' for p in pred]}")
    print(f"  residuals:   {[f'{r:+.2f}' for r in resid]}")
    print(f"  RMS error:   {np.sqrt((resid**2).mean()):.3f}s")
    print()

# 1. Pure quadratic least-squares
coeffs = np.polyfit(skills, durs, 2)
a, b, c = coeffs
print(f"Least-squares quadratic: y = {a:.5f}*x^2 + {b:.4f}*x + {c:.2f}")
report("quadratic", lambda s: a*s*s + b*s + c)

# 2. Pure cubic (exact through 4 points)
coeffs3 = np.polyfit(skills, durs, 3)
print(f"Cubic (exact 4-point): {coeffs3}")
report("cubic", lambda s: np.polyval(coeffs3, s))

# 3. Piecewise: base for skill <= 61, basex1.5 for skill >= 65
def piecewise(s):
    base = (s - 14) / 10
    if s <= 61:
        return base
    if s >= 65:
        return base * 1.5
    # interpolate linearly between (61, 4.7) and (65, 6.8)
    t = (s - 61) / 4
    return 4.7 + t * (6.8 - 4.7)
report("piecewise (base / linear / basex1.5)", piecewise)

# 4. Power-law (least-squares in log space)
log_s = np.log(skills)
log_d = np.log(durs)
b_pow, log_a = np.polyfit(log_s, log_d, 1)
a_pow = np.exp(log_a)
print(f"Power-law: y = {a_pow:.4e} * x^{b_pow:.3f}")
report("power-law", lambda s: a_pow * s**b_pow)
