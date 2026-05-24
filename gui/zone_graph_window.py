"""
Zone Graph Window  --  embedded matplotlib + networkx view of the
ZoneMapper's recorded zones and transitions.

Live-updates by polling the mapper every REFRESH_MS milliseconds and
re-rendering only when the underlying data has changed.

Node color encodes wisp availability:
  - green  -> health wisps only
  - blue   -> mana wisps only
  - cyan   -> both
  - gray   -> none observed
The current zone (if known) is outlined.
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg,
    NavigationToolbar2Tk,
)
import networkx as nx

from zone_mapper import ZoneMapper


REFRESH_MS = 2000  # how often to poll the mapper for changes


def _node_color(zone_data: dict) -> str:
    h = bool(zone_data.get("has_health_wisps"))
    m = bool(zone_data.get("has_mana_wisps"))
    if h and m:
        return "#5fc8c8"      # cyan
    if h:
        return "#6aad6a"      # green
    if m:
        return "#5a93d4"      # blue
    return "#888888"          # gray


def _short_name(zone: str) -> str:
    """Strip leading 'World/' so labels stay readable."""
    return zone.split("/", 1)[1] if "/" in zone else zone


class ZoneGraphWindow:
    """A Toplevel window with a live-updating zone graph."""

    def __init__(
        self,
        parent: tk.Widget,
        zone_mapper: ZoneMapper,
        get_current_zone=None,
    ):
        self._mapper = zone_mapper
        self._get_current_zone = get_current_zone or (lambda: None)
        self._last_signature: Optional[tuple] = None
        self._after_id: Optional[str] = None
        self._layout_cache: dict = {}

        self._win = tk.Toplevel(parent)
        self._win.title("Zone Graph")
        self._win.geometry("900x700")
        self._win.protocol("WM_DELETE_WINDOW", self._on_close)

        self._fig = Figure(figsize=(9, 7), dpi=100, facecolor="#2b2b2b")
        self._ax = self._fig.add_subplot(111, facecolor="#2b2b2b")

        self._canvas = FigureCanvasTkAgg(self._fig, master=self._win)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)

        toolbar_frame = ttk.Frame(self._win)
        toolbar_frame.pack(fill="x")
        NavigationToolbar2Tk(self._canvas, toolbar_frame)

        self._render()
        self._schedule_refresh()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_close(self):
        if self._after_id is not None:
            try:
                self._win.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._win.destroy()

    def _schedule_refresh(self):
        self._after_id = self._win.after(REFRESH_MS, self._tick)

    def _tick(self):
        # Re-render only when the graph or current-zone outline changed
        try:
            sig = self._signature()
            if sig != self._last_signature:
                self._render(sig=sig)
        except Exception:
            # Don't let any rendering error kill the refresh loop
            pass
        self._schedule_refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _signature(self) -> tuple:
        """Cheap snapshot of mutable state, used to skip identical renders."""
        s = self._mapper.summary()
        data = self._mapper.dump()
        # Include zone names + wisp flags so flag changes also redraw
        zone_state = tuple(sorted(
            (name, bool(z.get("has_health_wisps")), bool(z.get("has_mana_wisps")))
            for name, z in data.get("zones", {}).items()
        ))
        edges = tuple(sorted(
            (t["from_zone"], t["to_zone"])
            for t in data.get("transitions", [])
        ))
        return (
            s["zone_count"], s["transition_count"],
            zone_state, edges,
            self._get_current_zone(),
        )

    def _render(self, sig: Optional[tuple] = None):
        self._last_signature = sig or self._signature()
        data = self._mapper.dump()
        zones = data.get("zones", {})
        transitions = data.get("transitions", [])
        current_zone = self._get_current_zone()

        self._ax.clear()
        self._ax.set_facecolor("#2b2b2b")
        for spine in self._ax.spines.values():
            spine.set_visible(False)
        self._ax.set_xticks([])
        self._ax.set_yticks([])

        if not zones:
            self._ax.text(
                0.5, 0.5, "No zones recorded yet.\nRun auto-quest to populate the map.",
                ha="center", va="center",
                color="#bbbbbb", fontsize=12,
                transform=self._ax.transAxes,
            )
            self._canvas.draw_idle()
            return

        G = nx.DiGraph()
        for name in zones:
            G.add_node(name)
        for t in transitions:
            G.add_edge(t["from_zone"], t["to_zone"])

        # Stable-ish positions: reuse previous layout for existing nodes,
        # spring-place only the newcomers. Keeps the graph from jumping
        # around on every refresh.
        pos = self._compute_layout(G)

        node_colors = [_node_color(zones[n]) for n in G.nodes()]
        edge_color = "#999999"

        nx.draw_networkx_edges(
            G, pos, ax=self._ax,
            edge_color=edge_color, arrows=True,
            arrowsize=12, width=1.2,
            connectionstyle="arc3,rad=0.08",
        )
        nx.draw_networkx_nodes(
            G, pos, ax=self._ax,
            node_color=node_colors, node_size=550,
            edgecolors="#e8e8e8", linewidths=0.8,
        )

        # Highlight the current zone with a thicker outline
        if current_zone in G.nodes:
            nx.draw_networkx_nodes(
                G, pos, ax=self._ax,
                nodelist=[current_zone],
                node_color=_node_color(zones[current_zone]),
                node_size=700,
                edgecolors="#ffd76a", linewidths=2.5,
            )

        labels = {n: _short_name(n) for n in G.nodes()}
        nx.draw_networkx_labels(
            G, pos, labels=labels, ax=self._ax,
            font_size=8, font_color="#e8e8e8",
        )

        # Legend at the top
        legend_items = [
            ("health", "#6aad6a"),
            ("mana", "#5a93d4"),
            ("both", "#5fc8c8"),
            ("none", "#888888"),
        ]
        handles = [
            self._ax.scatter([], [], c=color, s=80, label=label)
            for label, color in legend_items
        ]
        leg = self._ax.legend(
            handles=handles, loc="upper right",
            facecolor="#3c3f41", edgecolor="#505050",
            labelcolor="#e8e8e8", fontsize=8,
        )
        leg.get_frame().set_alpha(0.9)

        title = f"{len(zones)} zones, {len(transitions)} transitions"
        if current_zone:
            title += f"  --  current: {_short_name(current_zone)}"
        self._ax.set_title(title, color="#e8e8e8", fontsize=10)

        self._fig.tight_layout()
        self._canvas.draw_idle()

    def _compute_layout(self, G: nx.DiGraph) -> dict:
        """Keep existing nodes pinned, only place new nodes."""
        existing = {n: self._layout_cache[n] for n in G.nodes if n in self._layout_cache}
        new_nodes = [n for n in G.nodes if n not in existing]

        if not existing:
            pos = nx.spring_layout(G, seed=42, k=0.9, iterations=80)
        elif not new_nodes:
            pos = existing
        else:
            # Anchor existing positions; let spring layout settle the new ones
            pos = nx.spring_layout(
                G, pos=existing, fixed=list(existing.keys()),
                seed=42, k=0.9, iterations=40,
            )

        self._layout_cache = dict(pos)
        return pos
