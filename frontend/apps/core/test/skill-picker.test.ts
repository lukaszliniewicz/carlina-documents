import assert from "node:assert/strict";
import test from "node:test";

import { matchingSkills } from "../src/lib/skill-picker.ts";

const skills = [
  { id: "research.web", name: "Web research", description: "Primary sources", source: "research" },
  { id: "writer.brief", name: "Brief writer", description: "Draft concise briefs", source: "writer" },
];

test("matches the /sk picker against names, descriptions, ids, and source", () => {
  assert.deepEqual(matchingSkills(skills, "primary").map((skill) => skill.id), ["research.web"]);
  assert.deepEqual(matchingSkills(skills, "").map((skill) => skill.id), ["research.web", "writer.brief"]);
  assert.deepEqual(matchingSkills(skills, "missing"), []);
});
