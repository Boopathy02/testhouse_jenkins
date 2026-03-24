from pathlib import Path
path = Path('allure_reports/allure_visualizer.py')
text = path.read_text()
lines = text.splitlines()
for i,line in enumerate(lines,1):
    if 'charts {' in line:
        print(i, line)
