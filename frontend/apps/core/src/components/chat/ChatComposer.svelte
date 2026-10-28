<script lang="ts">
  import { onDestroy } from "svelte";
  import type {
    FrontendCapability,
    FrontendChatModel,
    FrontendSlashCommand,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import ModelSelector from "./ModelSelector.svelte";
  import { matchingSkills, type ChatSkill } from "../../lib/skill-picker.ts";
  import type {
    ChatComposerConfig,
    ComposerAttachment,
    InlineComposerSuggestion,
  } from "../../lib/chat-state.ts";

  interface VoiceOption {
    id: string | number;
    name?: string;
    display_name?: string;
    mode?: string;
  }

  interface Props {
    value?: string;
    config: ChatComposerConfig;
    attachments: ComposerAttachment[];
    models: FrontendChatModel[];
    capabilities: FrontendCapability[];
    commands: FrontendSlashCommand[];
    skills: ChatSkill[];
    optionsLoading: boolean;
    busy: boolean;
    uploading: boolean;
    voiceModels: VoiceOption[];
    voices: VoiceOption[];
    voiceLoading: boolean;
    administratorApprovalMode: "review" | "full_access";
    onsend: () => void;
    onstop: () => void;
    onupload: (files: File[]) => Promise<void>;
    onremoveattachment: (id: string) => void;
    onstrategy: (id: string, strategy: string) => void;
    onloadvoice: () => Promise<void>;
    oncommand: (command: FrontendSlashCommand) => void;
    oninlinequery: (
      command: FrontendSlashCommand,
      query: string,
      signal: AbortSignal,
    ) => Promise<InlineComposerSuggestion[]>;
    onruntimepolicychange: (profile: "review" | "full_access" | null) => void;
    onrouting: () => void;
    onannounce: (message: string) => void;
  }

  let {
    value = $bindable(""),
    config = $bindable(),
    attachments,
    models,
    capabilities,
    commands,
    skills,
    optionsLoading,
    busy,
    uploading,
    voiceModels,
    voices,
    voiceLoading,
    administratorApprovalMode,
    onsend,
    onstop,
    onupload,
    onremoveattachment,
    onstrategy,
    onloadvoice,
    oncommand,
    oninlinequery,
    onruntimepolicychange,
    onrouting,
    onannounce,
  }: Props = $props();

  let textarea: HTMLTextAreaElement;
  let fileInput: HTMLInputElement;
  let openPanel = $state<"model" | "scope" | "policy" | "voice" | null>(
    null,
  );
  let capabilityQuery = $state("");
  let suggestionIndex = $state(0);
  let recording = $state(false);
  let recorder: MediaRecorder | null = null;
  let recordedChunks: Blob[] = [];
  let recordingStream: MediaStream | null = null;
  let slashDismissed = $state(false);
  let mentionDismissed = $state(false);
  let inlineSuggestions = $state<InlineComposerSuggestion[]>([]);
  let inlineSearching = $state(false);
  let inlineSearchError = $state("");
  let inlineSearchSequence = 0;
  let skillDismissed = $state(false);

  const model = $derived(models.find((item) => item.id === config.model));
  const selectedCapabilities = $derived(
    capabilities.filter((item) => config.capabilityIds.includes(item.id)),
  );
  const selectedVoice = $derived(
    voices.find((item) => String(item.id) === config.voice),
  );
  const policyLabel = $derived(
    config.runtimePolicySource === "conversation"
      ? config.runtimePolicy === "review"
        ? "Review"
        : "Full access"
      : `${administratorApprovalMode === "review" ? "Review" : "Full access"} default`,
  );
  const pythonRuntimeEnabled = $derived(
    config.runtimeProfileIds.includes("python.scratch") &&
      config.runtimeProfileIds.includes("python.session"),
  );
  const projectRuntimeEnabled = $derived(
    config.runtimeProfileIds.includes("project.workspace"),
  );
  const filteredCapabilities = $derived.by(() => {
    const needle = capabilityQuery.trim().toLocaleLowerCase();
    if (!needle) return capabilities;
    return capabilities.filter((capability) =>
      `${capability.title ?? capability.name ?? ""} ${capability.description ?? ""} ${capability.group ?? ""}`
        .toLocaleLowerCase()
        .includes(needle),
    );
  });
  const toolkitCapabilities = $derived(
    capabilities.filter(
      (capability) =>
        String(capability.capability_type ?? capability.type ?? "") ===
          "toolkit" || capability.id.endsWith(".toolkit"),
    ),
  );
  const slashQuery = $derived.by(() => {
    const beforeCaret = value.slice(0, textarea?.selectionStart ?? value.length);
    return beforeCaret.match(/(?:^|\s)\/([a-z0-9_-]*)$/i)?.[1] ?? null;
  });
  const commandSuggestions = $derived.by(() => {
    if (slashQuery === null || slashDismissed) return [];
    return commands
      .filter((command) =>
        command.command.toLocaleLowerCase().startsWith(slashQuery.toLocaleLowerCase()),
      )
      .slice(0, 8);
  });
  const inlineInvocation = $derived.by(() => {
    const caret = textarea?.selectionStart ?? value.length;
    const beforeCaret = value.slice(0, caret);
    const match = beforeCaret.match(
      /(^|\s)\/([a-z0-9_-]+)[ \t]+([^\n]*)$/i,
    );
    if (!match) return null;
    const command = commands.find(
      (candidate) =>
        supportsInlineSearch(candidate) &&
        candidate.command.toLocaleLowerCase() ===
          match[2].toLocaleLowerCase(),
    );
    if (!command) return null;
    return {
      command,
      query: match[3].trim(),
      start: (match.index ?? 0) + match[1].length,
      end: caret,
      minimum: Math.max(
        1,
        Number(command.inline_search?.min_query_length) || 2,
      ),
    };
  });
  const mentionInvocation = $derived.by(() => {
    const caret = textarea?.selectionStart ?? value.length;
    const beforeCaret = value.slice(0, caret);
    const match = beforeCaret.match(/(^|\s)@([a-z0-9_.-]*)$/i);
    if (!match) return null;
    return {
      query: match[2],
      start: (match.index ?? 0) + match[1].length,
      end: caret,
    };
  });
  const mentionSuggestions = $derived.by(() => {
    if (!mentionInvocation || mentionDismissed) return [];
    const needle = mentionInvocation.query.toLocaleLowerCase();
    return toolkitCapabilities
      .filter((capability) => {
        const alias = toolkitAlias(capability);
        return `${alias} ${labelForCapability(capability)} ${capability.id}`
          .toLocaleLowerCase()
          .includes(needle);
      })
      .slice(0, 8);
  });
  const skillInvocation = $derived.by(() => {
    const caret = textarea?.selectionStart ?? value.length;
    const beforeCaret = value.slice(0, caret);
    const match = beforeCaret.match(/(^|\s)\/sk(?:[ \t]+([^\n]*))?$/i);
    if (!match) return null;
    return {
      query: (match[2] ?? "").trim(),
      start: (match.index ?? 0) + match[1].length,
      end: caret,
    };
  });
  const skillSuggestions = $derived.by(() =>
    !skillInvocation || skillDismissed
      ? []
      : matchingSkills(skills, skillInvocation.query),
  );

  $effect(() => {
    value;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
  });

  $effect(() => {
    const invocation = inlineInvocation;
    const dismissed = slashDismissed;
    const sequence = ++inlineSearchSequence;
    if (
      !invocation ||
      dismissed ||
      invocation.query.length < invocation.minimum
    ) {
      inlineSuggestions = [];
      inlineSearching = false;
      inlineSearchError = "";
      return;
    }

    const controller = new AbortController();
    inlineSuggestions = [];
    inlineSearching = true;
    inlineSearchError = "";
    suggestionIndex = 0;
    const timer = setTimeout(() => {
      void oninlinequery(
        invocation.command,
        invocation.query,
        controller.signal,
      )
        .then((suggestions) => {
          if (sequence !== inlineSearchSequence || controller.signal.aborted) {
            return;
          }
          inlineSuggestions = suggestions;
          inlineSearching = false;
        })
        .catch((reason) => {
          if (sequence !== inlineSearchSequence || controller.signal.aborted) {
            return;
          }
          inlineSuggestions = [];
          inlineSearching = false;
          inlineSearchError =
            reason instanceof Error
              ? reason.message
              : "Inline search could not be completed.";
        });
    }, 220);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  });

  onDestroy(() => {
    if (recorder) {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      if (recorder.state !== "inactive") recorder.stop();
    }
    recordingStream?.getTracks().forEach((track) => track.stop());
  });

  function updateConfig(patch: Partial<ChatComposerConfig>) {
    config = { ...config, ...patch };
  }

  function changeRuntimePolicy(value: string): void {
    if (value === "review" || value === "full_access") {
      onruntimepolicychange(value);
    } else {
      onruntimepolicychange(null);
    }
  }

  function toggleRuntimeProfiles(
    profileIds: string[],
    enabled: boolean,
  ): void {
    const selected = new Set(config.runtimeProfileIds);
    for (const profileId of profileIds) {
      if (enabled) selected.add(profileId);
      else selected.delete(profileId);
    }
    updateConfig({ runtimeProfileIds: [...selected] });
  }

  function labelForCapability(capability: FrontendCapability): string {
    return String(capability.title ?? capability.name ?? capability.id);
  }

  function toolkitAlias(capability: FrontendCapability): string {
    const source = capability.id
      .replace(/\.toolkit$/i, "")
      .split(".")
      .pop()
      ?.toLocaleLowerCase() ?? "tools";
    const aliases: Record<string, string> = {
      googlecontacts: "contacts",
      googledrive: "gdrive",
      google_drive: "gdrive",
      iarf_library: "library",
      web_search: "web",
    };
    return (aliases[source] ?? source)
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  function supportsInlineSearch(command: FrontendSlashCommand): boolean {
    return Boolean(
      command.inline_search?.endpoint ||
        (command.is_inline && command.helper_id === "googlecontacts"),
    );
  }

  function voiceLabel(option: VoiceOption): string {
    return String(option.display_name ?? option.name ?? option.id);
  }

  function formatBytes(bytes: number): string {
    if (!bytes) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  }

  function togglePanel(panel: typeof openPanel) {
    openPanel = openPanel === panel ? null : panel;
    if (panel === "voice" && !voiceModels.length && !voiceLoading) {
      void onloadvoice();
    }
  }

  function chooseCommand(command: FrontendSlashCommand) {
    const caret = textarea?.selectionStart ?? value.length;
    const before = value.slice(0, caret);
    const after = value.slice(caret);
    if (supportsInlineSearch(command)) {
      const insertion = `/${command.command} `;
      const replaced = before.replace(
        /(?:^|\s)\/[a-z0-9_-]*$/i,
        (match) => `${match.startsWith("/") ? "" : match[0]}${insertion}`,
      );
      value = replaced + after;
      suggestionIndex = 0;
      slashDismissed = false;
      requestAnimationFrame(() => {
        textarea.focus();
        textarea.selectionStart = textarea.selectionEnd = replaced.length;
      });
      return;
    }
    if (command.ui_component_id) {
      const replaced = before.replace(/(?:^|\s)\/[a-z0-9_-]*$/i, (match) =>
        match.startsWith("/") ? "" : match[0],
      );
      value = `${replaced}${after}`;
      suggestionIndex = 0;
      slashDismissed = false;
      oncommand(command);
      requestAnimationFrame(() => textarea.focus());
      return;
    }
    const insertion = command.insert_text ?? `/${command.command} `;
    const replaced = before.replace(/(?:^|\s)\/[a-z0-9_-]*$/i, (match) => {
      const prefix = match.startsWith("/") ? "" : match[0];
      return `${prefix}${insertion}`;
    });
    value = replaced + after;
    suggestionIndex = 0;
    slashDismissed = false;
    requestAnimationFrame(() => {
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = replaced.length;
    });
  }

  function chooseInlineSuggestion(
    suggestion: InlineComposerSuggestion,
  ): void {
    const invocation = inlineInvocation;
    if (!invocation) return;
    const insertion = `${suggestion.insertText} `;
    value =
      value.slice(0, invocation.start) +
      insertion +
      value.slice(invocation.end);
    const nextCaret = invocation.start + insertion.length;
    inlineSuggestions = [];
    inlineSearchError = "";
    suggestionIndex = 0;
    slashDismissed = false;
    requestAnimationFrame(() => {
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = nextCaret;
    });
  }

  function chooseSkill(skill: ChatSkill): void {
    const invocation = skillInvocation;
    if (!invocation) return;
    const insertion = `/skill ${skill.id} `;
    value = value.slice(0, invocation.start) + insertion + value.slice(invocation.end);
    suggestionIndex = 0;
    skillDismissed = false;
    requestAnimationFrame(() => {
      if (!textarea) return;
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = invocation.start + insertion.length;
    });
  }

  function chooseToolkit(capability: FrontendCapability): void {
    const invocation = mentionInvocation;
    if (!invocation) return;
    const alias = toolkitAlias(capability);
    const insertion = `@${alias} `;
    value =
      value.slice(0, invocation.start) +
      insertion +
      value.slice(invocation.end);
    const retained =
      config.assistantMode === "direct" ? config.capabilityIds : [];
    updateConfig({
      assistantMode: "direct",
      capabilityIds: [...new Set([...retained, capability.id])],
    });
    const nextCaret = invocation.start + insertion.length;
    suggestionIndex = 0;
    mentionDismissed = false;
    onannounce(
      `${labelForCapability(capability)} added to the defined assistant scope.`,
    );
    requestAnimationFrame(() => {
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = nextCaret;
    });
  }

  function handleKeydown(event: KeyboardEvent) {
    if (skillInvocation) {
      if (event.key === "ArrowDown" && skillSuggestions.length) {
        event.preventDefault();
        suggestionIndex = (suggestionIndex + 1) % skillSuggestions.length;
        return;
      }
      if (event.key === "ArrowUp" && skillSuggestions.length) {
        event.preventDefault();
        suggestionIndex = (suggestionIndex - 1 + skillSuggestions.length) % skillSuggestions.length;
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        skillDismissed = true;
        return;
      }
      if (event.key === "Enter" && !event.shiftKey && skillSuggestions.length) {
        event.preventDefault();
        chooseSkill(skillSuggestions[suggestionIndex] ?? skillSuggestions[0]!);
        return;
      }
    }
    if (inlineSuggestions.length) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        suggestionIndex = (suggestionIndex + 1) % inlineSuggestions.length;
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        suggestionIndex =
          (suggestionIndex - 1 + inlineSuggestions.length) %
          inlineSuggestions.length;
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        suggestionIndex = 0;
        slashDismissed = true;
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chooseInlineSuggestion(
          inlineSuggestions[suggestionIndex] ?? inlineSuggestions[0]!,
        );
        return;
      }
    }
    if (mentionSuggestions.length) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        suggestionIndex = (suggestionIndex + 1) % mentionSuggestions.length;
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        suggestionIndex =
          (suggestionIndex - 1 + mentionSuggestions.length) %
          mentionSuggestions.length;
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        suggestionIndex = 0;
        mentionDismissed = true;
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chooseToolkit(
          mentionSuggestions[suggestionIndex] ?? mentionSuggestions[0]!,
        );
        return;
      }
    }
    if (commandSuggestions.length) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        suggestionIndex = (suggestionIndex + 1) % commandSuggestions.length;
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        suggestionIndex =
          (suggestionIndex - 1 + commandSuggestions.length) %
          commandSuggestions.length;
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        suggestionIndex = 0;
        slashDismissed = true;
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chooseCommand(commandSuggestions[suggestionIndex] ?? commandSuggestions[0]!);
        return;
      }
    }
    if (
      inlineInvocation &&
      inlineSearching &&
      event.key === "Enter" &&
      !event.shiftKey
    ) {
      event.preventDefault();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!busy && (value.trim() || attachments.length)) onsend();
    }
  }

  function handlePaste(event: ClipboardEvent) {
    const files = [...(event.clipboardData?.files ?? [])];
    if (!files.length) return;
    event.preventDefault();
    void onupload(files);
  }

  function handleDrop(event: DragEvent) {
    event.preventDefault();
    const files = [...(event.dataTransfer?.files ?? [])];
    if (files.length) void onupload(files);
  }

  async function toggleRecording() {
    if (recording) {
      recorder?.stop();
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !("MediaRecorder" in window)) {
      onannounce("Audio recording is not available in this browser.");
      return;
    }
    try {
      recordingStream = await navigator.mediaDevices.getUserMedia({
        audio: true,
      });
      recordedChunks = [];
      recorder = new MediaRecorder(recordingStream);
      recorder.ondataavailable = (event) => {
        if (event.data.size) recordedChunks.push(event.data);
      };
      recorder.onstop = () => {
        const type = recorder?.mimeType || "audio/webm";
        const extension = type.includes("ogg") ? "ogg" : "webm";
        const file = new File(
          recordedChunks,
          `recording-${Date.now()}.${extension}`,
          { type },
        );
        recordingStream?.getTracks().forEach((track) => track.stop());
        recordingStream = null;
        recorder = null;
        recording = false;
        if (file.size) void onupload([file]);
      };
      recorder.start();
      recording = true;
      onannounce("Recording started.");
    } catch (reason) {
      recordingStream?.getTracks().forEach((track) => track.stop());
      recordingStream = null;
      onannounce(
        reason instanceof Error
          ? `Recording could not start: ${reason.message}`
          : "Recording could not start.",
      );
    }
  }

  function toggleCapability(id: string) {
    updateConfig({
      capabilityIds: config.capabilityIds.includes(id)
        ? config.capabilityIds.filter((item) => item !== id)
        : [...config.capabilityIds, id],
    });
  }
</script>

<section
  class="composer"
  class:composer--recording={recording}
  aria-label="Message composer"
  ondragover={(event) => event.preventDefault()}
  ondrop={handleDrop}
>
  {#if attachments.length}
    <div class="attachment-tray" aria-label="Attachments for the next message">
      {#each attachments as attachment (attachment.id)}
        <article>
          <span class="attachment-tray__icon"><Icon name="file" size={15} /></span>
          <span class="attachment-tray__copy">
            <strong>{attachment.fileName}</strong>
            <small>{formatBytes(attachment.fileSize)}</small>
          </span>
          <select
            value={attachment.pass_as}
            aria-label={`How to pass ${attachment.fileName}`}
            onchange={(event) =>
              onstrategy(attachment.id, event.currentTarget.value)}
          >
            <option value="full_content">Extract content</option>
            <option value="metadata">Reference only</option>
            {#if attachment.mimeType === "application/pdf"}
              <option value="pdf_native">Native PDF</option>
              <option value="pdf_images">PDF as images</option>
            {/if}
          </select>
          <button
            type="button"
            aria-label={`Remove ${attachment.fileName}`}
            onclick={() => onremoveattachment(attachment.id)}
          >
            <Icon name="close" size={13} />
          </button>
        </article>
      {/each}
    </div>
  {/if}

  <div class="composer__field">
    <textarea
      bind:this={textarea}
      bind:value
      rows="1"
      placeholder="Write a message, paste media, or type / for commands and @ for toolkits…"
      aria-label="Message"
      onkeydown={handleKeydown}
      oninput={() => {
        slashDismissed = false;
        mentionDismissed = false;
        skillDismissed = false;
        suggestionIndex = 0;
      }}
      onpaste={handlePaste}
    ></textarea>

    {#if skillInvocation && !skillDismissed}
      <div class="slash-menu" role="listbox" aria-label="Available skills">
        {#if skillSuggestions.length}
          {#each skillSuggestions as skill, index (skill.id)}
            <button
              type="button"
              class:active={index === suggestionIndex}
              role="option"
              aria-selected={index === suggestionIndex}
              onmousedown={(event) => event.preventDefault()}
              onclick={() => chooseSkill(skill)}
            >
              <strong>{skill.name}</strong>
              <span>{skill.description} · {skill.source}</span>
            </button>
          {/each}
        {:else}
          <p class="slash-menu__state">No available skills match. Unavailable skills are not selectable.</p>
        {/if}
      </div>
    {:else if inlineInvocation && !slashDismissed && inlineInvocation.query.length >= inlineInvocation.minimum}
      <div class="slash-menu" role="listbox" aria-label={`Results for /${inlineInvocation.command.command}`}>
        {#if inlineSearching}
          <p class="slash-menu__state" role="status">
            Searching for “{inlineInvocation.query}”…
          </p>
        {:else if inlineSearchError}
          <p class="slash-menu__state slash-menu__state--error" role="alert">
            {inlineSearchError}
          </p>
        {:else if inlineSuggestions.length}
          {#each inlineSuggestions as suggestion, index (suggestion.id)}
            <button
              type="button"
              class:active={index === suggestionIndex}
              role="option"
              aria-selected={index === suggestionIndex}
              onmousedown={(event) => event.preventDefault()}
              onclick={() => chooseInlineSuggestion(suggestion)}
            >
              <strong>{suggestion.label}</strong>
              <span>{suggestion.description ?? suggestion.insertText}</span>
            </button>
          {/each}
        {:else}
          <p class="slash-menu__state">
            No matching contacts. Keep typing or press Enter to open the full picker.
          </p>
        {/if}
      </div>
    {:else if mentionSuggestions.length}
      <div class="slash-menu" role="listbox" aria-label="Toolkit actions">
        {#each mentionSuggestions as capability, index (capability.id)}
          <button
            type="button"
            class:active={index === suggestionIndex}
            role="option"
            aria-selected={index === suggestionIndex}
            onmousedown={(event) => event.preventDefault()}
            onclick={() => chooseToolkit(capability)}
          >
            <strong>@{toolkitAlias(capability)}</strong>
            <span>
              {labelForCapability(capability)}
              {capability.description ? ` · ${capability.description}` : ""}
            </span>
          </button>
        {/each}
      </div>
    {:else if commandSuggestions.length}
      <div class="slash-menu" role="listbox" aria-label="Slash commands">
        {#each commandSuggestions as command, index (command.command)}
          <button
            type="button"
            class:active={index === suggestionIndex}
            role="option"
            aria-selected={index === suggestionIndex}
            onmousedown={(event) => event.preventDefault()}
            onclick={() => chooseCommand(command)}
          >
            <strong>/{command.command}</strong>
            <span>{command.description ?? "Insert command"}</span>
          </button>
        {/each}
      </div>
    {/if}
  </div>

  <div class="composer__toolbar">
    <div class="composer__primary-controls">
      <input
        bind:this={fileInput}
        class="sr-only"
        type="file"
        multiple
        onchange={(event) => {
          const files = [...(event.currentTarget.files ?? [])];
          if (files.length) void onupload(files);
          event.currentTarget.value = "";
        }}
      />
      <button
        class="icon-control"
        type="button"
        disabled={uploading}
        aria-label="Attach files"
        title="Attach files"
        onclick={() => fileInput.click()}
      >
        <Icon name="attach" size={17} />
      </button>
      <button
        class="icon-control"
        class:active={recording}
        type="button"
        aria-label={recording ? "Stop recording" : "Record audio"}
        title={recording ? "Stop recording" : "Record audio"}
        onclick={toggleRecording}
      >
        <Icon name={recording ? "stop" : "microphone"} size={17} />
      </button>
      {#if uploading}<span class="uploading">Uploading…</span>{/if}
    </div>

    <div class="composer__send">
      <span aria-live="polite">
        {busy ? "Response in progress" : "Enter to send · Shift+Enter for a line break"}
      </span>
      <button
        class:stop={busy}
        type="button"
        disabled={!busy && !value.trim() && !attachments.length}
        aria-label={busy ? "Stop response" : "Send message"}
        onclick={busy ? onstop : onsend}
      >
        <Icon name={busy ? "stop" : "send"} size={16} />
        <span>{busy ? "Stop" : "Send"}</span>
      </button>
    </div>
  </div>

  <div class="composer__configuration">
    <button
      type="button"
      aria-expanded={openPanel === "model"}
      onclick={() => togglePanel("model")}
    >
      <span>Model</span>
      <strong>{model?.name ?? (optionsLoading ? "Loading…" : "Automatic")}</strong>
    </button>
    <button
      type="button"
      aria-expanded={openPanel === "scope"}
      onclick={() => togglePanel("scope")}
    >
      <span>Scope</span>
      <strong>
        {config.assistantMode === "default"
          ? "Autonomous"
          : `${selectedCapabilities.length} selected`}
      </strong>
    </button>
    <button
      type="button"
      aria-expanded={openPanel === "policy"}
      onclick={() => togglePanel("policy")}
    >
      <span>Policy</span>
      <strong>{policyLabel}</strong>
    </button>
    <button
      type="button"
      aria-expanded={openPanel === "voice"}
      onclick={() => togglePanel("voice")}
    >
      <span>Voice</span>
      <strong>{selectedVoice ? voiceLabel(selectedVoice) : config.voiceModel ? "Provider default" : "Browser default"}</strong>
    </button>
  </div>

  {#if openPanel}
    <div class="configuration-panel">
      <header>
        <div>
          <span>Conversation control</span>
          <strong>
            {openPanel === "model"
              ? "Model & reasoning"
              : openPanel === "scope"
                ? "Assistant scope"
                : openPanel === "policy"
                  ? "Policy & context"
                  : "Voice"}
          </strong>
        </div>
        <button type="button" aria-label="Close controls" onclick={() => (openPanel = null)}>
          <Icon name="close" size={14} />
        </button>
      </header>

      {#if openPanel === "model"}
        <div class="panel-grid">
          <div class="panel-field">
            <span>Language model</span>
            <ModelSelector
              value={config.model}
              {models}
              disabled={optionsLoading}
              onchange={(model) => updateConfig({ model })}
              {onrouting}
            />
          </div>
          <label>
            <span>Reasoning effort</span>
            <select
              value={config.thinkingLevel}
              onchange={(event) =>
                updateConfig({ thinkingLevel: event.currentTarget.value })}
            >
              <option value="default">Provider default</option>
              <option value="off">Off</option>
              <option value="minimal">Minimal</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="xhigh">Extra high</option>
            </select>
          </label>
        </div>
      {:else if openPanel === "scope"}
        <div class="scope-mode">
          <label>
            <input
              type="radio"
              name="assistant-mode"
              checked={config.assistantMode === "default"}
              onchange={() => updateConfig({ assistantMode: "default" })}
            />
            <span><strong>Autonomous discovery</strong><small>Choose the right active tools as the task unfolds.</small></span>
          </label>
          <label>
            <input
              type="radio"
              name="assistant-mode"
              checked={config.assistantMode === "direct"}
              onchange={() => updateConfig({ assistantMode: "direct" })}
            />
            <span><strong>Defined scope</strong><small>Use only the capabilities selected below.</small></span>
          </label>
        </div>
        {#if config.assistantMode === "direct"}
          <input
            class="capability-search"
            bind:value={capabilityQuery}
            type="search"
            placeholder="Filter capabilities"
            aria-label="Filter capabilities"
          />
          <div class="capability-list">
            {#each filteredCapabilities as capability (capability.id)}
              <label>
                <input
                  type="checkbox"
                  checked={config.capabilityIds.includes(capability.id)}
                  onchange={() => toggleCapability(capability.id)}
                />
                <span>
                  <strong>{labelForCapability(capability)}</strong>
                  <small>{capability.description ?? capability.group ?? capability.type ?? ""}</small>
                </span>
              </label>
            {:else}
              <p>No matching capabilities.</p>
            {/each}
          </div>
          <label class="delegation">
            <input
              type="checkbox"
              checked={config.enableDelegation}
              onchange={(event) =>
                updateConfig({ enableDelegation: event.currentTarget.checked })}
            />
            <span>Allow scoped delegation</span>
          </label>
        {/if}
      {:else if openPanel === "policy"}
        <div class="panel-grid">
          <label>
            <span>Action policy</span>
            <select
              value={
                config.runtimePolicySource === "conversation"
                  ? config.runtimePolicy
                  : "default"
              }
              onchange={(event) =>
                changeRuntimePolicy(event.currentTarget.value)}
            >
              <option value="default">
                Use administrator default · {administratorApprovalMode === "review"
                  ? "Review first"
                  : "Full access"}
              </option>
              <option value="review">Ask before sensitive actions</option>
              <option value="full_access">Full access</option>
            </select>
          </label>
          <label>
            <span>Context stewardship</span>
            <select
              value={config.contextMode}
              onchange={(event) =>
                updateConfig({
                  contextMode: event.currentTarget.value as
                    | "assisted"
                    | "manual"
                    | "aggressive",
                })}
            >
              <option value="assisted">Assisted</option>
              <option value="manual">Manual</option>
              <option value="aggressive">Aggressive</option>
            </select>
          </label>
        </div>
        <div class="runtime-profiles">
          <label>
            <input
              type="checkbox"
              checked={pythonRuntimeEnabled}
              onchange={(event) =>
                toggleRuntimeProfiles(
                  ["python.scratch", "python.session"],
                  event.currentTarget.checked,
                )}
            />
            <span>
              <strong>Governed Python</strong>
              <small>Allow bounded calculations, data transforms, documents, and resumable code sessions.</small>
            </span>
          </label>
          <label>
            <input
              type="checkbox"
              checked={projectRuntimeEnabled}
              onchange={(event) =>
                toggleRuntimeProfiles(
                  ["project.workspace"],
                  event.currentTarget.checked,
                )}
            />
            <span>
              <strong>Project workspace</strong>
              <small>Allow permission-gated source inspection, edits, tests, and supervised commands.</small>
            </span>
          </label>
        </div>
        <p class="panel-note">
          Review mode pauses before sensitive actions. Context stewardship
          controls how aggressively large tool results are compacted. Runtime
          profiles grant model access to code; they do not bypass review gates.
        </p>
      {:else}
        {#if voiceLoading}
          <p class="panel-note">Loading optional voice providers…</p>
        {:else if voiceModels.length || voices.length}
          <div class="panel-grid">
            <label>
              <span>Voice model</span>
              <select
                value={config.voiceModel}
                onchange={(event) =>
                  updateConfig({ voiceModel: event.currentTarget.value })}
              >
                <option value="">Automatic</option>
                {#each voiceModels as option (option.id)}
                  <option value={String(option.id)}>{voiceLabel(option)}</option>
                {/each}
              </select>
            </label>
            <label>
              <span>Cloned voice profile</span>
              <select
                value={config.voice}
                onchange={(event) => updateConfig({ voice: event.currentTarget.value })}
              >
                <option value="">Provider default</option>
                {#each voices as option (option.id)}
                  <option value={String(option.id)}>{voiceLabel(option)}</option>
                {/each}
              </select>
            </label>
          </div>
        {:else}
          <p class="panel-note">
            No optional voice provider is active. Responses can still be read
            aloud with the browser voice from each message.
          </p>
        {/if}
      {/if}
    </div>
  {/if}
</section>

<style>
  .composer {
    position: relative;
    width: min(54rem, calc(100% - 2rem));
    margin: 0 auto 0.55rem;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
    box-shadow: 0 1rem 3rem color-mix(in srgb, var(--color-umber) 10%, transparent);
  }

  .composer--recording {
    border-color: var(--color-danger);
  }

  .composer__field {
    position: relative;
  }

  textarea {
    width: 100%;
    min-height: 3.15rem;
    max-height: 13.75rem;
    display: block;
    resize: none;
    overflow-y: auto;
    border: 0;
    padding: 0.7rem 0.85rem 0.35rem;
    background: transparent;
    color: var(--color-ink);
    font-size: 0.82rem;
    line-height: 1.42;
    outline: none;
  }

  textarea::placeholder {
    color: var(--color-ink-muted);
  }

  .composer__toolbar {
    min-height: 2.55rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.2rem 0.35rem 0.3rem;
  }

  .composer__primary-controls,
  .composer__send {
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }

  .icon-control {
    width: 2rem;
    height: 2rem;
    display: grid;
    place-items: center;
    border: 1px solid transparent;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
  }

  .icon-control:hover,
  .icon-control.active {
    border-color: var(--color-rule);
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .icon-control.active {
    border-color: var(--color-danger);
    color: var(--color-danger);
  }

  .uploading {
    color: var(--color-ink-muted);
    font-size: 0.64rem;
  }

  .composer__send > span {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
  }

  .composer__send > button {
    min-width: 4.7rem;
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.45rem;
    border: 1px solid var(--color-lichen);
    padding: 0 0.8rem;
    background: var(--color-lichen);
    color: var(--color-surface);
    cursor: pointer;
    font-size: 0.7rem;
    font-weight: 700;
  }

  .composer__send > button.stop {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .composer__send > button:disabled {
    border-color: var(--color-rule);
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    cursor: not-allowed;
  }

  .composer__configuration {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-top: 1px solid var(--color-rule);
    background: color-mix(in srgb, var(--color-surface-muted) 55%, transparent);
  }

  .composer__configuration > button {
    min-width: 0;
    min-height: 2.3rem;
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 0.08rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0 0.55rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    text-align: left;
  }

  .composer__configuration > button:hover,
  .composer__configuration > button[aria-expanded="true"] {
    background: var(--color-clay-muted);
    color: var(--color-ink);
  }

  .composer__configuration span {
    font-size: 0.53rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .composer__configuration strong {
    max-width: 100%;
    overflow: hidden;
    font-size: 0.66rem;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attachment-tray {
    display: grid;
    gap: 1px;
    border-bottom: 1px solid var(--color-rule);
    background: var(--color-rule);
  }

  .attachment-tray article {
    min-height: 3.1rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 0.6rem;
    padding: 0.35rem 0.55rem;
    background: var(--color-surface-muted);
  }

  .attachment-tray__icon {
    width: 1.8rem;
    height: 1.8rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
  }

  .attachment-tray__copy {
    min-width: 0;
    display: grid;
  }

  .attachment-tray__copy strong {
    overflow: hidden;
    font-size: 0.68rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attachment-tray__copy small {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .attachment-tray select,
  .attachment-tray button {
    min-height: 1.9rem;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-size: 0.62rem;
  }

  .attachment-tray button {
    width: 1.9rem;
    display: grid;
    place-items: center;
    cursor: pointer;
  }

  .configuration-panel {
    position: absolute;
    z-index: 12;
    right: -1px;
    bottom: calc(100% + 0.55rem);
    width: min(34rem, calc(100vw - 2rem));
    max-height: min(32rem, calc(100vh - 10rem));
    overflow-y: auto;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
    box-shadow: 0 1.1rem 3rem color-mix(in srgb, var(--color-ink) 16%, transparent);
  }

  .configuration-panel > header {
    min-height: 4rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.6rem 0.8rem;
  }

  .configuration-panel > header div {
    display: grid;
    gap: 0.15rem;
  }

  .configuration-panel > header span {
    color: var(--color-clay);
    font-size: 0.55rem;
    font-weight: 750;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  .configuration-panel > header strong {
    font-family: var(--font-editorial);
    font-size: 1.1rem;
    font-weight: 550;
  }

  .configuration-panel > header button {
    width: 2rem;
    height: 2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule);
    background: transparent;
    cursor: pointer;
  }

  .panel-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.8rem;
    padding: 1rem;
  }

  .panel-grid label,
  .panel-grid .panel-field {
    display: grid;
    gap: 0.35rem;
  }

  .panel-grid label > span,
  .panel-grid .panel-field > span {
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .panel-grid select,
  .capability-search {
    width: 100%;
    min-height: 2.5rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.6rem;
    background: var(--color-surface);
    font-size: 0.7rem;
  }

  .scope-mode {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1px;
    margin: 1rem;
    background: var(--color-rule);
  }

  .scope-mode label,
  .capability-list label,
  .delegation {
    display: flex;
    align-items: flex-start;
    gap: 0.6rem;
    padding: 0.75rem;
    background: var(--color-surface-muted);
    cursor: pointer;
  }

  .runtime-profiles {
    display: grid;
    gap: 1px;
    margin: 0 1rem 0.7rem;
    border: 1px solid var(--color-rule);
    background: var(--color-rule);
  }

  .runtime-profiles label {
    min-height: 3.25rem;
    display: flex;
    align-items: flex-start;
    gap: 0.65rem;
    padding: 0.65rem 0.75rem;
    background: var(--color-surface);
    cursor: pointer;
  }

  .runtime-profiles input {
    margin-top: 0.15rem;
    accent-color: var(--color-lichen);
  }

  .runtime-profiles span,
  .runtime-profiles strong,
  .runtime-profiles small {
    display: block;
  }

  .runtime-profiles strong {
    font-size: 0.72rem;
  }

  .runtime-profiles small {
    margin-top: 0.18rem;
    color: var(--color-ink-muted);
    font-size: 0.64rem;
    line-height: 1.35;
  }

  .scope-mode span,
  .capability-list span {
    min-width: 0;
    display: grid;
    gap: 0.15rem;
  }

  .scope-mode strong,
  .capability-list strong {
    font-size: 0.7rem;
    font-weight: 650;
  }

  .scope-mode small,
  .capability-list small {
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    line-height: 1.4;
  }

  .capability-search {
    width: calc(100% - 2rem);
    margin: 0 1rem 0.5rem;
  }

  .capability-list {
    max-height: 15rem;
    display: grid;
    gap: 1px;
    overflow-y: auto;
    margin: 0 1rem;
    background: var(--color-rule);
  }

  .capability-list label {
    background: var(--color-surface);
  }

  .capability-list p {
    margin: 0;
    padding: 1rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .delegation {
    margin: 0.7rem 1rem 1rem;
    font-size: 0.68rem;
  }

  .panel-note {
    margin: 0;
    border-top: 1px solid var(--color-rule);
    padding: 0.8rem 1rem 1rem;
    color: var(--color-ink-muted);
    font-family: var(--font-editorial);
    font-size: 0.82rem;
    line-height: 1.55;
  }

  .slash-menu {
    position: absolute;
    z-index: 13;
    right: 0.7rem;
    bottom: calc(100% + 0.25rem);
    left: 0.7rem;
    max-height: 18rem;
    overflow-y: auto;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
    box-shadow: 0 0.8rem 2rem color-mix(in srgb, var(--color-ink) 13%, transparent);
  }

  .slash-menu button {
    width: 100%;
    min-height: 3rem;
    display: grid;
    grid-template-columns: minmax(7rem, 0.3fr) minmax(0, 1fr);
    align-items: center;
    gap: 0.7rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.55rem 0.7rem;
    background: transparent;
    color: var(--color-ink-soft);
    cursor: pointer;
    text-align: left;
  }

  .slash-menu button.active,
  .slash-menu button:hover {
    background: var(--color-clay-muted);
    color: var(--color-ink);
  }

  .slash-menu strong {
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.69rem;
  }

  .slash-menu span {
    overflow: hidden;
    color: var(--color-ink-muted);
    font-size: 0.65rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .slash-menu__state {
    min-height: 3rem;
    display: flex;
    align-items: center;
    margin: 0;
    padding: 0.6rem 0.75rem;
    color: var(--color-ink-muted);
    font-size: 0.67rem;
    line-height: 1.4;
  }

  .slash-menu__state--error {
    color: var(--color-danger);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @media (max-width: 42rem) {
    .composer {
      width: calc(100% - 0.75rem);
      margin-bottom: 0.45rem;
    }

    .composer__send > span {
      display: none;
    }

    .composer__send > button {
      min-width: 2.35rem;
      padding: 0;
    }

    .composer__send > button span {
      display: none;
    }

    .composer__configuration {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .composer__configuration > button:nth-child(2) {
      border-right: 0;
    }

    .composer__configuration > button:nth-child(-n + 2) {
      border-bottom: 1px solid var(--color-rule);
    }

    .panel-grid,
    .scope-mode {
      grid-template-columns: 1fr;
    }

    .attachment-tray article {
      grid-template-columns: auto minmax(0, 1fr) auto;
    }

    .attachment-tray select {
      grid-column: 2 / -1;
      grid-row: 2;
    }
  }
</style>
