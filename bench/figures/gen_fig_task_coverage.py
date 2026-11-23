"""Regenerate the README coverage figure from the public task manifest."""

import csv
from collections import Counter, defaultdict
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
STABLE, HELD_OUT, EXPLORATORY = "#006F72", "#90ADAD", "#AB5C1D"


def draw(manifest, output):
    cells = defaultdict(Counter)
    totals = Counter()
    with manifest.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            cells[row["cell"]][row["set"]] += 1
            totals[row["set"]] += 1

    width, height = 876, 501
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
    label(width - left, 32, f"{sum(totals.values())} task definitions", 15,
          MUTED, ha="right")
    box(left, 54, width - 2 * left, 1.1, INK)
    label(left, 104, "Material tier", 15, weight="bold")
    for j, name in enumerate(WORKFLOWS):
        x = first_col + j * col_step + 10
        label(x, 73, f"T{j + 1}", 12, STABLE, "bold")
        label(x, 103, name, 14, weight="bold", linespacing=1.12)

    max_count = max(sum(group.values()) for group in cells.values())
    bar_unit = (col_step - 34) / max_count
    for i, name in enumerate(TIERS):
        y = 129 + i * row_step
        label(left, y + 17, f"M{i + 1}", 17, weight="bold")
        label(left, y + 46, name, 12.5, MUTED, linespacing=1.1)
        for j in range(len(WORKFLOWS)):
            x = first_col + j * col_step
            counts = cells[f"M{i + 1}_T{j + 1}"]
            total = sum(counts.values())
            stable, exploratory = counts["stable"], counts["exploratory"]
            if exploratory and exploratory != total:
                raise ValueError("Mixed exploratory and stable/held-out cells need separate count labels")
            fill = "#E8F2F1" if total else "#F3F5F6"
            if exploratory:
                fill = "#FAEEE2"
            box(x, y, col_step - 5, row_step - 5, fill)
            if not total:
                label(x + 11, y + 30, "—", 19, "#667681")
                continue
            value = exploratory or stable
            color = EXPLORATORY if exploratory else STABLE
            label(x + 11, y + 26, str(value), 24, color, "bold")
            detail = "exploratory" if exploratory else f"of {total}"
            label(x + 15 + 17 * len(str(value)), y + 28, detail, 12, MUTED)
            bar_x, bar_y = x + 11, y + 48
            box(bar_x, bar_y, value * bar_unit, 5, color)
            if counts["held_out"]:
                box(bar_x + value * bar_unit, bar_y, counts["held_out"] * bar_unit, 5, HELD_OUT)

    box(left, 425, width - 2 * left, 0.7, "#C7D3D5")
    for x, key, name, color in [
        (left, "stable", "stable", STABLE), (182, "held_out", "held out", HELD_OUT),
        (348, "exploratory", "exploratory", EXPLORATORY),
    ]:
        box(x, 443, 12, 8, color)
        label(x + 19, 447, f"{totals[key]} {name}", 13, weight="bold")
    label(560, 447, "—  No task files", 13, MUTED)
    label(left, 478, "M1 and M2 show stable tasks out of all files in each cell. Bar length counts tasks on a common scale.",
          12, MUTED)
    fig.savefig(output, dpi=180, metadata={"Software": "Matplotlib"})
    plt.close(fig)


if __name__ == "__main__":
    draw(HERE.parent / "task_manifest.tsv", HERE / "task_coverage.png")
