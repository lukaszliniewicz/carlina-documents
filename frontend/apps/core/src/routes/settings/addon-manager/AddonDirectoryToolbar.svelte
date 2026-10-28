<script lang="ts">
  import { SearchField } from "@carlina/ui";

  import {
    readableAddonLabel,
    type AddonDirectoryStatus,
  } from "../../../lib/addon-manager.ts";

  interface Props {
    query: string;
    statusFilter: AddonDirectoryStatus;
    kindFilter: string;
    roleFilter: string;
    categoryFilter: string;
    kinds: readonly string[];
    roles: readonly string[];
    categories: readonly string[];
  }

  let {
    query = $bindable(),
    statusFilter = $bindable(),
    kindFilter = $bindable(),
    roleFilter = $bindable(),
    categoryFilter = $bindable(),
    kinds,
    roles,
    categories,
  }: Props = $props();
</script>

<section class="directory-toolbar" aria-label="Filter addons">
  <SearchField
    id="addon-directory-search"
    value={query}
    label="Search addons"
    placeholder="Search name, type, capability, provider…"
    oninput={(value) => (query = value)}
  />
  <label>
    <span>Status</span>
    <select id="addon-status-filter" name="addon_status_filter" bind:value={statusFilter}>
      <option value="all">All states</option>
      <option value="active">Active</option>
      <option value="inactive">Inactive</option>
      <option value="attention">Needs attention</option>
    </select>
  </label>
  <label>
    <span>Type</span>
    <select id="addon-kind-filter" name="addon_kind_filter" bind:value={kindFilter}>
      <option value="all">All package types</option>
      {#each kinds as kind}
        <option value={kind}>{readableAddonLabel(kind)}</option>
      {/each}
    </select>
  </label>
  <label>
    <span>Role</span>
    <select id="addon-role-filter" name="addon_role_filter" bind:value={roleFilter}>
      <option value="all">All roles</option>
      {#each roles as role}
        <option value={role}>{readableAddonLabel(role)}</option>
      {/each}
    </select>
  </label>
  <label>
    <span>Category</span>
    <select id="addon-category-filter" name="addon_category_filter" bind:value={categoryFilter}>
      <option value="all">All categories</option>
      {#each categories as category}
        <option value={category}>{readableAddonLabel(category)}</option>
      {/each}
    </select>
  </label>
</section>
