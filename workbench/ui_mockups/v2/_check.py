import os, re
os.chdir(r"C:\Users\xuan\Desktop\桌面\股票\workbench\ui_mockups\v2")

with open("p11_agents.html", "r", encoding="utf-8") as f:
    html = f.read()

m = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
s = m.group(1) if m else ""
for k in ["field-label", "config-actions", "agent-card-head", "agent-results"]:
    print(f"  .{k}: {'OK' if k in s else 'MISSING'} ")

for kw in ["field-label", "config-actions"]:
    print(f'  HTML contains "{kw}": {kw in html}')