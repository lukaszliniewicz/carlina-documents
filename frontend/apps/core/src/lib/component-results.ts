function trimmedString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function composerInsertionFromComponentResult(result: unknown): string {
  const direct = trimmedString(result);
  if (direct) return direct;

  if (result && typeof result === "object" && !Array.isArray(result)) {
    const record = result as Record<string, unknown>;
    const insertion = trimmedString(record.insert_text);
    if (insertion) return insertion;

    if (record.resource_type === "local_path") {
      return trimmedString(record.path);
    }
  }

  if (Array.isArray(result)) {
    return result
      .flatMap((item) => {
        if (!item || typeof item !== "object") return [];
        const email = trimmedString((item as Record<string, unknown>).email);
        return email ? [email] : [];
      })
      .join(", ");
  }

  return "";
}
