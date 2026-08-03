import os, re, sys

os.chdir(r"C:\Users\xuan\Desktop\桌面\股票\workbench\ui_mockups\v2")

with open("p11_agents.html", "r", encoding="utf-8") as f:
    html = f.read()

# single-fields
old_single = """        <div id="single-fields" class="agent-config">
          <label class="agent-field">股票<input id="agent-ts-code" class="field field-md" type="text" placeholder="如 000001.SZ" list="agent-stock-options" autocomplete="off"></label>
          <datalist id="agent-stock-options"></datalist>
          <label class="agent-field"><input id="agent-force-single" type="checkbox" class="chk" style="width:auto"> 强制重跑</label>
          <button id="agent-single-run" class="button primary" type="button" style="min-height:30px;padding:0 16px;font-size:12px">开始个股研判</button>
        </div>"""

new_single = """        <div id="single-fields" class="agent-config">
          <label class="agent-field"><span class="field-label">股票代码</span><input id="agent-ts-code" class="field field-md" type="text" placeholder="如 000001.SZ" list="agent-stock-options" autocomplete="off"></label>
          <datalist id="agent-stock-options"></datalist>
          <label class="agent-field" style="justify-self:end"><input id="agent-force-single" type="checkbox" class="chk" style="width:auto"> 强制重跑</label>
          <div class="config-actions">
            <button id="agent-single-run" class="button primary" type="button" style="min-height:30px;padding:0 20px;font-size:12px">开始个股研判</button>
          </div>
        </div>"""

html = html.replace(old_single, new_single)

# flow-fields
old_flow = """        <div id="flow-fields" class="agent-config" hidden>
          <label class="agent-field">候选数量<input id="agent-candidates" class="field field-sm" type="number" min="1" max="200" step="1"></label>
          <label class="agent-field">深度学习<input id="agent-depth" class="field field-sm" type="number" min="1" max="30" step="1"></label>
          <label class="agent-field">最终输出<input id="agent-final" class="field field-sm" type="number" min="1" max="10" step="1"></label>
          <label class="agent-field"><input id="agent-force-flow" type="checkbox" class="chk" style="width:auto"> 强制重跑</label>
          <button id="agent-flow-run" class="button primary" type="button" style="min-height:30px;padding:0 16px;font-size:12px">开始选股流程</button>
        </div>"""

new_flow = """        <div id="flow-fields" class="agent-config" hidden>
          <label class="agent-field"><span class="field-label">候选数量 <span class="hint">初始筛选</span></span><input id="agent-candidates" class="field field-sm" type="number" min="1" max="200" step="1"></label>
          <label class="agent-field"><span class="field-label">深度学习 <span class="hint">每批次</span></span><input id="agent-depth" class="field field-sm" type="number" min="1" max="30" step="1"></label>
          <label class="agent-field"><span class="field-label">最终输出 <span class="hint">最优 N 只</span></span><input id="agent-final" class="field field-sm" type="number" min="1" max="10" step="1"></label>
          <label class="agent-field" style="justify-self:end"><input id="agent-force-flow" type="checkbox" class="chk" style="width:auto"> 强制重跑</label>
          <div class="config-actions">
            <button id="agent-flow-run" class="button primary" type="button" style="min-height:30px;padding:0 20px;font-size:12px">开始选股流程</button>
          </div>
        </div>"""

html = html.replace(old_flow, new_flow)

with open("p11_agents.html", "w", encoding="utf-8") as f:
    f.write(html)

print("p11 html structure updated")