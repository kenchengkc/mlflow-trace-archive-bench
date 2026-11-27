"""Plot per-pass median timings from an archival drain report. Requires Matplotlib."""
import argparse
import gzip
import json
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.ticker import MaxNLocator


HERE = Path(__file__).resolve().parent


def plot_drain(report, output):
    rounds = [trial["passes"] for trial in report["rounds"]]
    if not rounds or not rounds[0]:
        raise ValueError("The report must contain at least one pass")
    counts = [row["archived_count"] for row in rounds[0]]
    numbers = list(range(1, len(counts) + 1))
    if counts[-1] != 0 or 0 in counts[:-1]:
        raise ValueError("Only the final pass should be empty")
    for rows in rounds:
        if ([row["pass_number"] for row in rows] != numbers
                or [row["archived_count"] for row in rows] != counts):
            raise ValueError("Pass numbers and archived counts must match across rounds")

    fig, ax = plt.subplots(figsize=(8.4, 4.5), facecolor="white")
    fig.subplots_adjust(left=.12, right=.95, top=.76, bottom=.22)
    fig.suptitle("Time per archival pass", x=.045, y=.96, ha="left",
                 fontsize=19, weight="bold", color="#172f40")
    ax.spines[:].set_visible(False)
    ax.grid(axis="y", color="#dce4e8", linewidth=.8)
    ax.set_axisbelow(True)
    for key, label, color, marker in (
        ("total_ms", "Complete pass", "#305e82", "o"),
        ("selection_ms", "Candidate selection", "#784d88", "D"),
    ):
        times = [median(row[key] for row in group) for group in zip(*rounds, strict=True)]
        ax.plot(numbers, times, label=label, color=color, linewidth=1.7,
                marker=marker, markersize=4.3, markerfacecolor="white", markeredgewidth=1.2)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1), ncol=2, frameon=False, fontsize=10.5,
              borderaxespad=0)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
    last = numbers[-1]
    ticks = MaxNLocator(nbins=6, integer=True).tick_values(1, last)
    gap = max(1, last / 12)
    inner = [int(tick) for tick in ticks if 1 + gap <= tick <= last - gap]
    positions = sorted({1, *inner, last})
    labels = [str(tick) if tick != last else f"{last}\n(empty)" for tick in positions]
    ax.set_xticks(positions, labels)
    ax.tick_params(length=0, colors="#485d6a", labelsize=10, pad=6)
    ax.set_xlabel("Archival pass", color="#485d6a", fontsize=11)
    ax.set_ylabel("Median time (ms)", color="#485d6a", fontsize=11)
    fig.savefig(output, dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=HERE.parent / "drain-study.json.gz")
    parser.add_argument("--output", type=Path, default=HERE / "drain-pass-times.png")
    args = parser.parse_args()
    opener = gzip.open if args.input.suffix == ".gz" else open
    with opener(args.input, "rt", encoding="utf-8") as source:
        plot_drain(json.load(source), args.output)
