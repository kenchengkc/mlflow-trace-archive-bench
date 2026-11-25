"""Rebuild the two README charts from the published study reports. Requires Matplotlib."""
import gzip
import json
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import NullLocator


HERE = Path(__file__).resolve().parent
INK = "#172f40"
MUTED = "#4f6370"
GRID = "#dee5e8"
FRESH = "#365d80"
LATE = "#784d88"
FASTER = "#086f65"
SLOWER = "#a0441f"
DEPTHS = (0, 50, 95, 99, 100)
ALL = tuple(range(1, 9))
STRATEGIES = {"global": "Global (MLflow)", "per_experiment": "Per experiment", "bounded_probe": "Bounded probe"}


def load_report(name):
    with gzip.open(HERE / name, "rt", encoding="utf-8") as source:
        return json.load(source)


def median_ms(measurement):
    samples = zip(measurement["planning_samples_ms"], measurement["samples_ms"], strict=True)
    return median(planning + execution for planning, execution in samples)


def figure(height, title, caption):
    fig = plt.figure(figsize=(9, height), facecolor="white")
    fig.text(.035, 1 - .22 / height, title, va="top", fontsize=20, weight="bold", color=INK)
    fig.text(.037, 1 - .62 / height, caption, va="top", fontsize=11.5, color=MUTED)
    return fig


def log_axis(ax, ticks, labels):
    ax.set_xscale("log")
    ax.set_xticks(ticks, labels)
    ax.xaxis.set_minor_locator(NullLocator())
    ax.spines[:].set_visible(False)
    ax.tick_params(length=0, colors=MUTED, labelsize=10, pad=7)
    ax.set_axisbelow(True)
    ax.grid(axis="x", color=GRID, linewidth=.8)


def latency_chart(start, late):
    fig = figure(3.95, "Time to select the next 100 traces",
                 "1 million synthetic traces  ·  All eight experiments  ·  PostgreSQL "
                 + start["postgres_settings"]["server_version"].split()[0])
    ax = fig.add_axes([.216, .24, .735, .435])
    log_axis(ax, [1, 3, 10, 30, 100, 300, 1000, 3000],
             ["1", "3", "10", "30", "100", "300", "1,000", "3,000"])
    ax.set(xlim=(.72, 3450), ylim=(-.36, 2.65))
    ax.set_yticks([2, 1, 0], STRATEGIES.values(), fontsize=12, color=INK)
    ax.tick_params(axis="y", pad=15)
    ax.set_xlabel("Median planning + execution time (ms, log scale)", fontsize=11, color=MUTED, labelpad=10)

    first, last = start["cases"][0], late["cases"][0]
    for y, key in zip([2, 1, 0], STRATEGIES):
        a, b = (median_ms(case["strategies"][key]) for case in (first, last))
        ax.plot([a, b], [y, y], color="#b7c6cf", lw=2.3, zorder=2)
        for x, marker, color, face in [(a, "o", FRESH, "white"), (b, "D", LATE, LATE)]:
            ax.plot(x, y, marker=marker, mec=color, mfc=face, ms=7, mew=1.8, zorder=3)
            ax.annotate(f"{x:,.2f}", (x, y), xytext=(0, 8), textcoords="offset points",
                        ha="center", va="bottom", color=color, fontsize=10.5, weight="bold")

    handles = [Line2D([], [], linestyle="none", marker=marker, markeredgewidth=1.8, markersize=7,
                      markeredgecolor=color, markerfacecolor=face, label=label)
               for marker, color, face, label in [
                   ("o", FRESH, "white", "Fresh queue (0% archived)"),
                   ("D", LATE, LATE, "Late queue (95% archived)")]]
    fig.legend(handles=handles, loc="center left", bbox_to_anchor=(.027, .72), ncol=2,
               frameon=False, handletextpad=.2, columnspacing=1.6, labelcolor=INK, fontsize=10.5)
    fig.savefig(HERE / "query-latency.png", dpi=180)
    plt.close(fig)


def scope_chart(report):
    fig = figure(4.65, "How experiment selection changes the result",
                 "100,000 synthetic traces  ·  Batch limit 100  ·  Bounded probe time divided by global query time")
    cases = {(tuple(c["experiment_ids"]), c["archived_pct"]): c for c in report["cases"]
             if c["rows"] == 100_000 and c["batch_size"] == 100}
    scopes = [((1,), "Frequent experiment"), ((8,), "Rare experiment"), (ALL, "All eight experiments")]
    fig.text(.036, .696, "Archived", color=MUTED, fontsize=10.5, weight="bold")
    for i, (scope, title) in enumerate(scopes):
        ax = fig.add_axes([.13 + i * .286, .215, .251, .445])
        log_axis(ax, [.25, .5, 1, 2, 4], ["¼×", "½×", "1×", "2×", "4×"])
        ax.set(xlim=(.15, 6.6), ylim=(4.55, -.55))
        ax.set_title(title, fontsize=12, loc="center", color=INK, weight="bold", pad=12)
        ax.set_yticks(range(len(DEPTHS)))
        ax.tick_params(axis="y", labelleft=False)
        ax.axvline(1, color="#6d8088", lw=1.15, zorder=2)
        for y, pct in enumerate(DEPTHS):
            values = cases[scope, pct]["strategies"]
            ratio = median_ms(values["bounded_probe"]) / median_ms(values["global"])
            color = FASTER if ratio <= 1 else SLOWER
            ax.hlines(y, 1, ratio, color=color, lw=2.0, zorder=2)
            ax.plot(ratio, y, marker="o", mfc=color, mec="white", mew=.8, ms=7, zorder=3)
            offset, align = (-6, "right") if ratio <= 1 else (6, "left")
            ax.annotate(f"{ratio:.2f}×", (ratio, y), xytext=(offset, 0), textcoords="offset points",
                        va="center", ha=align, fontsize=10, color=color, weight="bold")
            if i == 0:
                ax.text(-.12, y, f"{pct}%", transform=ax.get_yaxis_transform(), ha="right",
                        va="center", color=INK, fontsize=11.5)
    fig.text(.5, .065, "Relative median time (log scale)  ·  Below 1× favors the bounded probe",
             ha="center", fontsize=11, color=MUTED)
    fig.savefig(HERE / "scope-and-depth.png", dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    latency_chart(load_report("million-start.json.gz"), load_report("million-late.json.gz"))
    scope_chart(load_report("matrix.json.gz"))
