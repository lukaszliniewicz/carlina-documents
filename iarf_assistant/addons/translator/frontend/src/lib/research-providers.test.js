import { describe, expect, it } from "vitest";
import {
  allSelectedResearchToolsExecutable,
  blockResearchToolOptions,
  groupBlockResearchToolOptions,
  normaliseBlockResearchTools,
  normaliseResearchProviders,
  recommendedResearchToolSelections,
  researchProviderOptions,
  researchToolSection,
  researchToolSelectionKey,
  selectedResearchProviderIds,
  selectedResearchToolSelections
} from "./research-providers.js";

describe("research provider helpers", () => {
  it("normalises catalog providers and tool summaries", () => {
    expect(normaliseResearchProviders({ providers: [{ addon_id: "web", name: "Web", tools: [{ id: "search", description: "Search" }] }] })).toEqual([
      { id: "web", name: "Web", description: "", tools: [{ id: "search", name: "search", description: "Search" }] }
    ]);
  });

  it("keeps saved providers visible when the catalog no longer contains them", () => {
    const options = researchProviderOptions([{ id: "web", name: "Web" }], ["web", "old-addon", "old-addon"]);
    expect(options.map((provider) => provider.id)).toEqual(["web", "old-addon"]);
    expect(options[1].unavailable).toBe(true);
    expect(selectedResearchProviderIds(["web", "web", ""])).toEqual(["web"]);
  });

  it("keeps document-block research identity exact through tool and saved connection", () => {
    const catalog = normaliseBlockResearchTools([
      {
        tool_id: "web.search",
        tool_name: "Search",
        provider_id: "serp",
        provider_name: "Search Provider",
        provider_domain: "web",
        addon_id: "research-addon",
        connection_modes: ["saved"],
        connections: [{ id: "conn-1", label: "Research account" }],
        executable: true,
        private_data_capable: true
      }
    ]);
    const selection = {
      tool_id: "web.search",
      provider_id: "serp",
      provider_domain: "web",
      addon_id: "research-addon",
      connection_mode: "saved",
      connection_id: "conn-1"
    };
    const options = blockResearchToolOptions(catalog, [selection]);
    expect(options).toHaveLength(1);
    expect(options[0].selected).toBe(true);
    expect(options[0].selection).toEqual(selection);
    expect(allSelectedResearchToolsExecutable(catalog, [selection])).toBe(true);
    expect(researchToolSelectionKey(selection)).toContain("conn-1");
    expect(selectedResearchToolSelections([selection, selection])).toEqual([selection]);
  });

  it("does not infer an unavailable saved connection or silently substitute it", () => {
    const selection = {
      tool_id: "web.search",
      provider_id: "serp",
      provider_domain: "web",
      addon_id: "research-addon",
      connection_mode: "saved",
      connection_id: "old-account"
    };
    const options = blockResearchToolOptions([], [selection]);
    expect(options).toHaveLength(1);
    expect(options[0].selected).toBe(true);
    expect(options[0].executable).toBe(false);
    expect(options[0].connection_id).toBe("old-account");
    expect(allSelectedResearchToolsExecutable([], [selection])).toBe(false);
  });

  it("exposes connectionless choices without inventing a connection id", () => {
    const catalog = normaliseBlockResearchTools([{
      tool_id: "web.search",
      provider_id: "serp",
      provider_domain: "web",
      addon_id: "research-addon",
      connection_modes: ["connectionless"],
      connections: [],
      executable: true
    }]);
    const option = blockResearchToolOptions(catalog, [])[0];
    expect(option.selection.connection_mode).toBe("connectionless");
    expect(option.selection.connection_id).toBe("");
    expect(option.connection_label).toBe("No saved connection needed");
  });

  it("groups common web and knowledge tools behind readable settings sections", () => {
    const rows = [
      { addon_id: "web_search", tool_id: "text", tool_name: "Search", provider_id: "search", provider_name: "Web Search", provider_domain: "tools" },
      { addon_id: "knowledge", tool_id: "retrieve", tool_name: "Search collection", provider_id: "local", provider_name: "Knowledge", provider_domain: "retrieval" },
      { addon_id: "git_manager", tool_id: "read", tool_name: "Read repository", provider_id: "git", provider_name: "Git", provider_domain: "tools" },
      { addon_id: "googledrive", tool_id: "googledrive.search_files", tool_name: "Search files", provider_id: "drive", provider_name: "Google Drive", provider_domain: "google_drive" },
      { addon_id: "accounting", tool_id: "accounting.search", tool_name: "Search records", provider_id: "accounting", provider_name: "Accounting CRM", provider_domain: "wordpress_extension" }
    ];
    expect(researchToolSection(rows[0])).toBe("web");
    expect(researchToolSection(rows[1])).toBe("library");
    expect(researchToolSection(rows[2])).toBe("advanced");
    expect(researchToolSection(rows[3])).toBe("advanced");
    expect(researchToolSection(rows[4])).toBe("advanced");
    expect(groupBlockResearchToolOptions(rows)).toEqual({
      web: [rows[0]],
      library: [rows[1]],
      advanced: [rows[2], rows[3], rows[4]]
    });
  });

  it("recommends one Jina-first search and one Jina Reader page tool", () => {
    const catalog = [
      { tool_id: "web_search.text", tool_name: "Text Search", provider_id: "jina", provider_name: "Jina first", provider_domain: "web_search", addon_id: "web_search", connection_modes: ["connectionless"], connections: [], executable: true, research_section: "web", research_role: "general_search", research_recommended: true, research_priority: 10 },
      { tool_id: "scraping.get_web_content", tool_name: "Get Web Content", provider_id: "jina", provider_name: "Jina Reader", provider_domain: "scraping", addon_id: "scraping", connection_modes: ["connectionless"], connections: [], executable: true, research_section: "web", research_role: "page_reader", research_recommended: true, research_priority: 20 },
      { tool_id: "scraping.crawl_website", tool_name: "Crawl Website", provider_id: "jina", provider_name: "Jina Reader", provider_domain: "scraping", addon_id: "scraping", connection_modes: ["connectionless"], connections: [], executable: true, research_section: "web", research_role: "page_reader", research_recommended: true, research_priority: 30 },
      { tool_id: "googledrive.search", tool_name: "Search", provider_id: "drive", provider_name: "Drive", provider_domain: "google_drive", addon_id: "googledrive", connection_modes: ["connectionless"], connections: [], executable: true }
    ];
    const options = blockResearchToolOptions(catalog, []);

    expect(recommendedResearchToolSelections(options).map((selection) => selection.tool_id)).toEqual([
      "web_search.text",
      "scraping.get_web_content"
    ]);
  });
});
