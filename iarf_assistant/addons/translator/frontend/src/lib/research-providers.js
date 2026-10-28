/**
 * Keep the research-provider catalog deliberately small and defensive. The
 * catalog is supplied by installed add-ons, so an old project may reference
 * an add-on that is no longer available on this host.
 */
export function normaliseResearchProviders(payload) {
  const rows = Array.isArray(payload) ? payload : (Array.isArray(payload?.providers) ? payload.providers : []);
  return rows.map((provider) => {
    const id = String(provider?.id || provider?.addon_id || provider?.slug || "").trim();
    if (!id) return null;
    const tools = Array.isArray(provider?.tools)
      ? provider.tools.map((tool) => ({
          id: String(tool?.id || tool?.name || "").trim(),
          name: String(tool?.name || tool?.id || "Research tool").trim(),
          description: String(tool?.description || "").trim()
        })).filter((tool) => tool.id)
      : [];
    return {
      id,
      name: String(provider?.name || id).trim(),
      description: String(provider?.description || "").trim(),
      tools
    };
  }).filter(Boolean);
}

export function selectedResearchProviderIds(value) {
  const ids = Array.isArray(value) ? value : [];
  return [...new Set(ids.map((id) => String(id || "").trim()).filter(Boolean))];
}

/**
 * Return catalog rows plus synthetic rows for saved IDs that are currently
 * unavailable. Synthetic rows make the lost configuration explicit instead
 * of silently dropping it from a settings form.
 */
export function researchProviderOptions(providers, selectedIds) {
  const catalog = normaliseResearchProviders(providers);
  const selected = selectedResearchProviderIds(selectedIds);
  const known = new Set(catalog.map((provider) => provider.id));
  const unavailable = selected
    .filter((id) => !known.has(id))
    .map((id) => ({
      id,
      name: id,
      description: "This provider is saved on the project but is not available. Activate the add-on to use it.",
      tools: [],
      unavailable: true
    }));
  return [...catalog, ...unavailable];
}

const BLOCK_RESEARCH_SELECTION_FIELDS = [
  "tool_id",
  "provider_id",
  "provider_domain",
  "addon_id",
  "connection_mode",
  "connection_id"
];

function nonEmptyText(value) {
  const text = String(value ?? "").trim();
  return text || "";
}

function exactSelection(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const selection = {};
  for (const field of BLOCK_RESEARCH_SELECTION_FIELDS) {
    if (typeof value[field] !== "string") return null;
    const text = value[field].trim();
    if (text !== value[field]) return null;
    if (!text && field !== "connection_id") return null;
    selection[field] = text;
  }
  if (!new Set(["saved", "connectionless"]).has(selection.connection_mode)) return null;
  if (selection.connection_mode === "connectionless" && selection.connection_id) return null;
  if (selection.connection_mode === "saved" && !selection.connection_id) return null;
  return selection;
}

/**
 * Canonical identity for a document-block research choice.  The complete
 * tuple is intentional: a tool name or add-on ID alone is not an executable
 * permission and must never be used to infer a saved account.
 */
export function researchToolSelectionKey(value) {
  const selection = exactSelection(value);
  return selection ? BLOCK_RESEARCH_SELECTION_FIELDS.map((field) => selection[field]).join("\u001f") : "";
}

export function selectedResearchToolSelections(value) {
  const selections = Array.isArray(value) ? value : [];
  const seen = new Set();
  const result = [];
  for (const candidate of selections) {
    const selection = exactSelection(candidate);
    const key = researchToolSelectionKey(selection);
    if (!selection || !key || seen.has(key)) continue;
    seen.add(key);
    result.push(selection);
  }
  return result;
}

function normaliseConnection(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const id = nonEmptyText(value.id || value.connection_id);
  if (!id) return null;
  return {
    id,
    label: nonEmptyText(value.label || value.name || id)
  };
}

/**
 * Normalise the exact block catalog without collapsing tool/provider/
 * connection identity.  Rows are source-free metadata from the server; they
 * are not permission grants and unavailable rows remain visible.
 */
export function normaliseBlockResearchTools(payload) {
  const rows = Array.isArray(payload) ? payload : [];
  return rows.map((row) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) return null;
    const toolId = nonEmptyText(row.tool_id);
    const providerId = nonEmptyText(row.provider_id);
    const providerDomain = nonEmptyText(row.provider_domain);
    const addonId = nonEmptyText(row.addon_id);
    if (!toolId || !providerId || !providerDomain || !addonId) return null;
    const modes = Array.isArray(row.connection_modes)
      ? row.connection_modes.map(nonEmptyText).filter(Boolean)
      : [];
    const connections = Array.isArray(row.connections)
      ? row.connections.map(normaliseConnection).filter(Boolean)
      : [];
    return {
      tool_id: toolId,
      tool_name: nonEmptyText(row.tool_name || row.tool_id),
      provider_id: providerId,
      provider_name: nonEmptyText(row.provider_name || row.provider_id),
      provider_domain: providerDomain,
      addon_id: addonId,
      connection_modes: [...new Set(modes)],
      connections,
      executable: Boolean(row.executable),
      private_data_capable: Boolean(row.private_data_capable),
      authority_warning: nonEmptyText(row.authority_warning),
      unavailable_reason: nonEmptyText(row.unavailable_reason),
      required_connector_definition_id: nonEmptyText(row.required_connector_definition_id),
      research_section: new Set(["web", "library", "advanced"]).has(row.research_section) ? row.research_section : "",
      research_role: nonEmptyText(row.research_role),
      research_recommended: Boolean(row.research_recommended),
      research_priority: Number.isInteger(row.research_priority) ? row.research_priority : 0
    };
  }).filter(Boolean);
}

function selectionForRow(row, connectionMode, connectionId = "") {
  return {
    tool_id: row.tool_id,
    provider_id: row.provider_id,
    provider_domain: row.provider_domain,
    addon_id: row.addon_id,
    connection_mode: connectionMode,
    connection_id: connectionId
  };
}

/**
 * Flatten the exact catalog into checkbox-ready choices.  A saved row with
 * no visible ready connection is still rendered as unavailable; a saved
 * selection from an older project is appended using only its stored identity.
 */
export function blockResearchToolOptions(catalog, selectedValue) {
  const rows = normaliseBlockResearchTools(catalog);
  const selected = selectedResearchToolSelections(selectedValue);
  const selectedKeys = new Set(selected.map(researchToolSelectionKey));
  const selectedToolIds = new Set(selected.map((selection) => selection.tool_id));
  const options = [];

  for (const row of rows) {
    const base = {
      ...row,
      selected: false,
      unavailable: !row.executable
    };
    if (row.connection_modes.includes("connectionless")) {
      const selection = selectionForRow(row, "connectionless");
      options.push({
        ...base,
        connection_mode: "connectionless",
        connection_id: "",
        connection_label: "No saved connection needed",
        selection,
        selected: selectedKeys.has(researchToolSelectionKey(selection)),
        selectionConflict: selectedToolIds.has(row.tool_id) && !selectedKeys.has(researchToolSelectionKey(selection)),
        unavailable: !row.executable
      });
    }
    if (row.connection_modes.includes("saved")) {
      for (const connection of row.connections) {
        const selection = selectionForRow(row, "saved", connection.id);
        options.push({
          ...base,
          connection_mode: "saved",
          connection_id: connection.id,
          connection_label: connection.label,
          selection,
          selected: selectedKeys.has(researchToolSelectionKey(selection)),
          selectionConflict: selectedToolIds.has(row.tool_id) && !selectedKeys.has(researchToolSelectionKey(selection)),
          unavailable: !row.executable
        });
      }
    }
    if (!options.some((option) => option.tool_id === row.tool_id && option.provider_id === row.provider_id)) {
      options.push({
        ...base,
        connection_mode: row.connection_modes.includes("saved") ? "saved" : "connectionless",
        connection_id: "",
        connection_label: row.unavailable_reason || "No executable connection available",
        selection: null,
        selected: false,
        selectionConflict: false,
        unavailable: true
      });
    }
  }

  const knownKeys = new Set(options.filter((option) => option.selection).map((option) => researchToolSelectionKey(option.selection)));
  for (const selection of selected) {
    const key = researchToolSelectionKey(selection);
    if (knownKeys.has(key)) continue;
    options.push({
      tool_id: selection.tool_id,
      tool_name: selection.tool_id,
      provider_id: selection.provider_id,
      provider_name: selection.provider_id,
      provider_domain: selection.provider_domain,
      addon_id: selection.addon_id,
      connection_modes: [selection.connection_mode],
      connections: [],
      executable: false,
      private_data_capable: false,
      authority_warning: "This saved research selection is no longer available on this host.",
      unavailable_reason: "Saved tool/provider/connection is unavailable",
      required_connector_definition_id: "",
      connection_mode: selection.connection_mode,
      connection_id: selection.connection_id,
      connection_label: "Unavailable saved selection",
      selection,
      selected: true,
      selectionConflict: false,
      unavailable: true
    });
  }
  return options;
}

export function researchToolSection(option) {
  if (new Set(["web", "library", "advanced"]).has(option?.research_section)) {
    return option.research_section;
  }
  const addonId = nonEmptyText(option?.addon_id).toLowerCase();
  const providerIdentity = [
    addonId,
    nonEmptyText(option?.provider_id),
    nonEmptyText(option?.provider_name),
    nonEmptyText(option?.provider_domain)
  ].join(" ").toLowerCase();
  if (/\b(knowledge|collection|library|retrieval|vector|rag)\b/.test(providerIdentity)) return "library";
  if (["web_search", "scraping"].includes(addonId)
    || /\b(web_search|scraping|jina|patchright|web)\b/.test(providerIdentity)) return "web";
  return "advanced";
}

export function groupBlockResearchToolOptions(options) {
  const groups = { web: [], library: [], advanced: [] };
  for (const option of Array.isArray(options) ? options : []) {
    groups[researchToolSection(option)].push(option);
  }
  return groups;
}

/**
 * Choose the smallest useful, visible default for opt-in web research:
 * one Jina-first general search and one Jina Reader page fetch.  The result is
 * only written into the visible form; it grants no authority until the owner
 * saves settings and starts an operation.
 */
export function recommendedResearchToolSelections(options) {
  const available = (Array.isArray(options) ? options : [])
    .filter((option) => option?.selection && option.executable && !option.unavailable && !option.selectionConflict);
  const roles = ["general_search", "page_reader"];
  const preferred = roles.map((role) => available
    .filter((option) => option.research_recommended && option.research_role === role)
    .sort((left, right) => left.research_priority - right.research_priority)[0]
  ).filter(Boolean);
  return selectedResearchToolSelections(preferred.map((option) => option.selection));
}

export function executableResearchToolSelections(catalog, selectedValue) {
  const options = blockResearchToolOptions(catalog, selectedValue);
  const selected = selectedResearchToolSelections(selectedValue);
  const executableKeys = new Set(
    options.filter((option) => option.selected && option.executable && option.selection)
      .map((option) => researchToolSelectionKey(option.selection))
  );
  return selected.filter((selection) => executableKeys.has(researchToolSelectionKey(selection)));
}

export function allSelectedResearchToolsExecutable(catalog, selectedValue) {
  const selected = selectedResearchToolSelections(selectedValue);
  return selected.length > 0
    && new Set(selected.map((selection) => selection.tool_id)).size === selected.length
    && executableResearchToolSelections(catalog, selected).length === selected.length;
}
