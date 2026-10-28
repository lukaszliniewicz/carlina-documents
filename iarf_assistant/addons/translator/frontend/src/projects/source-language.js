const LANGUAGE_TAG_PATTERN = /^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*$/u;

function normaliseLanguage(value) {
  const text = String(value ?? "").trim();
  if (!text || text.toLowerCase() === "auto-detect" || !LANGUAGE_TAG_PATTERN.test(text)) {
    return "";
  }
  return text
    .split(/[-_]/u)
    .map((part, index) => {
      if (index === 0) return part.toLowerCase();
      if (part.length === 2 && /^[A-Za-z]+$/u.test(part)) return part.toUpperCase();
      if (part.length === 4 && /^[A-Za-z]+$/u.test(part)) return `${part[0].toUpperCase()}${part.slice(1).toLowerCase()}`;
      return part;
    })
    .join("-");
}

export function sourceLanguageForDocument(source) {
  const candidates = [source?.language, source?.metadata?.language];
  for (const candidate of candidates) {
    const language = normaliseLanguage(candidate);
    if (language) return language;
  }
  return "Auto-detect";
}

export function defaultSourceLanguage(source, currentLanguage = "Auto-detect") {
  const current = String(currentLanguage ?? "").trim();
  if (current && current !== "Auto-detect") return current;
  return sourceLanguageForDocument(source);
}
