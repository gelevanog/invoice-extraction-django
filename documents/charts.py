"""Server-side geometry for the small inline-SVG accuracy chart (no JavaScript).

One shared 0-100% axis: a column per reviewed invoice (its share of fields accepted
unchanged) and a line for cumulative accuracy. Columns are capped at 24px with a 4px
rounded top and square baseline; hover text comes from SVG ``<title>`` elements.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from documents.accuracy import TimelinePoint

WIDTH, HEIGHT = 1200, 220
LEFT, RIGHT, TOP, BOTTOM = 44, 44, 14, 30
MAX_BAR, GAP, RADIUS = 24.0, 2.0, 4.0


@dataclass(frozen=True, slots=True)
class Column:
    path: str  # SVG path of the column (empty for 0%)
    hit_x: float  # hover target spanning the whole band
    hit_width: float
    label_x: float
    label: str
    title: str


@dataclass(frozen=True, slots=True)
class Tick:
    y: float
    label: str


@dataclass(frozen=True, slots=True)
class TimelineChart:
    width: int
    height: int
    plot_left: int
    plot_right: int
    baseline: float
    columns: list[Column]
    ticks: list[Tick]
    line: str  # polyline points of cumulative accuracy
    end_x: float
    end_y: float
    end_label: str


def _y(rate: float) -> float:
    return TOP + (1 - rate) * (HEIGHT - TOP - BOTTOM)


def _column_path(x: float, width: float, top: float, baseline: float) -> str:
    """Rectangle with rounded top corners and a square baseline."""
    r = min(RADIUS, width / 2, baseline - top)
    return (
        f"M{x:.1f},{baseline:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} "
        f"H{x + width - r:.1f} Q{x + width:.1f},{top:.1f} {x + width:.1f},{top + r:.1f} "
        f"V{baseline:.1f} Z"
    )


def timeline_chart(points: Sequence[TimelinePoint]) -> TimelineChart | None:
    if not points:
        return None
    plot_width = WIDTH - LEFT - RIGHT
    band = plot_width / len(points)
    bar = min(MAX_BAR, band - GAP)
    baseline = _y(0.0)
    label_every = max(1, round(len(points) / 12))  # keep x labels from colliding
    columns, line = [], []
    for index, point in enumerate(points):
        center = LEFT + band * (index + 0.5)
        top = _y(point.rate)
        columns.append(
            Column(
                path=_column_path(center - bar / 2, bar, top, baseline) if point.rate else "",
                hit_x=LEFT + band * index,
                hit_width=band,
                label_x=center,
                label=f"#{point.document_id}" if index % label_every == 0 else "",
                title=(
                    f"#{point.document_id} {point.filename} ({point.vendor}): "
                    f"{point.reviewed - point.corrected}/{point.reviewed} fields accepted "
                    f"({point.rate:.0%}); cumulative {point.cumulative_rate:.0%}"
                ),
            )
        )
        line.append(f"{center:.1f},{_y(point.cumulative_rate):.1f}")
    last = points[-1]
    return TimelineChart(
        width=WIDTH,
        height=HEIGHT,
        plot_left=LEFT,
        plot_right=WIDTH - RIGHT,
        baseline=baseline,
        columns=columns,
        ticks=[Tick(_y(rate), f"{rate:.0%}") for rate in (0.0, 0.5, 1.0)],
        line=" ".join(line),
        end_x=LEFT + band * (len(points) - 0.5),
        end_y=_y(last.cumulative_rate),
        end_label=f"{last.cumulative_rate:.0%}",
    )
