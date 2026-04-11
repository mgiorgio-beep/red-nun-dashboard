import re
f = open('static/index.html', 'r').read()

# 1. Replace tab button
f = f.replace(
    """<button class="tab" onclick="switchTab(this, 'pour')">Pour Cost</button>""",
    """<button class="tab" onclick="switchTab(this, 'food')">Food Cost</button>\n  <button class="tab" onclick="switchTab(this, 'bev')">Bev Cost</button>"""
)

print("Step 1 done - tabs replaced")
open('static/index.html', 'w').write(f)
print("Saved")
