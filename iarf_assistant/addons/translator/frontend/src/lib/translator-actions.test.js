import { describe, expect, it } from "vitest";
import { buildTranslationRequest } from "./translator-actions.js";

describe("translator segment actions", () => {
  it("sends the visible segment version and idempotency key", () => {
    const request = buildTranslationRequest({ id: 7, version: "12" }, "translator.translate.7.key");
    expect(request.headers).toMatchObject({ "Content-Type": "application/json", "Idempotency-Key": "translator.translate.7.key" });
    expect(JSON.parse(request.body)).toEqual({ expected_version: 12 });
  });

  it("rejects translation when the segment version is unavailable", () => {
    expect(() => buildTranslationRequest({ id: 7 }, "key")).toThrow(/current version/i);
  });
});
