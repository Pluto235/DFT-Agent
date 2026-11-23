"""Regenerate the README coverage figure from the public task manifest."""

import csv
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

HERE = Path(__file__).resolve().parent
TIERS = [
    "Simple elemental and\nbinary crystals",
    "Larger or lower-\nsymmetry cells",
    "Magnetic transition-\nmetal systems",
    "Strongly correlated\nsystems",
]
WORKFLOWS = [
    "Variable-cell\nrelaxation",
    "Bands + DOS",
    "Equation of state",
    "Elastic tensor",
]
INK, MUTED = "#192F3C", "#465965"
ACCENT = "#006F72"


def draw(manifest, output):
    with manifest.open(encoding="utf-8", newline="") as source:
        cells = Counter(row["cell"] for row in csv.DictReader(source, delimiter="\t"))

    width, height = 876, 458
    fig = plt.figure(figsize=(width / 72, height / 72), facecolor="#FFFFFF")
    ax = fig.add_axes([0, 0, 1, 1], xlim=(0, width), ylim=(height, 0))
    ax.axis("off")

    def label(x, y, value, size, color=INK, weight="normal", **kwargs):
        return ax.text(x, y, value, fontsize=size, fontfamily="DejaVu Sans",
                       color=color, weight=weight, va="center", **kwargs)

    def box(x, y, w, h, color):
        ax.add_patch(Rectangle((x, y), w, h, facecolor=color, edgecolor="none"))

    left, first_col, col_step, row_step = 18, 210, 163, 71
    label(left, 31, "Benchmark task coverage", 25, weight="bold")
    label(width - left, 32, f"{sum(cells.values())} public task definitions", 15,
          MUTED, ha="right")
    box(left, 54, width - 2 * left, 1.1, INK)
    label(left, 104, "Material tier", 15, weight="bold")
    for j, name in enumerate(WORKFLOWS):
        x = first_col + j * col_step + 10
        label(x, 73, f"T{j + 1}", 12, ACCENT, "bold")
        label(x, 103, name, 14, weight="bold", linespacing=1.12)

    bar_unit = (col_step - 34) / max(cells.values(), default=1)
    for i, name in enumerate(TIERS):
        y = 129 + i * row_step
        label(left, y + 17, f"M{i + 1}", 17, weight="bold")
        label(left, y + 46, name, 12.5, MUTED, linespacing=1.1)
        for j in range(len(WORKFLOWS)):
            x = first_col + j * col_step
            count = cells[f"M{i + 1}_T{j + 1}"]
            box(x, y, col_step - 5, row_step - 5, "#E8F2F1" if count else "#F3F5F6")
            label(x + 11, y + 26, str(count), 24, ACCENT if count else "#667681", "bold")
            box(x + 11, y + 48, count * bar_unit, 5, ACCENT)

    box(left, 425, width - 2 * left, 0.7, "#C7D3D5")
    label(left, 441, "Counts include stable, exploratory and held-out tasks.", 12, MUTED)
    fig.savefig(output, dpi=180, metadata={"Software": "Matplotlib"})
    plt.close(fig)


if __name__ == "__main__":
    draw(HERE.parent / "task_manifest.tsv", HERE / "task_coverage.png")
