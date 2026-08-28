import test from "node:test";
import assert from "node:assert/strict";
import { jsonResponseFormat, roleSystemPrompt } from "../src/provider.js";

test("Pi provider requests JSON object output with role schema", () => {
  assert.deepEqual(jsonResponseFormat(), { type: "json_object" });
  assert.match(roleSystemPrompt("methodology"), /stance/);
  // coarse 与 debate 两个排序角色都已删除：规则方法论排出 Top20，20 只全部参辩。
  assert.throws(() => roleSystemPrompt("coarse"), /unknown role/);
  assert.throws(() => roleSystemPrompt("debate"), /unknown role/);
});

test("risk chair prompt asks the model for the risk_control decision itself", () => {
  const prompt = roleSystemPrompt("risk_chair");
  assert.match(prompt, /risk_control/);
  // 只能看多或看空：中性等于没给答案，而这份名单要拿去和规则组比收益。
  assert.match(prompt, /"verdict":"看多\|看空"/);
  assert.match(prompt, /中性 is not allowed/);
  // score 决定谁进前三，提示词必须说清这件事，否则模型会全部打同一个分。
  assert.match(prompt, /decides which stocks make the final list/);
  assert.doesNotMatch(prompt, /handled by the system/);
});

test("debater prompts ask for an argument with citations", () => {
  for (const role of ["bull", "bear", "bull_counter"]) {
    const prompt = roleSystemPrompt(role);
    assert.match(prompt, /"argument":"string"/);
    assert.match(prompt, /citations/);
    assert.match(prompt, /Never invent facts/);
  }
});

test("methodology brief is prepended to the schema contract when supplied", () => {
  const brief = "情绪周期七阶段：启动/发酵/高潮/分歧/退潮/冰点/修复。";
  const withBrief = roleSystemPrompt("bull", brief);
  assert.ok(withBrief.startsWith(brief));
  assert.match(withBrief, /"argument":"string"/);
  assert.doesNotMatch(roleSystemPrompt("bull"), /情绪周期/);
  assert.equal(roleSystemPrompt("bull", "   "), roleSystemPrompt("bull"));
});
