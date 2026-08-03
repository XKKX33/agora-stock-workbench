import os, re

os.chdir(r"C:\Users\xuan\Desktop\桌面\股票\workbench\ui_mockups\v2")

with open("p11_agents.html", "r", encoding="utf-8") as f:
    html = f.read()

oldm = re.search(r"<style>.*?</style>", html, re.DOTALL)
if not oldm:
    print("NO STYLE FOUND")
    exit(1)

old_style = oldm.group()

NEW = """<style>
    body {
      background-image:
        radial-gradient(1100px 560px at 12% -16%, rgba(62, 198, 255, .09), transparent 60%),
        radial-gradient(950px 540px at 92% -6%, rgba(139, 92, 246, .11), transparent 64%),
        radial-gradient(1100px 760px at 50% 116%, rgba(124, 58, 237, .06), transparent 62%);
      background-attachment: fixed;
    }
    .mode-tabs {
      display: flex; gap: 8px; flex-wrap: wrap;
      margin-bottom: 16px;
      padding: 4px;
      border-radius: var(--radius-pill);
      background: rgba(255,255,255,.03);
    }
    .mode-tabs .tab-btn {
      padding: 7px 20px; border: 1px solid rgba(148,163,184,.18); border-radius: var(--radius-pill);
      background: rgba(255,255,255,.02); font-size: 12px; cursor: pointer; transition: var(--t);
      color: var(--text-muted);
      backdrop-filter: blur(4px);
    }
    .mode-tabs .tab-btn:hover { border-color: rgba(167,139,250,.50); background: rgba(139,92,246,.10); color: #fff; }
    .mode-tabs .tab-btn.active {
      border-color: rgba(139,92,246,.6); background: rgba(139,92,246,.20); color: #fff;
      box-shadow: 0 0 14px rgba(124,58,237,.28);
    }
    .agent-config {
      display: grid; grid-template-columns: auto 1fr; gap: 10px 14px; align-items: end;
      padding: 16px 18px; border-radius: var(--radius);
      border: 1px solid rgba(139,92,246,.18);
      background: linear-gradient(135deg, rgba(62,198,255,.04), rgba(139,92,246,.08));
      margin-bottom: 12px;
    }
    .agent-field {
      display: grid; gap: 4px; font-size: 11px; color: var(--text-muted);
    }
    .agent-field .field-label {
      display: flex; align-items: baseline; gap: 6px;
      font-size: 11px; color: var(--text-muted); letter-spacing: .02em;
    }
    .agent-field .field-label .hint {
      font-size: 10px; color: var(--text-muted); opacity: .8;
      font-weight: 400;
    }
    .agent-field input.field-sm { width: 112px; min-height: 30px; }
    .agent-field input.field-md { width: 200px; min-height: 30px; }
    .agent-field .chk { width: auto; min-height: auto; }
    .agent-config .config-actions {
      grid-column: 1 / -1;
      display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
      padding-top: 6px;
      border-top: 1px solid rgba(148,163,184,.10);
    }
    .agent-status { font-size: 12px; color: var(--text-muted); max-width: 360px; text-align: right; line-height: 1.7; }
    .agent-pool-note { font-size: 12px; color: var(--text-muted); margin-bottom: 8px; }
    .agent-progress {
      margin: 12px 0; padding: 14px 16px; border-radius: var(--radius);
      border: 1px solid rgba(139,92,246,.22);
      background: linear-gradient(135deg, rgba(62,198,255,.05), rgba(139,92,246,.08));
      backdrop-filter: blur(6px);
    }
    .progress-head { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 7px; }
    .progress-track { height: 7px; border-radius: var(--radius-pill); background: var(--navy); overflow: hidden; }
    .progress-fill {
      height: 100%; width: 0; border-radius: var(--radius-pill);
      background: linear-gradient(90deg, #3ec6ff, #8b5cf6);
      box-shadow: 0 0 18px rgba(124,58,237,.55);
      transition: width .3s var(--ease);
    }
    .agent-results {
      display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 14px; margin-top: 12px;
    }
    .agent-card {
      border: 1px solid rgba(139,92,246,.18); border-radius: var(--radius);
      padding: 16px;
      background: linear-gradient(160deg, rgba(62,198,255,.04), rgba(139,92,246,.07));
      transition: var(--t);
      backdrop-filter: blur(4px);
    }
    .agent-card:hover {
      border-color: rgba(139,92,246,.34);
      box-shadow: 0 0 0 1px rgba(139,92,246,.10), 0 0 28px -12px rgba(124,58,237,.26);
    }
    .agent-card-head {
      display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 8px;
      margin-bottom: 6px;
    }
    .agent-card-head .stock-label { display: flex; align-items: center; gap: 8px; }
    .agent-card-head strong { font-size: 15px; }
    .agent-card-head .mono { font-size: 11px; color: var(--text-muted); }
    .verdict-bull { color: #e05a5a; border-color: rgba(224,90,90,.40); }
    .verdict-bear { color: #3da678; border-color: rgba(61,166,120,.40); }
    .verdict-flat { color: #c49a4a; border-color: rgba(196,154,74,.40); }
    .agent-score-row { display: flex; align-items: center; gap: 8px; margin: 8px 0 5px; }
    .agent-score-track { flex: 1; height: 5px; border-radius: var(--radius-pill); background: var(--navy); overflow: hidden; }
    .agent-score-fill { height: 100%; border-radius: var(--radius-pill); background: linear-gradient(90deg, #3ec6ff, #8b5cf6); }
    .agent-thesis { font-size: 12.5px; line-height: 1.7; margin: 6px 0; }
    .agent-action { font-size: 12px; color: #a5b4fc; margin: 3px 0 6px; }
    .agent-risks { margin: 4px 0 8px; padding: 0; list-style: none; display: grid; gap: 3px; }
    .agent-risks li { font-size: 12px; color: var(--text-muted); line-height: 1.6; padding-left: 13px; position: relative; }
    .agent-risks li::before { content: "!"; position: absolute; left: 0; top: 0; color: #c49a4a; font-weight: 700; }
    .agent-card-foot { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
    .agent-source { font-size: 11px; color: var(--text-muted); margin: 0 0 6px; }
    details.agent-detail { margin-top: 8px; border-top: 1px solid rgba(148,163,184,.12); padding-top: 8px; }
    details.agent-detail summary { cursor: pointer; font-size: 12px; color: var(--text-muted); padding: 2px 0; }
    details.agent-detail summary:hover { color: var(--accent); }
    .agent-analyst { margin-top: 6px; padding: 8px 10px; border-radius: var(--radius-sm); background: rgba(255,255,255,.03); font-size: 12px; }
    .agent-analyst .mono { color: var(--text-muted); }
    .agent-debate { display: grid; gap: 5px; margin-top: 6px; font-size: 12px; }
    .agent-debate b { color: #e05a5a; }
    .agent-debate .bear b { color: #3da678; }
    .agent-recent { margin-top: 14px; border-top: 1px solid var(--line-soft); padding-top: 10px; }
    .agent-recent-label { font-size: 11px; color: var(--text-muted); margin-bottom: 6px; letter-spacing: .06em; }
    .agent-recent-row {
      display: inline-flex; gap: 8px; align-items: center;
      margin: 3px 8px 3px 0; padding: 5px 12px;
      border: 1px solid rgba(139,92,246,.20); border-radius: var(--radius-pill);
      font-size: 12px; cursor: pointer; transition: var(--t);
    }
    .agent-recent-row:hover { border-color: rgba(139,92,246,.48); background: rgba(139,92,246,.08); }
    .agent-empty { padding: 30px 0; text-align: center; color: var(--text-muted); font-size: 13px; }
</style>"""

html = html.replace(old_style, NEW)

with open("p11_agents.html", "w", encoding="utf-8") as f:
    f.write(html)

print("p11 style fully replaced OK")