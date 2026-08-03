import os, re

os.chdir(r"C:\Users\xuan\Desktop\桌面\股票\workbench\ui_mockups\v2")

with open("p12_settings.html", "r", encoding="utf-8") as f:
    html = f.read()

# Replace the style block
old_style_match = re.search(r'<style>.*?</style>', html, re.DOTALL)
old_style = old_style_match.group()

new_style = """<style>
    body {
      background-image:
        radial-gradient(1100px 560px at 12% -16%, rgba(62, 198, 255, .09), transparent 60%),
        radial-gradient(950px 540px at 92% -6%, rgba(139, 92, 246, .11), transparent 64%),
        radial-gradient(1100px 760px at 50% 116%, rgba(124, 58, 237, .06), transparent 62%);
      background-attachment: fixed;
    }
    .settings-grid { display: grid; grid-template-columns: 1fr; gap: 18px; margin-top: 16px; }
    .settings-block { padding: 18px 20px; border-radius: var(--radius); border: 1px solid rgba(139,92,246,.18); background: linear-gradient(160deg, rgba(62,198,255,.04), rgba(139,92,246,.07)); backdrop-filter: blur(4px); }
    .settings-block h2 { margin: 0 0 8px; font-size: 15px; }
    .settings-block p { margin: 0 0 14px; font-size: 12px; color: var(--text-muted); line-height: 1.7; }
    .grid-form { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; }
    .field-row { display: grid; gap: 4px; font-size: 11px; color: var(--text-muted); }
    .field-row .label { display: flex; align-items: baseline; gap: 6px; font-weight: 500; }
    .field-row .hint { font-size: 10px; color: var(--text-muted); opacity: .9; }
    .field-row input.field, .field-row select.field { width: 100%; min-height: 34px; }
    .key-chip { margin-top: 6px; }
    .security-note {
      padding: 10px 14px; border-radius: var(--radius-sm);
      background: rgba(62,198,255,.06); border: 1px solid rgba(62,198,255,.18);
      font-size: 11px; color: #a5b4fc; margin-top: 10px; line-height: 1.7;
    }
    .save-bar {
      margin-top: 22px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
      padding: 14px 18px; border-radius: var(--radius);
      border: 1px solid rgba(139,92,246,.20);
      background: linear-gradient(135deg, rgba(62,198,255,.05), rgba(139,92,246,.09));
      backdrop-filter: blur(6px);
    }
    .save-status { font-size: 13px; }
    .save-status.ok { color: #3da678; }
    .save-status.err { color: #e05a5a; }
</style>"""

html = html.replace(old_style, new_style)

with open("p12_settings.html", "w", encoding="utf-8") as f:
    f.write(html)

print("p12 style replaced OK")