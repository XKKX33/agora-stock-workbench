import test from "node:test";
import assert from "node:assert/strict";
import { jsonResponseFormat, roleSystemPrompt } from "../src/provider.js";

test("Pi provider requests JSON object output with role schema", () => {
  assert.deepEqual(jsonResponseFormat(), { type: "json_object" });
  assert.match(roleSystemPrompt("coarse"), /selected/);
  assert.match(roleSystemPrompt("methodology"), /stance/);
  assert.match(roleSystemPrompt("debate"), /risk_control/);
});
