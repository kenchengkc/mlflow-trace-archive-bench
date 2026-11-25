"""Regenerate scope-and-depth.png from matrix.json.gz. Requires Matplotlib."""

import gzip
import json
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, LogNorm
from matplotlib.ticker import NullLocator


HERE = Path(__file__).resolve().parent
DEPTHS = [0, 50, 95, 99, 100]
SCOPES = {
    (1,): "Experiment 1\n(frequent)",
    (8,): "Experiment 8\n(rare)",
    tuple(range(1, 9)): "All eight\nexperiments",
}


def median_time(measurement):
    pairs = zip(measurement["planning_samples_ms"], measurement["samples_ms"], strict=True)
    return median(planning + execution for planning, execution in pairs)


def main():
    with gzip.open(HERE / "matrix.json.gz", "rt", encoding="utf-8") as source:
        report = json.load(source)
    ratios = {}
    for case in report["cases"]:
        if case["rows"] == 100_000 and case["batch_size"] == 100:
            strategies = case["strategies"]
            key = tuple(case["experiment_ids"]), case["archived_pct"]
            ratios[key] = median_time(strategies["bounded_probe"]) / median_time(strategies["global"])
    values = [[ratios[scope, pct] for pct in DEPTHS] for scope in SCOPES]

    ink = "#192c35"
    muted = "#475c64"
    palette = LinearSegmentedColormap.from_list(
        "time_ratio", ["#09506a", "#92bdca", "#f5f4ef", "#edba88", "#91400e"]
    )
    # Reciprocal endpoints place equal speed exactly at the midpoint of the log scale.
    extent = max(5, max(ratios.values()), 1 / min(ratios.values()))
    scale = LogNorm(vmin=1 / extent, vmax=extent)

    fig = plt.figure(figsize=(9.4, 5.2), facecolor="white")
    fig.text(.035, .925, "Bounded probe time relative to the global query",
             fontsize=21, weight="bold", color=ink)
    version = report["postgres_settings"]["server_version"].split()[0]
    fig.text(.035, .855, f"100,000 traces   ·   Batch limit 100   ·   PostgreSQL {version}"
             f"   ·   {report['samples_per_strategy']} samples per strategy", fontsize=12, color=muted)

    ax = fig.add_axes([.215, .352, .750, .315])
    mesh = ax.pcolormesh(values, cmap=palette, norm=scale, edgecolors="white", linewidth=2)
    ax.invert_yaxis()
    ax.xaxis.tick_top()
    ax.set_xticks([i + .5 for i in range(len(DEPTHS))], labels=[f"{pct}%" for pct in DEPTHS])
    ax.set_yticks([i + .5 for i in range(len(SCOPES))], labels=SCOPES.values())
    ax.tick_params(length=0, labelcolor=ink, labelsize=12, pad=8)
    ax.spines[:].set_visible(False)
    fig.text(.215, .748, "Share of initially eligible traces already archived", fontsize=12, color=ink)
    for row, series in enumerate(values):
        for col, value in enumerate(series):
            r, g, b, _ = palette(scale(value))
            light = .2126 * r**2.2 + .7152 * g**2.2 + .0722 * b**2.2
            ax.text(col + .5, row + .5, f"{value:.2f}×", va="center", ha="center",
                    fontsize=15, weight="bold", color="white" if light < .24 else ink)

    bar_ax = fig.add_axes([.215, .211, .750, .026])
    bar = fig.colorbar(mesh, cax=bar_ax, orientation="horizontal")
    bar.set_ticks([.25, .5, 1, 2, 4], labels=["0.25×", "0.5×", "1×", "2×", "4×"])
    bar.ax.xaxis.set_minor_locator(NullLocator())
    bar.ax.tick_params(length=3, colors=muted, labelsize=10)
    bar.outline.set_visible(False)
    for x, label, align in [(.215, "Bounded faster", "left"), (.59, "Equal time", "center"),
                            (.965, "Bounded slower", "right")]:
        fig.text(x, .26, label, ha=align, fontsize=11, color=ink)

    fig.text(.035, .052, "Each cell divides the bounded-probe median by the global-query median. "
             "Each sample includes server planning and execution.", fontsize=9.5, color=muted)
    fig.savefig(HERE / "scope-and-depth.png", dpi=170)
    plt.close(fig)


if __name__ == "__main__":
    main()
