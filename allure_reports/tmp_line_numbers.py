from pathlib import Path
text=Path('allure_reports/allure_visualizer.py').read_text().splitlines()
targets=['_ensure_plotly_html_dimensions','_apply_plotly_embed_defaults','build_interactive_dashboard']
for target in targets:
    for idx,line in enumerate(text,1):
        if target in line:
            print(target, idx)
