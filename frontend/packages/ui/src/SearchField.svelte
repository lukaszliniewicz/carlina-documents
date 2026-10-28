<script lang="ts">
  import Icon from "./Icon.svelte";

  interface Props {
    value?: string;
    label?: string;
    placeholder?: string;
    id?: string;
    oninput?: (value: string) => void;
    onkeydown?: (event: KeyboardEvent) => void;
  }

  let {
    value = "",
    label = "Search",
    placeholder = "Search",
    id = "search-field",
    oninput = () => undefined,
    onkeydown,
  }: Props = $props();
</script>

<label for={id}>
  <span class="sr-only">{label}</span>
  <Icon name="search" size={17} />
  <input
    {id}
    type="search"
    {value}
    {placeholder}
    {onkeydown}
    oninput={(event) => oninput(event.currentTarget.value)}
  />
</label>

<style>
  label {
    min-height: 2.6rem;
    display: flex;
    align-items: center;
    gap: 0.65rem;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink-muted);
    padding: 0 0.8rem;
  }

  label:focus-within {
    border-color: var(--color-focus);
    box-shadow: inset 3px 0 0 var(--color-focus);
    color: var(--color-ink);
  }

  input {
    width: 100%;
    min-width: 0;
    border: 0;
    outline: 0;
    background: transparent;
    font-size: 0.84rem;
  }

  input::placeholder {
    color: var(--color-ink-muted);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }
</style>
