from pathlib import Path
patterns = {'donut': 'donut_fig.update_layout', 'pie': 'pie_fig.update_layout', 'gauge': 'gauge_fig.update_layout'}
path = Path('allure_reports/allure_visualizer.py')
text = path.read_text()
lines = text.splitlines()
for i,line in enumerate(lines,1):
    for label, pattern in patterns.items():
        if pattern in line:
            print(label, i)
