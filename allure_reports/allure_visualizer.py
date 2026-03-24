import os
import sys
import argparse
import duckdb
import matplotlib.pyplot as plt
from matplotlib.colors import to_hex
import matplotlib.dates as mdates
from matplotlib.collections import LineCollection
from matplotlib.patches import Patch
import json
import html
import re
import pandas as pd
import numpy as np
from collections import Counter
from pathlib import Path
from typing import Optional, List, Tuple

try:
    import plotly.io as pio
    from plotly.tools import mpl_to_plotly
    import plotly.graph_objects as go
    PLOTLY_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    pio = None
    mpl_to_plotly = None
    go = None
    PLOTLY_AVAILABLE = False

# --- Configuration ---
# By default write charts to the directory where this script lives to avoid creating
# a nested `allure_reports` folder when running from inside the `allure_reports` dir.
DEFAULT_OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Helper Functions ---

def _interactive_output_path(static_path: str, enabled: bool) -> Optional[str]:
    if not enabled or not static_path:
        return None
    stem, _ = os.path.splitext(static_path)
    return f"{stem}.html"


def _matplotlib_to_plotly(fig):
    if not PLOTLY_AVAILABLE:
        raise RuntimeError("Plotly is not installed")
    converter = getattr(pio, "from_matplotlib", None)
    if callable(converter):
        return converter(fig)
    if mpl_to_plotly:
        return mpl_to_plotly(fig)
    raise AttributeError(
        "Plotly installation does not expose from_matplotlib or mpl_to_plotly. "
        "Upgrade plotly>=5.24 or ensure plotly.tools is available."
    )


def _plotly_color_sequence(count: int):
    """Return a sequence of Plotly-friendly hex colors derived from a Matplotlib colormap."""
    if count <= 0:
        return []
    cmap = plt.colormaps.get_cmap("tab20")
    samples = np.linspace(0, 1, count)
    return [to_hex(cmap(sample)) for sample in samples]


def _ensure_plotly_html_dimensions(path: Optional[str]):
    """Post-process Plotly HTML so the graph div stretches and existing head content stays intact."""
    if not path:
        return
    try:
        html_text = Path(path).read_text(encoding='utf-8')
    except Exception:
        return

    style_block = (
        "<style>"
        "html,body{height:100%;margin:0;padding:0;background:transparent;}"
        ".plotly-graph-div{width:100% !important;height:100% !important;margin:0!important;padding:0!important;}"
        "</style>\n"
    )
    target = "<head>"
    if style_block.strip() in html_text:
        return
    if target not in html_text:
        return
    html_text = html_text.replace(target, f"{target}\n{style_block}", 1)

    html_text, _ = re.subn(
        r'(<div[^>]*class="plotly-graph-div"[^>]*?)style="[^"]*"',
        r'\1style="width:100%;height:100%;"',
        html_text,
        count=1,
    )
    try:
        Path(path).write_text(html_text, encoding='utf-8')
    except Exception:
        pass

def _apply_plotly_embed_defaults(fig):
    """Normalize Plotly figures so they render with legible text and backgrounds."""
    if not PLOTLY_AVAILABLE or fig is None or not getattr(fig, "layout", None):
        return
    layout = fig.layout
    if not layout.template:
        layout.template = "plotly_white"
    layout.paper_bgcolor = layout.paper_bgcolor or "rgba(255,255,255,0)"
    layout.plot_bgcolor = layout.plot_bgcolor or "rgba(255,255,255,0)"
    layout.font = dict(color="#0d0d0d")
    layout.autosize = True
    layout.margin = dict(t=48, r=40, b=40, l=40)
    layout.width = None
    layout.height = None
    if layout.title:
        layout.title.font = dict(color="#0d0d0d")
    if layout.legend:
        layout.legend.font = dict(color="#0d0d0d")


def _finalize_chart(
    fig,
    static_path: Optional[str],
    interactive_path: Optional[str],
    label: str,
    interactive_fig=None,
):
    saved_targets = []
    if static_path:
        fig.savefig(static_path)
        saved_targets.append(static_path)
    if interactive_path:
        if not PLOTLY_AVAILABLE:
            print(f"Interactive chart requested for {label} but Plotly is not installed.")
        else:
            try:
                figure_to_export = interactive_fig or _matplotlib_to_plotly(fig)
                _apply_plotly_embed_defaults(figure_to_export)
                pio.write_html(
                    figure_to_export,
                    interactive_path,
                    include_plotlyjs='cdn',
                    auto_play=False,
                    full_html=True,
                )
                saved_targets.append(f"{interactive_path} (interactive)")
            except Exception as exc:
                print(f"Failed to export interactive chart for {label}: {exc}")
            else:
                _ensure_plotly_html_dimensions(interactive_path)
    plt.close(fig)
    if saved_targets:
        print(f"{label} saved to {', '.join(saved_targets)}")
    else:
        print(f"{label} rendering skipped (no output paths provided).")


def build_interactive_dashboard(output_dir: str, entries: List[Tuple[str, Optional[str]]]):
    available = []
    for title, path in entries:
        if not path or not os.path.exists(path):
            continue
        rel_path = os.path.relpath(path, output_dir).replace('\\', '/')
        available.append((html.escape(title), rel_path))
    if not available:
        print("Interactive dashboard skipped: no interactive charts were generated.")
        return

    dashboard_path = os.path.join(output_dir, 'interactive_charts.html')
    sections = []
    for escaped_title, rel_path in available:
        iframe_src = f"assets/{rel_path}"
        sections.append(
            f"""
            <section class=\"chart-card\">
                <header>{escaped_title}</header>
                <div class=\"chart-frame\">
                    <iframe src=\"{iframe_src}\" loading=\"lazy\"></iframe>
                </div>
            </section>
            """.strip()
        )

    card_markup = "\n".join(sections)
    html_doc = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <title>Interactive Allure Charts</title>
        <style>
        body {{ font-family: Arial, sans-serif; background:#0e1117; color:#f5f5f5; margin:0; padding:32px; }}
        h1 {{ margin-top:0; font-size:24px; }}
        .charts {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:24px; grid-auto-rows:minmax(420px, 1fr); }}
        @media (max-width: 1024px) {{
            .charts {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
        }}
        @media (max-width: 720px) {{
            .charts {{ grid-template-columns:repeat(1, minmax(0, 1fr)); }}
        }}
        .chart-card {{ background:#1b1f2a; border-radius:12px; padding:18px; box-shadow:0 8px 24px rgba(0,0,0,0.35); display:flex; flex-direction:column; overflow:hidden; min-height:48vh; aspect-ratio:1 / 1; transition: all .3s ease-in-out;}}
        .chart-card:hover {{background: linear-gradient(to right, #4c3ecb, #e353ed);}}
        .chart-card header {{ font-weight:bold; margin-bottom:12px; letter-spacing:0.5px; }}
        .chart-frame {{ flex:1; display:flex; min-height:0; justify-content:center; align-items:center; }}
        .chart-frame iframe {{ flex:1; min-height: 95%; width:100%; border:none; border-radius:8px; background:#fff; }}
        .hint {{ margin-top:16px; font-size:14px; color:#cfd8dc; }}
    </style>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <meta http-equiv=\"Cache-Control\" content=\"no-store\" />
    <meta http-equiv=\"Pragma\" content=\"no-cache\" />
    <meta http-equiv=\"Expires\" content=\"0\" />
    <script>function refreshFrames(){{document.querySelectorAll('iframe').forEach(f => f.contentWindow.location.reload());}}</script>
</head>
<body>
    <h1>Interactive Allure Charts</h1>
    <div class=\"charts\">
        {card_markup}
    </div>
    <p class=\"hint\">Hover any chart for tooltips, scroll to zoom, or click legend items to filter series.</p>
</body>
</html>
"""

    with open(dashboard_path, 'w', encoding='utf-8') as dash_file:
        dash_file.write(html_doc)
    print(f"Interactive dashboard saved to '{dashboard_path}'")

def _latest_json_mtime(directory: Path) -> float:
    """Return the most recent modification time among JSON files in directory."""
    latest = directory.stat().st_mtime
    for entry in directory.glob('*.json'):
        try:
            latest = max(latest, entry.stat().st_mtime)
        except FileNotFoundError:
            continue
    return latest


def _auto_detect_org_allure_dir(repo_root: Path):
    """Pick the newest allure-results directory under backend/organizations automatically."""
    org_root = repo_root / 'backend' / 'organizations'
    if not org_root.is_dir():
        return None

    best_path = None
    best_mtime = -1.0

    for account_dir in org_root.iterdir():
        if not account_dir.is_dir():
            continue
        for project_dir in account_dir.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / 'generated_runs' / 'src' / 'allure-results'
            if not candidate.is_dir():
                continue
            try:
                mtime = _latest_json_mtime(candidate)
            except FileNotFoundError:
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best_path = candidate

    if best_path is not None:
        rel = best_path.relative_to(repo_root)
        print(f"Auto-detected Allure results directory at '{rel}'")
        return str(best_path)

    return None

def find_allure_files(directory):
    """Finds all allure result files in the given directory."""
    if not os.path.isdir(directory):
        print(f"Error: Allure results directory not found at '{directory}'")
        return []
    
    print(f"Searching for .json files in '{directory}'...")
    result_files = []
    for filename in os.listdir(directory):
        if filename.endswith('.json'):
            result_files.append(os.path.join(directory, filename))
    print(f"Found {len(result_files)} JSON files.")
    return result_files

def create_test_status_chart(df, output_file, interactive_file=None):
    """Creates a bar chart for individual test case statuses."""
    if df.empty:
        print("No test case data to generate a chart.")
        return

    # Sort by name for consistent ordering
    df = df.sort_values('name').reset_index(drop=True)

    plt.figure(figsize=(10, 2 + len(df) * 0.5)) # Adjust height based on number of tests
    status_colors = {'passed': 'green', 'failed': 'red', 'broken': 'orange', 'skipped': 'grey'}
    colors = df['status'].map(status_colors).fillna('blue')

    bars = plt.barh(df['name'], [1] * len(df), color=colors, height=0.6)
    plt.xlabel('Status')
    plt.ylabel('Test Case')
    plt.title('Individual Test Case Status')
    plt.xticks([]) # Hide x-axis ticks
    plt.xlim(0, 1)   # Set x-axis limit

    # Add status text inside the bars
    for i, (bar, status) in enumerate(zip(bars, df['status'])):
        plt.text(0.5, bar.get_y() + bar.get_height()/2,
                 status.capitalize(),
                 ha='center', va='center', color='white', weight='bold', fontsize=10)

    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Test status chart")

def create_step_timing_chart(df, output_file, interactive_file=None):
    """Creates a bar chart for average step duration."""
    df['duration'] = df['stop'] - df['start']
    # show duration in seconds
    df['duration_seconds'] = df['duration'] / 1000
    
    avg_duration = df.groupby('name')['duration_seconds'].mean().reset_index()
    avg_duration = avg_duration.sort_values(by='duration_seconds', ascending=False)

    # Determine color thresholds using percentiles: <=25% green, 25-75% blue, >=75% red
    if avg_duration['duration_seconds'].size == 0:
        print("No step duration data to generate a chart.")
        return

    low_thresh = avg_duration['duration_seconds'].quantile(0.25)
    high_thresh = avg_duration['duration_seconds'].quantile(0.75)

    def color_for_duration(x):
        if x >= high_thresh:
            return 'red'
        elif x >= low_thresh:
            return 'blue'
        else:
            return 'green'

    colors = avg_duration['duration_seconds'].apply(color_for_duration)

    # Special-case: color specific durations yellow (e.g., 13.7 and 13.8 seconds)
    # Compare rounded values to one decimal place to handle minor float differences.
    special_yellow = {13.7, 13.8}
    def override_color(val, current_color):
        if round(val, 1) in special_yellow:
            return 'yellow'
        return current_color

    colors = [override_color(v, c) for v, c in zip(avg_duration['duration_seconds'], colors)]

    plt.figure(figsize=(12, 6))
    step_names = avg_duration['name'].astype(str).tolist()
    positions = np.arange(len(step_names))
    bars = plt.bar(positions, avg_duration['duration_seconds'], color=colors)
    plt.xlabel('Step Name')
    plt.ylabel('Average Duration (seconds)')
    plt.title('Average Step Duration')
    plt.xticks(positions, step_names, rotation=45, ha='right')

    # Annotate bars with duration values (rounded)
    for bar, val in zip(bars, avg_duration['duration_seconds']):
        height = bar.get_height()
        plt.text(bar.get_x() + bar.get_width() / 2, height + max(0.5, 0.02 * height), f"{val:.1f}",
                 ha='center', va='bottom', fontsize=8)

    plt.tight_layout()
    interactive_fig = None
    if PLOTLY_AVAILABLE and interactive_file:
        interactive_fig = go.Figure(
            data=go.Bar(
                x=step_names,
                y=avg_duration['duration_seconds'],
                marker=dict(color=colors),
                hovertemplate="Step: %{x}<br>Average duration: %{y:.1f}s<extra></extra>",
            )
        )
        interactive_fig.update_layout(
            title="Average Step Duration",
            xaxis_title="Step Name",
            yaxis_title="Average Duration (seconds)",
            template="plotly_white",
            paper_bgcolor="rgba(255,255,255,0)",
            plot_bgcolor="rgba(255,255,255,0)",
            font=dict(color="#0d0d0d"),
        )
    _finalize_chart(
        plt.gcf(),
        output_file,
        interactive_file,
        "Step timing chart",
        interactive_fig=interactive_fig,
    )

def create_step_duration_trend_chart(df, output_file, interactive_file=None):
    """Creates a stock-like trend line showing how step durations rise and fall."""
    if df.empty:
        print("No step data to generate a duration trend chart.")
        return

    df = df.copy()
    df['duration_seconds'] = (df['stop'] - df['start']) / 1000.0
    df = df.dropna(subset=['duration_seconds', 'start']).reset_index(drop=True)

    if df.empty:
        print("Step trend chart skipped because computed durations are missing.")
        return

    df = df.sort_values(by='start').reset_index(drop=True)
    step_names = df['name'].astype(str).tolist()
    durations = df['duration_seconds']
    indices = np.arange(len(step_names))

    fig, ax = plt.subplots(figsize=(14, 6))

    if len(durations) > 1:
        segments = [
            ((indices[i], durations.iloc[i]), (indices[i + 1], durations.iloc[i + 1]))
            for i in range(len(durations) - 1)
        ]
        colors = [
            'red' if durations.iloc[i + 1] > durations.iloc[i] else 'green'
            for i in range(len(durations) - 1)
        ]
        line_collection = LineCollection(segments, colors=colors, linewidths=2)
        ax.add_collection(line_collection)
    else:
        ax.plot(indices, durations, color='green', linewidth=2)

    # Plot markers for each step with color based on comparison to previous point
    marker_colors = ['green']
    for i in range(1, len(durations)):
        marker_colors.append('red' if durations.iloc[i] > durations.iloc[i - 1] else 'green')

    ax.scatter(indices, durations, color=marker_colors, zorder=3)
    ax.plot(indices, durations, color='black', linewidth=0.5, alpha=0.6)

    mean_duration = durations.mean()
    ax.axhline(mean_duration, color='blue', linestyle='--', label=f'Average {mean_duration:.2f}s')

    max_idx = durations.idxmax()
    max_duration = durations.iloc[max_idx]
    ax.annotate(f"Longest {max_duration:.2f}s",
                xy=(indices[max_idx], max_duration),
                xytext=(indices[max_idx], max_duration * 1.1),
                arrowprops=dict(arrowstyle='->', color='black'),
                ha='center')

    ax.set_title('Step Duration Trend (stock-style)')
    ax.set_xlabel('Step Execution Order')
    ax.set_ylabel('Duration (seconds)')
    ax.set_xticks(indices)
    ax.set_xticklabels(step_names, rotation=45, ha='right', fontsize=8)
    ax.grid(True, axis='y', alpha=0.4)
    ax.legend(loc='upper right')

    plt.tight_layout()
    interactive_fig = None
    if PLOTLY_AVAILABLE and interactive_file:
        average_line = [mean_duration] * len(step_names)
        interactive_fig = go.Figure()
        interactive_fig.add_trace(
            go.Scatter(
                x=step_names,
                y=durations,
                mode="lines+markers",
                marker=dict(color=marker_colors, size=6),
                line=dict(color="rgba(0, 0, 0, 0.6)", width=1.5),
                hovertemplate="Step: %{x}<br>Duration: %{y:.2f}s<extra></extra>",
                name="Duration",
            )
        )
        interactive_fig.add_trace(
            go.Scatter(
                x=step_names,
                y=average_line,
                mode="lines",
                line=dict(color="#1f77b4", dash="dash", width=2),
                hoverinfo="skip",
                name=f"Average {mean_duration:.2f}s",
            )
        )
        interactive_fig.add_annotation(
            x=step_names[max_idx],
            y=max_duration,
            text=f"Longest {max_duration:.2f}s",
            showarrow=True,
            arrowhead=2,
            ax=0,
            ay=-30,
        )
        interactive_fig.update_layout(
            title="Step Duration Trend (stock-style)",
            xaxis_title="Step Name",
            yaxis_title="Duration (seconds)",
            template="plotly_white",
            paper_bgcolor="rgba(255,255,255,0)",
            plot_bgcolor="rgba(255,255,255,0)",
            font=dict(color="#0d0d0d"),
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
    _finalize_chart(
        plt.gcf(),
        output_file,
        interactive_file,
        "Step duration trend chart",
        interactive_fig=interactive_fig,
    )

def display_executor_info(allure_dir):
    """Reads and displays information from executor.json."""
    executor_file = os.path.join(allure_dir, 'executor.json')
    if not os.path.exists(executor_file):
        print("executor.json not found, skipping executor info.")
        return

    try:
        with open(executor_file, 'r') as f:
            executor_data = json.load(f)

        print("\n--- Executor Information ---")
        print(f"  Executor: {executor_data.get('name', 'N/A')}")
        print(f"  Type: {executor_data.get('type', 'N/A')}")
        print(f"  Build Name: {executor_data.get('buildName', 'N/A')}")
        print(f"  Build URL: {executor_data.get('buildUrl', 'N/A')}")
        print(f"  Report URL: {executor_data.get('reportUrl', 'N/A')}")
        print("--------------------------\n")

    except json.JSONDecodeError:
        print("Error reading executor.json: Invalid JSON")
    except Exception as e:
        print(f"An error occurred while processing executor.json: {e}")

def create_test_duration_chart(df, output_file, top_n=20, interactive_file=None):
    """Creates a horizontal bar chart for the longest running tests."""
    if df.empty:
        print("No test duration data to generate a chart.")
        return
        
    # Sort by duration and take the top N
    longest_tests = df.sort_values(by='duration', ascending=False).head(top_n)

    if longest_tests.empty:
        return

    longest_tests = longest_tests.copy()
    longest_tests['status_normalized'] = longest_tests['status'].fillna('unknown').astype(str).str.lower()
    status_colors = {
        "passed": "#2e7d32",
        "failed": "#c62828",
        "broken": "#f57c00",
        "skipped": "#607d8b",
        "unknown": "#9e9e9e",
    }

    def determine_color(row):
        name = str(row["name"] or "").lower()
        if "positive" in name:
            return "#43a047"  # stronger green for positive tests
        if "negative" in name:
            return "#c62828"  # keep red for negative tests
        return status_colors.get(row["status_normalized"], "#1976d2")

    colors = [determine_color(row) for _, row in longest_tests.iterrows()]

    plt.figure(figsize=(10, 8))
    bars = plt.barh(longest_tests['name'], longest_tests['duration'], color=colors)
    plt.xlabel('Duration (seconds)')
    plt.ylabel('Test Case')
    plt.title(f'Top {top_n} Longest Running Tests')
    plt.gca().invert_yaxis() # To show longest at the top

    # Add duration labels to the bars
    for bar in bars:
        width = bar.get_width()
        plt.text(width + 0.1, bar.get_y() + bar.get_height()/2, f'{width:.2f}s', 
                 ha='left', va='center')

    legend_handles = []
    seen_statuses = set()
    for status, color in status_colors.items():
        if status in longest_tests['status_normalized'].values and status not in seen_statuses:
            legend_handles.append(Patch(color=color, label=status.capitalize()))
            seen_statuses.add(status)
    if legend_handles:
        plt.legend(handles=legend_handles, title="Status", loc="lower right")
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Test duration chart")

def create_duration_distribution_donut_chart(df, output_file, top_n=10, interactive_file=None):
    """Creates a donut chart for test duration distribution."""
    if df.empty:
        print("No test duration data to generate a distribution donut chart.")
        return

    df = df.sort_values(by='duration', ascending=False)
    
    # If more than top_n tests, group the rest into "Others"
    if len(df) > top_n:
        top_df = df.head(top_n)
        others_duration = df.tail(len(df) - top_n)['duration'].sum()
        
        # Create a new DataFrame for the chart
        chart_data = top_df[['name', 'duration']].copy()
        chart_data.loc[top_n] = {'name': 'Others', 'duration': others_duration}
    else:
        chart_data = df[['name', 'duration']].copy()

    # Create the donut chart
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw=dict(aspect="equal"))

    wedges, texts, autotexts = ax.pie(chart_data['duration'], wedgeprops=dict(width=0.5), startangle=90,
                                      autopct='%1.1f%%')

    ax.legend(wedges, chart_data['name'],
              title=f"Top {top_n} Tests by Duration",
              loc="center left",
              bbox_to_anchor=(1, 0, 0.5, 1))

    plt.setp(autotexts, size=8, weight="bold", color="white")
    ax.set_title("Test Duration Distribution")

    total_duration_secs = df['duration'].sum()
    ax.text(0, 0, f'Total Time\n{total_duration_secs:.2f}s', ha='center', va='center', fontsize=18)

    plt.tight_layout()

    interactive_fig = None
    if PLOTLY_AVAILABLE and go:
        colors = _plotly_color_sequence(len(chart_data))
        donut_fig = go.Figure(
            go.Pie(
                labels=chart_data["name"],
                values=chart_data["duration"],
                hole=0.45,
                sort=False,
                hovertemplate="%{label}: %{value:.2f}s (%{percent})<extra></extra>",
                marker=dict(colors=colors, line=dict(color="white", width=1)),
                textinfo="label+percent",
            )
        )
        donut_fig.update_traces(textinfo="percent", insidetextfont=dict(size=14, color="white"), hovertemplate="%{label}: %{value:.2f}s (%{percent})<extra></extra>")
        donut_fig.update_layout(
            title="Test Duration Distribution",
            template="plotly_white",
            paper_bgcolor="rgba(255,255,255,0)",
            plot_bgcolor="rgba(255,255,255,0)",
            font=dict(color="#0d0d0d"),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=-0.1,
                xanchor="center",
                x=0.5,
                font=dict(color="#0d0d0d"),
            ),
            margin=dict(t=60, r=20, b=60, l=20),
            height=360,
            width=360,
            autosize=False,
        )
        donut_fig.add_annotation(
            text=f"Total Time<br>{total_duration_secs:.2f}s",
            x=0.5,
            y=0.5,
            font=dict(color="#0d0d0d", size=14),
            showarrow=False,
        )
        interactive_fig = donut_fig

    _finalize_chart(
        plt.gcf(),
        output_file,
        interactive_file,
        "Duration distribution donut chart",
        interactive_fig=interactive_fig,
    )

# --- Analytics helpers & charts ---

def _normalize_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        text = text[1:-1]
    return text.strip()


def _extract_label_values(labels, candidate_names):
    if not isinstance(labels, list):
        return []
    matches = []
    for label in labels:
        if not isinstance(label, dict):
            continue
        name = (label.get("name") or "").lower()
        if name in candidate_names:
            value = label.get("value") or label.get("text")
            normalized = _normalize_text(value)
            if normalized:
                matches.append(normalized)
    return matches


def _extract_parameter_values(parameters, keyword_fragments):
    if not isinstance(parameters, list):
        return []
    matches = []
    for param in parameters:
        if not isinstance(param, dict):
            continue
        name = (param.get("name") or "").lower()
        value = param.get("value") or param.get("text")
        if value is None:
            continue
        normalized = _normalize_text(value)
        if not normalized:
            continue
        if any(fragment in name for fragment in keyword_fragments):
            matches.append(normalized)
    return matches


def _build_pass_rate_summary(results_df):
    if results_df.empty or "start" not in results_df.columns:
        return pd.DataFrame()
    subset = results_df.dropna(subset=["start", "status"]).copy()
    if subset.empty:
        return pd.DataFrame()
    subset["event_time"] = pd.to_datetime(subset["start"], unit="ms", utc=True, errors="coerce")
    subset = subset.dropna(subset=["event_time"])
    if subset.empty:
        return pd.DataFrame()
    subset["day"] = subset["event_time"].dt.floor("D")
    status_counts = (
        subset.groupby("day")["status_normalized"]
        .value_counts()
        .unstack(fill_value=0)
    )
    summary = status_counts.assign(total=status_counts.sum(axis=1)).reset_index()
    summary["pass_rate"] = summary.get("passed", 0) / summary["total"]
    summary["instability"] = (summary.get("failed", 0) + summary.get("broken", 0)) / summary["total"]
    return summary.sort_values("day")


def _collect_failure_messages(results_df):
    messages = []
    for _, row in results_df.iterrows():
        status = row.get("status_normalized", "")
        if status not in ("failed", "broken"):
            continue
        details = row.get("statusDetails")
        message = None
        if isinstance(details, dict):
            message = details.get("message") or details.get("trace")
        if message:
            snippet = str(message).splitlines()[0]
            if snippet:
                messages.append(snippet)
                continue
        name = row.get("name") or row.get("fullName") or "Unknown failure"
        messages.append(str(name))
    return messages


def _is_healing_candidate(row):
    details = row.get("statusDetails")
    if isinstance(details, dict) and details.get("flaky"):
        return True
    labels = _extract_label_values(row.get("labels"), {"healing", "self-healing", "self_healing"})
    if labels:
        return True
    params = _extract_parameter_values(row.get("parameters"), {"healing"})
    return bool(params)


def create_pass_rate_gauge(results_df, output_file, interactive_file=None):
    if results_df.empty:
        print("Skipping pass rate gauge chart: no data available.")
        return

    if "run_id" in results_df.columns:
        aggregator = results_df.groupby("run_id").agg(
            total=("status_normalized", "size"),
            passed=("status_normalized", lambda s: (s == "passed").sum()),
            failed=("status_normalized", lambda s: s.isin(["failed", "broken"]).sum()),
            event_time=("start", "max"),
        )
        if aggregator.empty:
            print("Skipping pass rate gauge chart: insufficient status data.")
            return
        if aggregator["event_time"].isna().all():
            latest_run = aggregator.iloc[-1]
            latest_run_id = aggregator.index[-1]
        else:
            latest_run_id = aggregator["event_time"].idxmax()
            latest_run = aggregator.loc[latest_run_id]
        label = f"Run {latest_run_id}"
    else:
        total = len(results_df)
        passed = (results_df["status_normalized"] == "passed").sum()
        failed = results_df["status_normalized"].isin(["failed", "broken"]).sum()
        latest_run = {"total": total, "passed": passed, "failed": failed}
        label = "All runs"

    total = latest_run["total"]
    if total == 0:
        print("Skipping pass rate gauge chart: no test counts recorded.")
        return

    passed = latest_run["passed"]
    failed = latest_run["failed"]
    pass_rate = passed / total
    pass_percent = pass_rate * 100
    remaining = max(0, 100 - pass_percent)

    fig, ax = plt.subplots(figsize=(6, 6), subplot_kw=dict(aspect="equal"))
    wedges, _ = ax.pie(
        [pass_percent, remaining],
        startangle=90,
        counterclock=False,
        colors=["#2e7d32", "#c62828"],
        wedgeprops=dict(width=0.4, edgecolor="white"),
    )
    ax.set_title("Current Pass Rate")
    ax.text(0, 0.1, f"{pass_percent:.1f}%", ha="center", va="center", fontsize=28, fontweight="bold")
    ax.text(0, -0.2, label, ha="center", va="center", fontsize=10)
    ax.text(
        0,
        -0.4,
        f"{passed} passed / {failed} failed\n{total} total tests",
        ha="center",
        va="center",
        fontsize=9,
    )
    plt.tight_layout()

    interactive_fig = None
    if PLOTLY_AVAILABLE and go:
        gauge_fig = go.Figure(
            go.Indicator(
                mode="gauge+number",
                value=pass_percent,
                number={"suffix": "%", "font": {"size": 32}},
                gauge={
                    "axis": {"range": [0, 100], "tickmode": "array", "tickvals": [0, 25, 50, 75, 100]},
                    "bar": {"color": "#2e7d32"},
                    "steps": [
                        {"range": [0, pass_percent], "color": "#2e7d32"},
                        {"range": [pass_percent, 100], "color": "#c62828"},
                    ],
                },
            )
        )
        gauge_fig.update_layout(
            title="Current Pass Rate",
            template="plotly_white",
            paper_bgcolor="rgba(255,255,255,0)",
            plot_bgcolor="rgba(255,255,255,0)",
            font=dict(color="#0d0d0d"),
            height=420,
            margin=dict(t=60, b=40, l=40, r=40),
        )
        gauge_fig.add_annotation(
            text=label,
            x=0.5,
            y=0.32,
            showarrow=False,
            font={"size": 12, "color": "#0d0d0d"},
            xref="paper",
            yref="paper",
        )
        gauge_fig.add_annotation(
            text=f"{passed} passed / {failed} failed<br>{total} total tests",
            x=0.5,
            y=0.08,
            showarrow=False,
            font={"size": 10, "color": "#0d0d0d"},
            xref="paper",
            yref="paper",
        )
        interactive_fig = gauge_fig

    _finalize_chart(
        plt.gcf(),
        output_file,
        interactive_file,
        "Pass rate gauge chart",
        interactive_fig=interactive_fig,
    )


def create_stability_trend_chart(results_df, output_file, interactive_file=None):
    summary = _build_pass_rate_summary(results_df)
    if summary.empty:
        print("Skipping stability trend chart: insufficient timestamped data.")
        return
    plt.figure(figsize=(12, 5))
    plt.plot(
        summary["day"],
        summary["instability"] * 100,
        marker="o",
        color="#d84315",
        label="Instability (failed + broken)",
    )
    plt.plot(
        summary["day"],
        summary["pass_rate"] * 100,
        marker="o",
        color="#1565c0",
        linestyle="--",
        label="Pass rate",
    )
    plt.title("Stability Trend")
    plt.xlabel("Day")
    plt.ylabel("Rate (%)")
    plt.xticks(rotation=30)
    plt.grid(True, axis="y", linestyle="--", alpha=0.5)
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.legend()
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Stability trend chart")


def create_failed_broken_distribution_chart(results_df, output_file, interactive_file=None):
    counts = results_df["status_normalized"].value_counts()
    data = [
        ("failed", counts.get("failed", 0)),
        ("broken", counts.get("broken", 0)),
    ]
    total = sum(value for _, value in data)
    if total == 0:
        print("No failed or broken results to generate distribution chart.")
        return
    labels, values = zip(*data)
    colors = ["#c62828", "#ef6c00"]
    plt.figure(figsize=(6, 4))
    bars = plt.bar(labels, values, color=colors)
    plt.title("Failed / Broken Distribution")
    plt.ylabel("Count")
    for bar in bars:
        height = bar.get_height()
        plt.text(
            bar.get_x() + bar.get_width() / 2,
            height + 0.1,
            f"{int(height)}",
            ha="center",
            va="bottom",
        )
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Failed/broken distribution chart")


def create_problem_category_chart(results_df, output_file, top_n=6, interactive_file=None):
    messages = _collect_failure_messages(results_df)
    if not messages:
        print("No failure messages to analyze problem categories.")
        return
    counter = Counter(messages)
    entries = counter.most_common(top_n)
    labels, counts = zip(*entries)
    plt.figure(figsize=(10, max(4, len(labels) * 0.5)))
    plt.barh(labels[::-1], counts[::-1], color="#3949ab")
    plt.title("Top Problem Categories")
    plt.xlabel("Occurrences")
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Problem category chart")


def create_feature_heatmap(results_df, output_file, interactive_file=None):
    rows = []
    for _, row in results_df.iterrows():
        feature_values = _extract_label_values(row.get("labels"), {"feature"})
        fallback = _extract_label_values(row.get("labels"), {"suite", "story", "parentSuite", "package"})
        feature = feature_values[0] if feature_values else (fallback[0] if fallback else None)
        if not feature:
            continue
        status = row.get("status_normalized", "unknown")
        rows.append({"feature": feature, "status": status})
    if not rows:
        print("No feature labels found to build a heatmap.")
        return
    feature_df = pd.DataFrame(rows)
    pivot = feature_df.pivot_table(index="feature", columns="status", aggfunc="size", fill_value=0)
    pivot = pivot.reindex(pivot.sum(axis=1).sort_values(ascending=False).index)
    fig, ax = plt.subplots(figsize=(12, max(4, pivot.shape[0] * 0.4)))
    im = ax.imshow(pivot.values, aspect="auto", cmap="YlGnBu")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, rotation=45, ha="right")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)
    ax.set_title("Feature / Suite Status Heatmap")
    fig.colorbar(im, ax=ax, label="Test count")
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Feature heatmap")


def create_flaky_test_bar_chart(results_df, output_file, top_n=8, interactive_file=None):
    if results_df.empty:
        print("Skipping flaky test chart: dataset is empty.")
        return
    grouping_key = "historyId" if "historyId" in results_df.columns else "name"
    grouped = results_df.groupby(grouping_key)
    records = []
    for key, group in grouped:
        total = len(group)
        failures = group["status_normalized"].isin(["failed", "broken"]).sum()
        failure_rate = failures / total if total else 0
        display_name = group.iloc[0].get("name") or str(key)
        records.append(
            {
                "test": display_name,
                "failure_rate": failure_rate,
                "failures": failures,
                "runs": total,
            }
        )
    records.sort(key=lambda item: item["failure_rate"], reverse=True)
    top_records = records[:top_n]
    if not top_records or top_records[0]["failure_rate"] == 0:
        print("No flaky test candidates detected.")
        return
    labels = [rec["test"] for rec in top_records][::-1]
    rates = [rec["failure_rate"] * 100 for rec in top_records][::-1]
    plt.figure(figsize=(10, max(4, len(labels) * 0.5)))
    plt.barh(labels, rates, color="#6a1b9a")
    plt.title("Flaky Test Failure Rate")
    plt.xlabel("Failure rate (%)")
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Flaky test bar chart")


def create_healing_success_rate_chart(results_df, output_file, interactive_file=None):
    healing_mask = results_df.apply(_is_healing_candidate, axis=1)
    healing_df = results_df[healing_mask]
    if healing_df.empty:
        print("No self-healing metadata available for the healing success rate chart.")
        return
    passed = (healing_df["status_normalized"] == "passed").sum()
    failures = healing_df["status_normalized"].isin(["failed", "broken"]).sum()
    others = len(healing_df) - passed - failures
    labels = ["Healed passes", "Final failures", "Other outcomes"]
    values = [passed, failures, others]
    if sum(values[:2]) == 0:
        print("Healing chart skipped because healing attempts did not resolve into pass/failure outcomes.")
        return
    colors = ["#2e7d32", "#c62828", "#757575"]
    plt.figure(figsize=(6, 6))
    plt.pie(values, labels=labels, autopct="%1.1f%%", colors=colors, startangle=90)
    plt.title("Healing Success Rate")
    centre_circle = plt.Circle((0, 0), 0.70, fc="white")
    plt.gca().add_artist(centre_circle)
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Healing success rate chart")


def create_dom_change_hotspots_chart(results_df, output_file, top_n=6, interactive_file=None):
    page_counts = Counter()
    for _, row in results_df.iterrows():
        status = row.get("status_normalized", "")
        if status not in ("failed", "broken"):
            continue
        pages = _extract_label_values(row.get("labels"), {"story", "suite", "parentSuite", "package"})
        target = pages[0] if pages else "Unknown page"
        page_counts[target] += 1
    if not page_counts:
        print("No DOM change hotspots detected.")
        return
    entries = page_counts.most_common(top_n)
    labels, counts = zip(*entries)
    plt.figure(figsize=(10, max(4, len(labels) * 0.5)))
    plt.barh(labels[::-1], counts[::-1], color="#00838f")
    plt.title("DOM Change Hotspots (pages)")
    plt.xlabel("Failure count")
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "DOM change hotspots chart")


def create_error_pareto_chart(results_df, output_file, interactive_file=None):
    messages = _collect_failure_messages(results_df)
    if not messages:
        print("Skipping error pareto chart: no failure messages.")
        return
    counter = Counter(messages)
    counts = pd.Series(counter).sort_values(ascending=False)
    top_counts = counts.head(10)
    cumulative = top_counts.cumsum()
    percent = cumulative / counts.sum() * 100
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(top_counts.index, top_counts.values, color="#283593")
    ax.set_ylabel("Failure count")
    ax2 = ax.twinx()
    ax2.plot(top_counts.index, percent, color="#d84315", marker="o")
    ax2.set_ylabel("Cumulative %")
    ax.set_title("Error Cause Pareto")
    ax.tick_params(axis="x", rotation=30)
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Error cause pareto chart")


def create_locator_strategy_usage_chart(results_df, output_file, interactive_file=None):
    strategies = []
    keys = {"locator", "selector", "strategy"}
    for _, row in results_df.iterrows():
        values = _extract_parameter_values(row.get("parameters"), keys)
        strategies.extend(values)
        if not values:
            label_values = _extract_label_values(row.get("labels"), {"framework"})
            strategies.extend(label_values)
    if not strategies:
        print("Locator strategy usage chart skipped: no locator metadata.")
        return
    counts = Counter(strategies)
    labels, values = zip(*counts.most_common(6))
    plt.figure(figsize=(10, 5))
    plt.bar(labels, values, color="#6d4c41")
    plt.title("Locator Strategy Usage")
    plt.ylabel("Occurrences")
    plt.xticks(rotation=30)
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Locator strategy usage chart")


def create_execution_duration_trend_chart(test_duration_df, output_file, interactive_file=None):
    if test_duration_df.empty:
        print("Skipping execution duration trend chart: no duration data.")
        return
    durations = test_duration_df.dropna(subset=["start", "duration"]).copy()
    if durations.empty:
        print("Skipping execution duration trend chart: no records with valid start/duration.")
        return
    durations["event_time"] = pd.to_datetime(durations["start"], unit="ms", utc=True, errors="coerce")
    durations = durations.dropna(subset=["event_time"]).sort_values("event_time")
    plt.figure(figsize=(12, 6))
    plt.plot(durations["event_time"], durations["duration"], marker="o", color="#1565c0", label="Duration (s)")
    avg = durations["duration"].mean()
    plt.axhline(avg, color="#ef6c00", linestyle="--", label=f"Average {avg:.2f}s")
    plt.title("Execution Duration Trend")
    plt.xlabel("Execution time")
    plt.ylabel("Duration (s)")
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%H:%M\n%b %d"))
    plt.legend()
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout()
    interactive_fig = None
    if PLOTLY_AVAILABLE and interactive_file:
        event_times = durations["event_time"].dt.tz_convert(None)
        interactive_fig = go.Figure()
        interactive_fig.add_trace(
            go.Scatter(
                x=event_times,
                y=durations["duration"],
                mode="lines+markers",
                marker=dict(color="#1565c0", size=6),
                line=dict(color="#1565c0", width=2),
                hovertemplate="Execution time: %{x}<br>Duration: %{y:.2f}s<extra></extra>",
                name="Duration (s)",
            )
        )
        interactive_fig.add_trace(
            go.Scatter(
                x=event_times,
                y=[avg] * len(event_times),
                mode="lines",
                line=dict(color="#ef6c00", width=2, dash="dash"),
                name=f"Average {avg:.2f}s",
                hoverinfo="skip",
            )
        )
        interactive_fig.update_layout(
            title="Execution Duration Trend",
            xaxis=dict(title="Execution time"),
            yaxis=dict(title="Duration (seconds)"),
            template="plotly_white",
            hovermode="x",
            margin=dict(t=60, r=40, l=60, b=80),
        )

    _finalize_chart(
        plt.gcf(),
        output_file,
        interactive_file,
        "Execution duration trend chart",
        interactive_fig=interactive_fig,
    )


def create_self_healing_vs_failure_chart(results_df, output_file, interactive_file=None):
    healing_mask = results_df.apply(_is_healing_candidate, axis=1)
    healing_df = results_df[healing_mask]
    if healing_df.empty:
        print("Skipping self-healing vs final failure chart: no healing candidates.")
        return
    healed_pass = (healing_df["status_normalized"] == "passed").sum()
    healed_fail = healing_df["status_normalized"].isin(["failed", "broken"]).sum()
    plt.figure(figsize=(6, 5))
    labels = ["Healing resolved", "Healing failed"]
    values = [healed_pass, healed_fail]
    colors = ["#2e7d32", "#c62828"]
    bars = plt.bar(labels, values, color=colors)
    for bar in bars:
        plt.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.1,
            f"{int(bar.get_height())}",
            ha="center",
            va="bottom",
        )
    plt.title("Self-Healing vs Final Failure")
    plt.ylabel("Count")
    plt.tight_layout()
    _finalize_chart(plt.gcf(), output_file, interactive_file, "Self-healing vs final failure chart")

# --- Main Logic ---

def main():
    """Main function to generate allure report charts."""
    print("Starting Allure report visualization...")

    # Parse CLI args (allow overriding the results dir)
    parser = argparse.ArgumentParser(description='Visualize Allure results')
    parser.add_argument('--results-dir', help='Path to allure-results directory')
    parser.add_argument('--output-dir', help='Directory to save generated charts (default: script dir)')
    parser.add_argument('--interactive', action='store_true', help='Also export interactive HTML charts (requires Plotly)')
    parser.add_argument('--interactive-only', action='store_true', help='Skip static PNG exports when generating interactive charts')
    args = parser.parse_args()

    interactive_env = os.environ.get('ALLURE_INTERACTIVE_CHARTS', '').strip().lower()
    interactive_enabled = bool(args.interactive or interactive_env in {'1', 'true', 'yes', 'on'})
    interactive_only_env = os.environ.get('ALLURE_INTERACTIVE_ONLY', '').strip().lower()
    interactive_only = bool(args.interactive_only or interactive_only_env in {'1', 'true', 'yes', 'on'})
    if interactive_only:
        interactive_enabled = True
    if interactive_enabled and not PLOTLY_AVAILABLE:
        print("Interactive charts requested but Plotly is not installed. Install plotly to enable HTML exports.")
        interactive_enabled = False

    # Resolve the results directory in this order: CLI arg -> ENV -> default -> repository search
    def resolve_allure_dir(cli_override=None):
        # CLI override
        if cli_override:
            if os.path.isdir(cli_override):
                print(f"Using CLI-specified Allure results directory: '{cli_override}'")
                return cli_override
            else:
                print(f"CLI-specified path does not exist: '{cli_override}'")

        # Environment variable
        env_dir = os.environ.get('ALLURE_RESULTS_DIR')
        if env_dir:
            if os.path.isdir(env_dir):
                print(f"Using ALLURE_RESULTS_DIR from environment: '{env_dir}'")
                return env_dir
            else:
                print(f"Environment ALLURE_RESULTS_DIR does not exist: '{env_dir}'")

        repo_root_path = Path(__file__).resolve().parent.parent

        # Organization-aware auto detection (picks latest run automatically)
        auto_dir = _auto_detect_org_allure_dir(repo_root_path)
        if auto_dir:
            return auto_dir

        # Fallback: search the repository for any 'allure-results' directories
        repo_root = str(repo_root_path)
        candidates = []
        for root, dirs, files in os.walk(repo_root):
            for d in dirs:
                if d == 'allure-results':
                    candidates.append(os.path.join(root, d))
            # keep search limited to avoid long runs
            if len(candidates) >= 10:
                break

        if candidates:
            print('Could not find configured Allure results directory. Found the following candidates in the repository:')
            for c in candidates:
                print(f" - {c}")
            chosen = candidates[0]
            print(f"Using the first candidate: {chosen}")
            return chosen

        # Nothing found
        print('Error: No Allure results directory found. Checked the CLI arg, environment, default, and repository search.')
        print("Options:\n - Pass --results-dir /path/to/allure-results\n - Set ALLURE_RESULTS_DIR environment variable\n - Ensure results exist at the configured default path")
        return None

    allure_dir = resolve_allure_dir(args.results_dir)
    if not allure_dir:
        return

    allure_files = find_allure_files(allure_dir)
    if not allure_files:
        return

    # Resolve output directory: CLI -> ENV -> default (script dir)
    output_dir = args.output_dir or os.environ.get('ALLURE_OUTPUT_DIR')
    if not output_dir and allure_dir:
        parent_src = Path(allure_dir).resolve().parent
        output_dir = os.path.join(parent_src, 'allure_charts')
    if not output_dir:
        output_dir = DEFAULT_OUTPUT_DIR
    output_dir = os.path.abspath(output_dir)
    # Create output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    if interactive_only:
        # Remove any legacy static charts so only HTML files remain in the output.
        for png_file in Path(output_dir).glob("*.png"):
            try:
                png_file.unlink()
            except FileNotFoundError:
                continue

    # Define chart file paths
    def chart_paths(filename):
        base = os.path.join(output_dir, filename)
        static_path = None if interactive_only else base
        return static_path, _interactive_output_path(base, interactive_enabled)

    TEST_STATUS_CHART_FILE, TEST_STATUS_CHART_HTML = chart_paths('test_status_chart.png')
    STEP_TIMING_CHART_FILE, STEP_TIMING_CHART_HTML = chart_paths('step_timing_chart.png')
    STEP_DURATION_TREND_CHART_FILE, STEP_DURATION_TREND_CHART_HTML = chart_paths('step_duration_trend_chart.png')
    TEST_DURATION_CHART_FILE, TEST_DURATION_CHART_HTML = chart_paths('test_duration_chart.png')
    DURATION_DISTRIBUTION_DONUT_CHART_FILE, DURATION_DISTRIBUTION_DONUT_CHART_HTML = chart_paths('duration_distribution_donut_chart.png')
    PASS_RATE_GAUGE_FILE, PASS_RATE_GAUGE_HTML = chart_paths('pass_rate_gauge.png')
    STABILITY_TREND_FILE, STABILITY_TREND_HTML = chart_paths('stability_trend.png')
    FAILED_BROKEN_DISTRIBUTION_FILE, FAILED_BROKEN_DISTRIBUTION_HTML = chart_paths('failed_broken_distribution.png')
    PROBLEM_CATEGORY_FILE, PROBLEM_CATEGORY_HTML = chart_paths('problem_category_chart.png')
    FEATURE_HEATMAP_FILE, FEATURE_HEATMAP_HTML = chart_paths('feature_heatmap.png')
    FLAKY_TEST_BAR_FILE, FLAKY_TEST_BAR_HTML = chart_paths('flaky_test_bar_chart.png')
    HEALING_SUCCESS_RATE_FILE, HEALING_SUCCESS_RATE_HTML = chart_paths('healing_success_rate.png')
    DOM_CHANGE_HOTSPOTS_FILE, DOM_CHANGE_HOTSPOTS_HTML = chart_paths('dom_change_hotspots.png')
    ERROR_PARETO_FILE, ERROR_PARETO_HTML = chart_paths('error_pareto_chart.png')
    LOCATOR_STRATEGY_FILE, LOCATOR_STRATEGY_HTML = chart_paths('locator_strategy_usage.png')
    EXECUTION_DURATION_TREND_FILE, EXECUTION_DURATION_TREND_HTML = chart_paths('execution_duration_trend.png')
    SELF_HEALING_VS_FAILURE_FILE, SELF_HEALING_VS_FAILURE_HTML = chart_paths('self_healing_vs_failure.png')

    interactive_chart_entries = [
        ("Test status", TEST_STATUS_CHART_HTML),
        ("Step timing", STEP_TIMING_CHART_HTML),
        ("Step duration trend", STEP_DURATION_TREND_CHART_HTML),
        ("Test duration", TEST_DURATION_CHART_HTML),
        ("Duration distribution (donut)", DURATION_DISTRIBUTION_DONUT_CHART_HTML),
        ("Pass rate gauge", PASS_RATE_GAUGE_HTML),
        ("Stability trend", STABILITY_TREND_HTML),
        ("Failed vs broken", FAILED_BROKEN_DISTRIBUTION_HTML),
        ("Problem categories", PROBLEM_CATEGORY_HTML),
        ("Feature heatmap", FEATURE_HEATMAP_HTML),
        ("Flaky tests", FLAKY_TEST_BAR_HTML),
        ("Healing success", HEALING_SUCCESS_RATE_HTML),
        ("DOM hotspots", DOM_CHANGE_HOTSPOTS_HTML),
        ("Error pareto", ERROR_PARETO_HTML),
        ("Locator strategy", LOCATOR_STRATEGY_HTML),
        ("Execution duration trend", EXECUTION_DURATION_TREND_HTML),
        ("Self-healing vs failure", SELF_HEALING_VS_FAILURE_HTML),
    ]


    # Display executor info
    display_executor_info(allure_dir)

    # Use DuckDB to process the JSON files
    con = duckdb.connect(database=':memory:', read_only=False)
    json_path = os.path.join(allure_dir, "*-result.json").replace('\\', '/')

    base_results_query = f"SELECT * FROM read_json_auto('{json_path}')"
    base_results_df = pd.DataFrame()
    try:
        base_results_df = con.execute(base_results_query).fetchdf()
        if not base_results_df.empty:
            base_results_df["status_normalized"] = (
                base_results_df["status"].fillna("unknown").astype(str).str.lower()
            )
        else:
            base_results_df["status_normalized"] = pd.Series(dtype=str)
    except Exception as e:
        print(f"Failed to load base Allure data for analytics: {e}")
        base_results_df = pd.DataFrame()
    if "status_normalized" not in base_results_df.columns:
        base_results_df["status_normalized"] = pd.Series(dtype=str)


    # Query for test case status
    test_case_query = f"""
        SELECT
            name,
            status
        FROM
            read_json_auto('{json_path}')
        WHERE
            status IS NOT NULL
    """
    try:
        test_case_df = con.execute(test_case_query).fetchdf()
        if not test_case_df.empty:
            create_test_status_chart(
                test_case_df,
                TEST_STATUS_CHART_FILE,
                interactive_file=TEST_STATUS_CHART_HTML,
            )
    except Exception as e:
        print(f"Could not query test case statuses. Error: {e}")


    # Query for step timings
    step_timing_query = f"""
        SELECT
            step.name,
            step.start,
            step.stop,
            name as test_name
        FROM (
            SELECT
                name,
                UNNEST(steps) as step
            FROM
                read_json_auto('{json_path}')
            WHERE
                json_type(steps) = 'ARRAY' AND array_length(steps) > 0
        )
        WHERE
            step.start IS NOT NULL AND step.stop IS NOT NULL
    """
    try:
        step_timing_df = con.execute(step_timing_query).fetchdf()
        if not step_timing_df.empty:
            create_step_timing_chart(
                step_timing_df,
                STEP_TIMING_CHART_FILE,
                interactive_file=STEP_TIMING_CHART_HTML,
            )
            create_step_duration_trend_chart(
                step_timing_df,
                STEP_DURATION_TREND_CHART_FILE,
                interactive_file=STEP_DURATION_TREND_CHART_HTML,
            )
        else:
            print("No step timing information found in the allure results.")
    except Exception as e:
        print(f"Could not query step timings. Error: {e}")
    
    # --- Test Duration Analysis ---
    print("\n--- Analyzing Test Durations ---")
    test_duration_df = pd.DataFrame()
    diagnostic_query = f"""
        SELECT
            COUNT(*) as total_results,
            COUNT(CASE WHEN start IS NOT NULL AND stop IS NOT NULL THEN 1 END) as with_duration
        FROM read_json_auto('{json_path}')
    """
    try:
        diag_df = con.execute(diagnostic_query).fetchdf()
        total_results = diag_df['total_results'][0]
        with_duration = diag_df['with_duration'][0]
        
        print(f"Found {total_results} test results in total.")
        print(f"Found {with_duration} results with valid start/stop times for duration analysis.")

        if with_duration > 0:
            test_duration_query = f"""
                SELECT
                    name,
                    start,
                    (stop - start) / 1000.0 as duration,
                    COALESCE(status, 'unknown') as status
                FROM read_json_auto('{json_path}')
                WHERE start IS NOT NULL AND stop IS NOT NULL AND name IS NOT NULL
            """
            test_duration_df = con.execute(test_duration_query).fetchdf()
            if not test_duration_df.empty:
                print("Generating duration-based charts...")
                create_test_duration_chart(
                    test_duration_df,
                    TEST_DURATION_CHART_FILE,
                    interactive_file=TEST_DURATION_CHART_HTML,
                )
                create_duration_distribution_donut_chart(
                    test_duration_df,
                    DURATION_DISTRIBUTION_DONUT_CHART_FILE,
                    interactive_file=DURATION_DISTRIBUTION_DONUT_CHART_HTML,
                )
            else:
                print("WARNING: Failed to generate duration charts even though timing data was expected.")
        else:
            print("\nWARNING: No test results contain the necessary 'start' and 'stop' timestamps.")
            print("         Duration-based charts will not be generated.")
            print("         Please check if your Allure test adapter is configured to record test timings.")

    except Exception as e:
        print(f"An error occurred during test duration analysis: {e}")

    create_pass_rate_gauge(
        base_results_df,
        PASS_RATE_GAUGE_FILE,
        interactive_file=PASS_RATE_GAUGE_HTML,
    )
    create_stability_trend_chart(
        base_results_df,
        STABILITY_TREND_FILE,
        interactive_file=STABILITY_TREND_HTML,
    )
    create_failed_broken_distribution_chart(
        base_results_df,
        FAILED_BROKEN_DISTRIBUTION_FILE,
        interactive_file=FAILED_BROKEN_DISTRIBUTION_HTML,
    )
    create_problem_category_chart(
        base_results_df,
        PROBLEM_CATEGORY_FILE,
        interactive_file=PROBLEM_CATEGORY_HTML,
    )
    create_feature_heatmap(
        base_results_df,
        FEATURE_HEATMAP_FILE,
        interactive_file=FEATURE_HEATMAP_HTML,
    )
    create_flaky_test_bar_chart(
        base_results_df,
        FLAKY_TEST_BAR_FILE,
        interactive_file=FLAKY_TEST_BAR_HTML,
    )
    create_healing_success_rate_chart(
        base_results_df,
        HEALING_SUCCESS_RATE_FILE,
        interactive_file=HEALING_SUCCESS_RATE_HTML,
    )
    create_dom_change_hotspots_chart(
        base_results_df,
        DOM_CHANGE_HOTSPOTS_FILE,
        interactive_file=DOM_CHANGE_HOTSPOTS_HTML,
    )
    create_error_pareto_chart(
        base_results_df,
        ERROR_PARETO_FILE,
        interactive_file=ERROR_PARETO_HTML,
    )
    create_locator_strategy_usage_chart(
        base_results_df,
        LOCATOR_STRATEGY_FILE,
        interactive_file=LOCATOR_STRATEGY_HTML,
    )
    create_execution_duration_trend_chart(
        test_duration_df,
        EXECUTION_DURATION_TREND_FILE,
        interactive_file=EXECUTION_DURATION_TREND_HTML,
    )
    create_self_healing_vs_failure_chart(
        base_results_df,
        SELF_HEALING_VS_FAILURE_FILE,
        interactive_file=SELF_HEALING_VS_FAILURE_HTML,
    )

    if interactive_enabled:
        build_interactive_dashboard(output_dir, interactive_chart_entries)

    con.close()
    print("Allure report visualization finished.")

if __name__ == '__main__':
    main()
