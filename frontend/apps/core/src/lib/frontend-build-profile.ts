import { lstatSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const descriptorKeys = [
  "format", "schema_version", "application_profile", "source_revision",
  "source_date_epoch", "addon_ids",
];
const documentAddons = ["doc_processor", "sentence_segmenter", "translator"];
const invalidDescriptor = "Application distribution descriptor is malformed.";

// Select build inputs from generated package identity. Runtime admission remains
// the responsibility of the stricter Python distribution parser.
export function frontendBuildProfile(descriptorPath: string): "workbench" | "documents" {
  let status;
  try {
    status = lstatSync(descriptorPath);
  } catch (error: unknown) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") {
      return "workbench";
    }
    throw new Error("Application distribution descriptor could not be read.", { cause: error });
  }
  if (!status.isFile() || status.size > 4096) {
    throw new Error(invalidDescriptor);
  }

  let value: unknown;
  try {
    const payload = readFileSync(descriptorPath);
    if (payload.length > 4096) throw new Error(invalidDescriptor);
    value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(payload));
  } catch {
    throw new Error(invalidDescriptor);
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(invalidDescriptor);
  }
  const descriptor = value as Record<string, unknown>;
  if (
    Object.keys(descriptor).length !== descriptorKeys.length ||
    !descriptorKeys.every((key) => Object.hasOwn(descriptor, key)) ||
    descriptor.format !== "carlina.application-distribution" ||
    descriptor.schema_version !== 1 ||
    descriptor.application_profile !== "documents" ||
    typeof descriptor.source_revision !== "string" ||
    !/^[0-9a-f]{40}$/.test(descriptor.source_revision) ||
    typeof descriptor.source_date_epoch !== "number" ||
    !Number.isSafeInteger(descriptor.source_date_epoch) ||
    descriptor.source_date_epoch <= 0 ||
    !Array.isArray(descriptor.addon_ids) ||
    descriptor.addon_ids.length !== documentAddons.length ||
    !descriptor.addon_ids.every((addonId, index) => addonId === documentAddons[index])
  ) {
    throw new Error(invalidDescriptor);
  }
  return "documents";
}

export function coreRouteAliases(
  descriptorPath = fileURLToPath(
    new URL("../../../../../carlina/distribution.json", import.meta.url),
  ),
): Record<string, string> {
  const profile = frontendBuildProfile(descriptorPath);
  return {
    "@carlina/core-routes": fileURLToPath(
      new URL(`${profile}-route-loaders.ts`, import.meta.url),
    ),
  };
}
