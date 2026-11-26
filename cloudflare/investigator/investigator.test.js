import assert from "node:assert/strict";
import test from "node:test";
import { investigate, instructions } from "./investigator.js";

test("the Agent follows a new model-selected lead and delegates that exact decision", async () => {
  const decision = { action: "search", query: "North Quay dispatch receipt driver field" };
  const calls = [];
  await investigate("decide", async (op, args) => {
    calls.push([op, args]);
    return op === "model" ? decision : {};
  });
  assert.deepEqual(calls, [["model", { instructions }], ["act", { decision }]]);
});

test("an exhibit cannot introduce a new tool or endpoint", async () => {
  const calls = [];
  await assert.rejects(investigate("decide", async op => {
    calls.push(op);
    return { action: "https://foreign.example/upload" };
  }));
  assert.deepEqual(calls, ["model"]);
});

test("a population review resumes its saved batch", async () => {
  const calls = [];
  await investigate("review", async (op, args) => calls.push([op, args]));
  assert.deepEqual(calls, [["review", {}]]);
});

test("literal review returns exact Unicode offsets on eligible spans, excluding partial words and cut edges", async () => {
  const text = "🚚 sam SAMs SAM sam Sam. sam!";
  const calls = [];
  await investigate("review", async (op, value) => {
    calls.push([op, value]);
    return {
      batch: "batch", literal: "sam",
      records: [
        {text, start: 0, length: Array.from(text).length, ranges: [[2, 5], [12, 28]]},
        {text: "Sam sam", start: 40, length: 100, ranges: [[0, 100]]},
      ],
    };
  });
  assert.deepEqual(calls[1], ["literal_result", { batch: "batch", offsets: [[2, 15, 19, 24], []] }]);
});

test("literal metacharacters remain plain text and dotted i preserves saved case matching", async () => {
  let result;
  await investigate("review", async (op, value) => {
    if (op === "review") return {
      batch: "v", literal: "i.%", records: [{text: "🚩 İ.% iZ% ı.%", start: 0, length: 13, ranges: [[0, 13]]}],
    };
    result = value;
  });
  assert.deepEqual(result.offsets, [[2, 10]]);
});
