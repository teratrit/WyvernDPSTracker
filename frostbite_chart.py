"""Frostbite duration vs water skill - including sigmoid (diminishing-returns) fit."""
import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from pathlib import Path

skills = np.array([51, 61, 65, 72], dtype=float)
durs   = np.array([3.7, 4.7, 6.8, 8.7])

# Quadratic least-squares
a, b, c = np.polyfit(skills, durs, 2)

# Sigmoid: y = L / (1 + exp(-k*(x - x0))) + b0
def sigmoid(x, L, k, x0, b0):
    return L / (1.0 + np.exp(-k * (x - x0))) + b0

p0 = [10, 0.3, 65, 3.0]   # plausible starting guess
popt, _ = curve_fit(sigmoid, skills, durs, p0=p0, maxfev=20000)
L, k, x0, b0 = popt

# Diagnostic prints
print(f"quadratic:  y = {a:.5f}x^2 + {b:.4f}x + {c:.3f}")
print(f"sigmoid:    L={L:.2f} k={k:.3f} x0={x0:.2f} b0={b0:.3f}")
print(f"sigmoid plateau (x->inf): {L + b0:.2f}s")

x_close = np.linspace(45, 80, 400)
x_far   = np.linspace(45, 110, 600)

fig, (ax, ax2) = plt.subplots(1, 2, figsize=(12, 5.2),
                              gridspec_kw={'width_ratios': [1.1, 1]})

# --- Panel 1: close-up around the data ---
ax.plot(x_close, a*x_close**2 + b*x_close + c,
        color='steelblue', lw=1.5, label='quadratic')
ax.plot(x_close, sigmoid(x_close, *popt),
        color='crimson', lw=1.5, label='sigmoid')
ax.scatter(skills, durs, s=55, color='black', zorder=5)
for s, d in zip(skills, durs):
    ax.annotate(f' ({s}, {d}s)', (s, d), fontsize=9, va='center')
ax.set_xlabel('water skill')
ax.set_ylabel('frostbite duration (seconds)')
ax.set_title('observed range', fontsize=11)
ax.grid(True, alpha=0.25, lw=0.5)
ax.spines['top'].set_visible(False); ax.spines['right'].set_visible(False)
ax.legend(loc='upper left', frameon=False, fontsize=9)
ax.set_xlim(45, 80); ax.set_ylim(2, 11)

# --- Panel 2: extrapolation to skill 110 ---
ax2.plot(x_far, a*x_far**2 + b*x_far + c,
         color='steelblue', lw=1.5, label='quadratic')
ax2.plot(x_far, sigmoid(x_far, *popt),
         color='crimson', lw=1.5, label='sigmoid')
ax2.axhline(L + b0, color='crimson', lw=0.7, ls=':',
            label=f'sigmoid plateau ~ {L+b0:.1f}s')
ax2.scatter(skills, durs, s=40, color='black', zorder=5)
ax2.set_xlabel('water skill')
ax2.set_title('extrapolated to skill 110', fontsize=11)
ax2.grid(True, alpha=0.25, lw=0.5)
ax2.spines['top'].set_visible(False); ax2.spines['right'].set_visible(False)
ax2.legend(loc='upper left', frameon=False, fontsize=9)
ax2.set_xlim(45, 110); ax2.set_ylim(0, 30)

fig.suptitle('Frostbite duration - quadratic vs sigmoid', fontsize=12, y=1.01)
plt.tight_layout()

out = Path(__file__).parent / 'frostbite_chart_v3.png'
plt.savefig(out, dpi=130, bbox_inches='tight')
print(f"Saved: {out}")
