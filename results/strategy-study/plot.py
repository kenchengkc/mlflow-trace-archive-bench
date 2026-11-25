"""Rebuild the light and dark README charts from the published reports. Requires Matplotlib."""
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.ticker import NullLocator
from matplotlib.transforms import blended_transform_factory


HERE = Path(__file__).resolve().parent
DEPTHS = (0, 50, 95, 99, 100)
ALL = tuple(range(1, 9))
STRATEGIES = {"global": "Global (MLflow)", "per_experiment": "Per experiment", "bounded_probe": "Bounded probe"}


@dataclass(frozen=True)
class Theme:
    background: str
    ink: str
    muted: str
    grid: str
    reference: str
    fresh: str
    late: str
    faster: str
    slower: str


LIGHT = Theme(background="#ffffff", ink="#172f40", muted="#485d6a", grid="#dce4e8", reference="#657983",
              fresh="#305e82", late="#784d88", faster="#086f65", slower="#a0441f")
DARK = Theme(background="#0d1117", ink="#edf3f7", muted="#b2c0cb", grid="#35424d", reference="#8295a1",
             fresh="#8dc9f5", late="#dda8ee", faster="#6cdbca", slower="#ffb58c")


def load_report(name):
    with gzip.open(HERE / name, "rt", encoding="utf-8") as source:
        return json.load(source)


def total_ms(measurement):
    pairs = zip(measurement["planning_samples_ms"], measurement["samples_ms"], strict=True)
    return [planning + execution for planning, execution in pairs]


def figure(height, title, theme):
    fig = plt.figure(figsize=(9.4, height), facecolor=theme.background)
    fig.text(.035, 1 - .22 / height, title, va="top", fontsize=20, weight="bold", color=theme.ink)
    return fig


def log_axis(ax, ticks, labels, theme):
    ax.set_facecolor(theme.background)
    ax.set_xscale("log")
    ax.set_xticks(ticks, labels)
    ax.xaxis.set_minor_locator(NullLocator())
    ax.spines[:].set_visible(False)
    ax.tick_params(length=0, colors=theme.muted, labelsize=10, pad=7)
    ax.set_axisbelow(True)
    ax.grid(axis="x", color=theme.grid, linewidth=.8)


def latency_chart(start, late, theme, suffix):
    fig = figure(4.35, "Time to select the next 100 traces", theme)
    ax = fig.add_axes([.25, .16, .37, .63])
    log_axis(ax, [1, 10, 100, 1000], ["1", "10", "100", "1,000"], theme)
    ax.set(xlim=(.75, 2600), ylim=(7.05, -.95), yticks=[])
    ax.set_xlabel("Planning + execution time (ms, log scale)", fontsize=11, color=theme.muted, labelpad=7)
    # Align the text columns to the plotted rows while keeping the log scale independent.
    row_coordinates = blended_transform_factory(fig.transFigure, ax.transData)
    for x, title in [(.757, "Median (ms)"), (.965, "Six-run range (ms)")]:
        fig.text(x, .82, title, fontsize=11, weight="bold", ha="right", color=theme.muted)

    for offset, report, color, marker in [(0, start, theme.fresh, "o"), (4, late, theme.late, "D")]:
        case = report["cases"][0]
        fig.text(.035, offset - .65, f"{case['archived_pct']}% archived", transform=row_coordinates,
                 color=color, fontsize=12.5, weight="bold", va="center")
        for i, (key, label) in enumerate(STRATEGIES.items()):
            samples = total_ms(case["strategies"][key])
            middle, low, high = median(samples), min(samples), max(samples)
            y = offset + i + .33
            ax.errorbar(middle, y, xerr=[[middle - low], [high - middle]],
                        fmt=marker, ms=5.4, capsize=3.6, elinewidth=1.7, capthick=1.3,
                        color=color, markerfacecolor=theme.background, markeredgewidth=1.5, zorder=3)
            for x, text, align, weight in [
                (.035, label, "left", "normal"), (.757, f"{middle:,.2f}", "right", "bold"),
                (.965, f"{low:,.2f}–{high:,.2f}", "right", "normal")
            ]:
                fig.text(x, y, text, transform=row_coordinates, ha=align, va="center",
                         fontsize=11.5, weight=weight, color=theme.ink)

    fig.savefig(HERE / f"query-latency{suffix}.png", dpi=180)
    plt.close(fig)


def scope_chart(report, theme, suffix):
    fig = figure(3.95, "Bounded probe time by experiment scope", theme)
    cases = {(tuple(c["experiment_ids"]), c["archived_pct"]): c for c in report["cases"]
             if c["rows"] == 100_000 and c["batch_size"] == 100}
    scopes = [((1,), "Frequent experiment (ID 1)"), ((8,), "Rare experiment (ID 8)"), (ALL, "All eight experiments")]
    fig.text(.036, .80, "Archived", color=theme.muted, fontsize=10.5, weight="bold")
    for i, (scope, title) in enumerate(scopes):
        ax = fig.add_axes([.13 + i * .286, .22, .251, .54])
        log_axis(ax, [.25, .5, 1, 2, 4], ["¼×", "½×", "1×", "2×", "4×"], theme)
        ax.set(xlim=(.15, 6.6), ylim=(4.55, -.55))
        ax.set_title(title, fontsize=11.5, color=theme.ink, weight="bold", pad=12)
        ax.set_yticks(range(len(DEPTHS)))
        ax.tick_params(axis="y", labelleft=False)
        ax.axvline(1, color=theme.reference, lw=1.15, zorder=2)
        for y, pct in enumerate(DEPTHS):
            measures = cases[scope, pct]["strategies"]
            ratio = median(total_ms(measures["bounded_probe"])) / median(total_ms(measures["global"]))
            color = theme.faster if ratio <= 1 else theme.slower
            ax.hlines(y, 1, ratio, color=color, lw=2, zorder=2)
            ax.plot(ratio, y, marker="o", mfc=color, mec=theme.background, mew=.8, ms=7, zorder=3)
            offset, align = (-6, "right") if ratio <= 1 else (6, "left")
            ax.annotate(f"{ratio:.2f}×", (ratio, y), xytext=(offset, 0), textcoords="offset points",
                        va="center", ha=align, fontsize=10, color=color, weight="bold")
            if i == 0:
                ax.text(-.12, y, f"{pct}%", transform=ax.get_yaxis_transform(), ha="right",
                        va="center", color=theme.ink, fontsize=11.5)
    fig.text(.5, .055, "Median time ratio (bounded probe / global query, log scale)",
             ha="center", fontsize=11, color=theme.muted)
    fig.savefig(HERE / f"scope-and-depth{suffix}.png", dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    start = load_report("million-start.json.gz")
    late = load_report("million-late.json.gz")
    matrix = load_report("matrix.json.gz")
    for suffix, theme in [("", LIGHT), ("-dark", DARK)]:
        latency_chart(start, late, theme, suffix)
        scope_chart(matrix, theme, suffix)
