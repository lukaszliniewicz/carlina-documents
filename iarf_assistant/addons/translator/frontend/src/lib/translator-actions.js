export function buildTranslationRequest(segment, idempotencyKey) {
  const version = Number(segment?.version);
  if (!Number.isInteger(version)) {
    throw new Error("This segment has no current version. Refresh the workspace before translating it.");
  }
  return {
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey
    },
    body: JSON.stringify({ expected_version: version })
  };
}
