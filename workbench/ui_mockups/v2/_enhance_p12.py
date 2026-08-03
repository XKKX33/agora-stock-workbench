import os, re

os.chdir(r"C:\Users\xuan\Desktop\桌面\股票\workbench\ui_mockups\v2")

with open("p12_settings.html", "r", encoding="utf-8") as f:
    html = f.read()

# 1. Enhance style block - make settings-block glassier
html = html.replace(
    ".settings-block { padding: 18px 20px; border-radius: var(--radius); border: 1px solid rgba(139,92,246,.18); background: linear-gradient(160deg, rgba(62,198,255,.04), rgba(139,92,246,.07)); backdrop-filter: blur(4px); }",
    ".settings-block { padding: 22px 24px; border-radius: var(--radius); border: 1px solid rgba(139,92,246,.20); background: linear-gradient(160deg, rgba(62,198,255,.05), rgba(139,92,246,.09)); backdrop-filter: blur(8px); transition: var(--t); }\n    .settings-block:hover { border-color: rgba(139,92,246,.35); box-shadow: 0 0 0 1px rgba(139,92,246,.08), 0 0 32px -16px rgba(124,58,237,.25); }"
)

# 2. Make field-row have padding and hover
html = html.replace(
    ".field-row { display: grid; gap: 4px; font-size: 11px; color: var(--text-muted); }",
    ".field-row { display: grid; gap: 4px; font-size: 11px; color: var(--text-muted); padding: 8px 10px; border-radius: var(--radius-sm); background: rgba(255,255,255,.02); border: 1px solid transparent; transition: var(--t); }\n    .field-row:hover { border-color: rgba(139,92,246,.15); background: rgba(255,255,255,.04); }"
)

# 3. Add focus style for inputs
html = html.replace(
    ".key-chip { margin-top: 6px; }",
    ".key-chip { margin-top: 6px; }\n    .field-row input.field:focus, .field-row select.field:focus { border-color: rgba(139,92,246,.5); box-shadow: 0 0 0 2px rgba(139,92,246,.15); }"
)

# 4. Enhance save-bar with sticky
html = html.replace(
    ".save-bar {",
    ".save-bar { position: sticky; bottom: 16px; box-shadow: 0 -8px 24px rgba(0,0,0,.3);"
)

# 5. Add section divider style before </style>
html = html.replace(
    "</style>",
    """
    .section-divider {
      display: flex; align-items: center; gap: 12px;
      margin: 16px 0 12px; font-size: 11px; color: var(--text-muted);
      font-weight: 500; letter-spacing: .06em;
    }
    .section-divider::after {
      content: ''; flex: 1; height: 1px;
      background: linear-gradient(90deg, rgba(139,92,246,.3), transparent);
    }
    .settings-block .block-desc { margin: 0 0 14px; font-size: 12px; color: var(--text-muted); line-height: 1.7; }
    .settings-block h2 .ico { font-size: 16px; opacity: .85; }
</style>"""
)

# 6. Enhance body: add section dividers and icons to settings blocks
# Replace the first settings block header
html = html.replace(
    '<div><h2>AI / Agent 接口</h2></div>',
    '<h2><span class="ico">🤖</span> AI / Agent 接口</h2>'
)
html = html.replace(
    '<p>适用于多 agent 研判与 AI 复盘。provider 目前仅支持 <code>openai_compatible</code>。</p>',
    '<p class="block-desc">适用于多 agent 研判与 AI 复盘。provider 目前仅支持 <code>openai_compatible</code>。</p>'
)

# Replace second settings block header
html = html.replace(
    '<div><h2>研判默认参数</h2></div>',
    '<h2><span class="ico">📊</span> 研判默认参数</h2>'
)
html = html.replace(
    '<p>选股流程面板的默认值与硬上限。前端仍可在 AI Agent 页面临时修改，这里写默认。</p>',
    '<p class="block-desc">选股流程面板的默认值与硬上限。前端仍可在 AI Agent 页面临时修改，这里设置默认值。</p>'
)

# Add section dividers
# After the first block desc, before grid-form
html = html.replace(
    '<p class="block-desc">适用于多 agent 研判与 AI 复盘。provider 目前仅支持 <code>openai_compatible</code>。</p>\n          <div class="grid-form" id="agent-form">',
    '<p class="block-desc">适用于多 agent 研判与 AI 复盘。provider 目前仅支持 <code>openai_compatible</code>。</p>\n          <div class="section-divider">连接配置</div>\n          <div class="grid-form" id="agent-form">'
)

# Add section divider before the max params
html = html.replace(
    '<p class="block-desc">选股流程面板的默认值与硬上限。前端仍可在 AI Agent 页面临时修改，这里设置默认值。</p>\n          <div class="grid-form">',
    '<p class="block-desc">选股流程面板的默认值与硬上限。前端仍可在 AI Agent 页面临时修改，这里设置默认值。</p>\n          <div class="section-divider">默认值（首次加载时生效）</div>\n          <div class="grid-form">'
)

# Add section divider before max_* fields in the second block
html = html.replace(
    '</label>\n            <label class="field-row"><span class="label">max_candidates',
    '</label>\n          </div>\n          <div class="section-divider">硬上限（前端不能超过）</div>\n          <div class="grid-form">\n            <label class="field-row"><span class="label">max_candidates'
)

# 7. Add placeholders to inputs
html = html.replace(
    '<p class="block-desc">选股流程面板的默认值与硬上限。前端仍可在 AI Agent 页面临时修改，这里设置默认值。</p>',
    '<p class="block-desc">选股流程面板的默认值与硬上限。前端仍可在 AI Agent 页面临时修改，这里设置默认值，保存后下次刷新生效。</p>'
)

# Add hint labels
html = html.replace(
    '<span class="label">启用<muted></muted></span>',
    '<span class="label">启用</span>'
)
html = html.replace(
    '<span class="label">provider</span>',
    '<span class="label">provider</span>'
)
html = html.replace(
    '<span class="label">model</span>',
    '<span class="label">model <span class="hint">模型名</span></span>'
)
html = html.replace(
    '<span class="label">temperature</span>',
    '<span class="label">temperature <span class="hint">0~2</span></span>'
)
html = html.replace(
    '<span class="label">max_tokens</span>',
    '<span class="label">max_tokens <span class="hint">100~32000</span></span>'
)

with open("p12_settings.html", "w", encoding="utf-8") as f:
    f.write(html)

print("p12 enhanced OK")
