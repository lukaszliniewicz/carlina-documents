import { createIdempotencyKey } from "../lib/api.js";

const CONTRACT = "translator-shared-glossary-v1";

export function createSharedGlossaryClient(client, projectId) {
  const root = "/addons/translator/api/shared-glossaries";
  const projectRoot = `/addons/translator/api/projects/${encodeURIComponent(projectId)}/shared-glossary`;
  async function read(url, signal) {
    const result = await client.fetchJson(url, { signal });
    if (result.contract !== CONTRACT) throw new Error("This shared glossary response is incompatible. Refresh the workspace.");
    return result;
  }
  async function pages(url, field, signal) {
    let offset = 0;
    let first = null;
    const items = [];
    do {
      const page = await read(`${url}${url.includes("?") ? "&" : "?"}offset=${offset}&limit=100`, signal);
      if (first && (page.revision !== first.revision || page.binding_version !== first.binding_version || page.source_plan_id !== first.source_plan_id)) {
        throw new Error("The glossary changed while loading. Refresh to read one consistent version.");
      }
      first ||= page;
      if (!Array.isArray(page[field])) throw new Error("The glossary list could not be read.");
      items.push(...page[field]);
      const next = page.pagination?.next_offset;
      if (next == null) break;
      if (!Number.isInteger(next) || next <= offset || items.length > 10000) throw new Error("The glossary list did not finish loading. Please refresh.");
      offset = next;
    } while (!signal?.aborted);
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    return { ...first, [field]: items };
  }
  async function asset(id, signal) {
    const url = `${root}/${encodeURIComponent(id)}`;
    const result = await pages(url, "items", signal);
    const proposals = [...(result.proposals || [])];
    let next = result.proposal_pagination?.next_offset;
    let previous = 0;
    while (next != null) {
      if (!Number.isInteger(next) || next <= previous || proposals.length > 10000) throw new Error("The contribution list did not finish loading. Please refresh.");
      previous = next;
      const page = await read(`${url}?revision=${result.revision}&limit=1&proposal_offset=${next}&proposal_limit=10`, signal);
      if (!Array.isArray(page.proposals)) throw new Error("The contribution list could not be read.");
      proposals.push(...page.proposals);
      next = page.proposal_pagination?.next_offset;
    }
    return { ...result, proposals };
  }
  const write = (url, body, signal) => client.fetchJson(url, {
    method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  return {
    list: (signal) => pages(root, "items", signal),
    state: (signal) => pages(projectRoot, "local_terms", signal),
    asset,
    create: (body, signal) => write(root, { ...body, project_id: projectId }, signal),
    preview: (body, signal) => write(`${projectRoot}/preview`, body, signal),
    apply: (body, signal) => write(`${projectRoot}/apply`, body, signal),
    detach: (body, signal) => write(`${projectRoot}/detach`, body, signal),
    contribute: (id, body, signal) => write(`${root}/${encodeURIComponent(id)}/proposals`, { ...body, project_id: projectId }, signal),
    review: (id, proposalId, body, signal) => write(`${root}/${encodeURIComponent(id)}/proposals/${encodeURIComponent(proposalId)}/review`, body, signal)
  };
}

// Preserve the retry key after an uncertain response; a changed intent gets a new key.
export function glossaryMutation(previous, action, body) {
  const intent = JSON.stringify({ action, body });
  return previous?.intent === intent ? previous : { intent, key: createIdempotencyKey("shared-glossary") };
}
