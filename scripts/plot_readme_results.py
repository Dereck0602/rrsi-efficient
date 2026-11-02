"""Redraw the two README matrices from the published HTML table, without model calls.

From the repository root:
    uv run --no-project --with matplotlib==3.8.0 python scripts/plot_readme_results.py

README.md is the data source. The Chinese table must contain the same scores and
deltas. This checks published numbers, not the underlying experiment evidence.
"""

import re
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.patches import Rectangle

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ("Claude Code", "Mini-Agent")
LABELS = (
    "SearchQA",
    "HotpotQA",
    "Terminal-Bench\n2.0",
    "AppWorld",
    "NQ",
    "TriviaQA",
    "MuSiQue",
    "SWE-bench",
    "BFCL\nMulti-Turn",
    "OfficeQA\nPro",
    "DeepSearchQA",
)
SOURCES = (
    "",
    "",
    "",
    "",
    "SearchQA",
    "SearchQA",
    "HotpotQA",
    "Terminal 2.0",
    "AppWorld",
    "SearchQA",
    "HotpotQA",
)


class Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag == "td":
            self.cell = ""
        elif tag == "br" and self.cell is not None:
            self.cell += " "

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == "td":
            self.row.append(self.cell.strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)


def read_results(path):
    text = path.read_text(encoding="utf-8")
    table = Table()
    table.feed("<table>" + text.split("<table>", 1)[1].split("</table>", 1)[0])
    results = {name: [] for name in HARNESSES}
    for row in table.rows:
        if len(row) == 13:
            method, harness, *cells = row
        else:
            if len(row) != 12:
                raise ValueError(f"Unexpected table row: {row}")
            harness, *cells = row
        if not results[harness]:
            method = "Initial harness"
        scores, deltas = [], []
        for cell in cells:
            numbers = re.findall(r"[+-]?\d+\.\d+", cell)
            baseline = not results[harness]
            if len(numbers) != (1 if baseline else 2):
                raise ValueError(f"Unexpected score cell: {cell}")
            score = Decimal(numbers[0])
            if not 0 <= score <= 100:
                raise ValueError(f"Score outside [0, 100]: {score}")
            delta = Decimal(0) if baseline else Decimal(numbers[1])
            if not baseline and delta != score - results[harness][0][1][len(scores)]:
                raise ValueError(f"Incorrect delta: {method}, {harness}, {cell}")
            scores.append(score)
            deltas.append(delta)
        results[harness].append((method, scores, deltas))
    for rows in results.values():
        names = [row[0] for row in rows]
        if len(rows) != 13 or len(set(names)) != 13 or "RRSI" not in names:
            raise ValueError(f"Expected baseline and twelve study methods: {names}")
    return results


def draw(harness, rows, output):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
    fig, ax = plt.subplots(figsize=(23, 12))
    fig.subplots_adjust(left=0.015, right=0.985, top=0.99, bottom=0.02)
    ax.set(xlim=(-1.95, 12), ylim=(14.05, -3.6))
    ax.axis("off")
    ink, muted = "#152b3c", "#52677a"
    cmap = LinearSegmentedColormap.from_list(
        "change", ["#e9a18b", "#f3f5f7", "#70b9ac"]
    )
    norm = TwoSlopeNorm(vmin=-30, vcenter=0, vmax=30)
    ax.text(-1.9, -3.1, harness, fontsize=26, weight="bold", color=ink)
    ax.text(
        -1.9,
        -2.6,
        "12 RSI methods · 11 benchmarks · published experiment results",
        fontsize=13,
        color=muted,
    )
    ax.text(
        -1.9,
        -2.15,
        "Final score (%) / change from initial (pp) · Bold = best per benchmark, including ties",
        fontsize=11,
        color=muted,
    )
    for left, right, title in [
        (0, 4, "SOURCE · IN-DOMAIN"),
        (4, 9, "NEAR-DOMAIN · FROZEN TRANSFER"),
        (9, 11, "FAR-OOD · FROZEN TRANSFER"),
    ]:
        ax.add_patch(
            Rectangle(
                (left, -1.65), right - left - 0.04, 0.4, facecolor="#e7edf2", lw=0
            )
        )
        ax.text(
            (left + right) / 2,
            -1.45,
            title,
            ha="center",
            va="center",
            fontsize=10,
            weight="bold",
            color=ink,
        )
    for col, label in enumerate(LABELS):
        ax.text(
            col + 0.48,
            -0.83,
            label,
            ha="center",
            va="center",
            fontsize=10.5,
            weight="bold",
            color=ink,
        )
        if SOURCES[col]:
            ax.text(
                col + 0.48,
                -0.25,
                f"from {SOURCES[col]}",
                ha="center",
                fontsize=8,
                color=muted,
            )
    ax.text(
        11.48,
        -1.45,
        "MACRO MEAN",
        ha="center",
        va="center",
        fontsize=10,
        weight="bold",
        color=ink,
    )
    ax.text(
        11.48,
        -0.75,
        "Score / change\nacross 11 tasks",
        ha="center",
        va="center",
        fontsize=10,
        color=muted,
    )
    best = [max(row[1][i] for row in rows) for i in range(11)]
    for y, (method, scores, deltas) in enumerate(rows):
        ax.text(
            -0.15,
            y + 0.48,
            method,
            ha="right",
            va="center",
            fontsize=12,
            weight="bold",
            color=ink,
        )
        values = scores + [sum(scores) / 11]
        changes = deltas + [sum(deltas) / 11]
        for x, (score, delta) in enumerate(zip(values, changes)):
            face = "#e7edf2" if y == 0 else cmap(norm(float(delta)))
            ax.add_patch(
                Rectangle((x + 0.02, y + 0.03), 0.94, 0.9, facecolor=face, lw=0)
            )
            weight = "bold" if x < 11 and score == best[x] else "normal"
            ax.text(
                x + 0.49,
                y + (0.48 if y == 0 else 0.34),
                f"{score:.2f}",
                ha="center",
                va="center",
                fontsize=12,
                color=ink,
                weight=weight,
            )
            if y:
                ax.text(
                    x + 0.49,
                    y + 0.7,
                    f"{delta:+.2f}",
                    ha="center",
                    va="center",
                    fontsize=9.5,
                    color=muted,
                )
    for x in [4, 9, 11]:
        ax.plot([x - 0.01, x - 0.01], [-1.2, 12.96], color="#bdcbd5", lw=0.8)
    ax.text(
        -1.9,
        13.6,
        "Cell color = change (pp); the same scale is used for both harnesses.",
        fontsize=10,
        color=muted,
    )
    for x, delta, label in [
        (5.4, -30, "−30"),
        (6.5, -15, "−15"),
        (7.6, 0, "0"),
        (8.7, 15, "+15"),
        (9.8, 30, "+30 pp"),
    ]:
        ax.add_patch(
            Rectangle((x, 13.32), 0.35, 0.35, facecolor=cmap(norm(delta)), lw=0)
        )
        ax.text(x + 0.43, 13.5, label, va="center", fontsize=10, color=muted)
    fig.savefig(
        output, dpi=160, facecolor="white", pil_kwargs={"quality": 95, "subsampling": 0}
    )
    plt.close(fig)


def main():
    results = read_results(ROOT / "README.md")
    if results != read_results(ROOT / "README.zh-CN.md"):
        raise ValueError("English and Chinese experiment tables differ")
    for index, harness in enumerate(HARNESSES, 1):
        output = ROOT / f"assets/readme/overall-result-{index}.jpg"
        draw(harness, results[harness], output)
        print(
            f"Rendered {output.name}: 12 methods, 11 benchmark columns; deltas verified."
        )


if __name__ == "__main__":
    main()
