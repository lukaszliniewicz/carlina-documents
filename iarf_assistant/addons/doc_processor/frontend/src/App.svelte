<script lang="ts">
  import { Button, Dialog, Icon, PageHeader } from "@carlina/ui";
  import { onDestroy, onMount } from "svelte";
  import {
    addProjectDocuments,
    controlDocument,
    controlProjectRun,
    createProject,
    deleteDocument,
    deleteProject,
    loadDocuments,
    loadEngines,
    loadModels,
    loadProjects,
    loadPromptEditor,
    processDocument,
    removeProjectDocument,
    reorderProjectDocuments,
    savePromptSettings,
    startProjectRun,
    updateProject,
    uploadDocument,
    uploadProjectDocuments
  } from "./lib/api";
  import { defaultProcessingSettings, normalizeProcessingSettings } from "./lib/options";
  import type {
    DocumentProject,
    EngineOption,
    ModelOption,
    Notice,
    ProcessedDocument,
    ProcessingSettings,
    PromptEditorSection,
    PromptSettings
  } from "./lib/types";
  import { isActiveDocumentStatus, isActiveProjectStatus, needsDocumentAttention, selectDefaultModels } from "./lib/utils";
  import CropModal from "./components/CropModal.svelte";
  import DocumentPreviewModal from "./components/DocumentPreviewModal.svelte";
  import ExportModal from "./components/ExportModal.svelte";
  import ImageReviewModal from "./components/ImageReviewModal.svelte";
  import LibraryView from "./components/LibraryView.svelte";
  import NoticeBar from "./components/NoticeBar.svelte";
  import PdfPreviewModal from "./components/PdfPreviewModal.svelte";
  import ProjectExportModal from "./components/ProjectExportModal.svelte";
  import ProjectView from "./components/ProjectView.svelte";
  import PromptStudioModal from "./components/PromptStudioModal.svelte";
  import Sidebar from "./components/Sidebar.svelte";

  type Confirmation = {
    title: string;
    description: string;
    confirmLabel: string;
    action: () => Promise<void>;
  };

  let engines: EngineOption[] = [];
  let models: ModelOption[] = [];
  let settings: ProcessingSettings = defaultProcessingSettings();
  let promptSections: PromptEditorSection[] = [];
  let documents: ProcessedDocument[] = [];
  let projects: DocumentProject[] = [];
  let activeProjectId: string | null = null;
  let loading = true;
  let uploadBusy = false;
  let projectUploadBusy = false;
  let promptSaving = false;
  let promptStudioOpen = false;
  let creatingProject = false;
  let uploadResetToken = 0;
  let notice: Notice | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let noticeTimer: ReturnType<typeof setTimeout> | null = null;
  let refreshBusy = false;
  let connectionError = "";
  let pollFailures = 0;
  let refreshGeneration = 0;
  let refreshAbortController: AbortController | null = null;
  let mounted = true;
  let confirmation: Confirmation | null = null;
  let confirmationBusy = false;

  let cropDocumentId: string | null = null;
  let imageReviewDocumentId: string | null = null;
  let previewDocumentId: string | null = null;
  let previewAnchor = "";
  let pdfPreviewDocument: { id: string; filename: string } | null = null;
  let exportDocumentId: string | null = null;
  let exportProjectId: string | null = null;

  $: activeProject = activeProjectId ? projects.find((project) => project.id === activeProjectId) || null : null;
  $: activeDocumentCount = documents.filter((document) => isActiveDocumentStatus(document.status)).length;
  $: completedDocumentCount = documents.filter((document) => ["completed", "completed_with_errors"].includes(document.status)).length;
  $: attentionDocumentCount = documents.filter((document) => needsDocumentAttention(document.status)).length;

  onMount(() => {
    const handleVisibility = () => {
      if (document.hidden) {
        if (pollTimer) {
          clearTimeout(pollTimer);
          pollTimer = null;
        }
        return;
      }
      void refresh().catch((error) => console.warn("Document processor refresh failed.", error));
    };
    document.addEventListener("visibilitychange", handleVisibility);
    const handleOnline = () => { if (connectionError && !document.hidden) void refresh().catch(() => {}); };
    window.addEventListener("online", handleOnline);
    void initialize();
    return () => { document.removeEventListener("visibilitychange", handleVisibility); window.removeEventListener("online", handleOnline); };
  });

  onDestroy(() => {
    mounted = false;
    refreshAbortController?.abort();
    if (pollTimer) {
      clearTimeout(pollTimer);
    }
    if (noticeTimer) {
      clearTimeout(noticeTimer);
    }
  });

  async function initialize() {
    loading = true;
    try {
      const [loadedModels, loadedEngines, promptEditor] = await Promise.all([
        loadModels(),
        loadEngines(),
        loadPromptEditor()
      ]);
      models = loadedModels;
      engines = loadedEngines;
      promptSections = promptEditor.sections;
      const defaults = selectDefaultModels(models);
      settings = normalizeProcessingSettings({
        ...settings,
        parsing_model: settings.parsing_model || defaults.parsingModel,
        metadata_model: settings.metadata_model || defaults.metadataModel
      });
      await refresh();
    } catch (error) {
      showNotice("error", error instanceof Error ? error.message : "Failed to initialize document processor.");
    } finally {
      loading = false;
    }
  }

  async function refresh() {
    const generation = ++refreshGeneration;
    refreshAbortController?.abort();
    const controller = new AbortController();
    refreshAbortController = controller;
    try {
      const [nextDocuments, nextProjects] = await Promise.all([
        loadDocuments(controller.signal), loadProjects(controller.signal)
      ]);
      if (!mounted || controller.signal.aborted || generation !== refreshGeneration) return;
      documents = nextDocuments;
      projects = nextProjects;
      connectionError = "";
      pollFailures = 0;
      if (activeProjectId && !projects.some((project) => project.id === activeProjectId)) activeProjectId = null;
    } catch (error) {
      if (mounted && !controller.signal.aborted && generation === refreshGeneration) {
        connectionError = "Connection interrupted. Showing the last update and retrying automatically.";
        pollFailures = Math.min(pollFailures + 1, 4);
      }
      throw error;
    } finally {
      if (mounted && !controller.signal.aborted && generation === refreshGeneration) schedulePolling();
    }
  }

  function schedulePolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (document.hidden) {
      return;
    }
    const hasActiveDocuments = documents.some((document) => isActiveDocumentStatus(document.status));
    const hasActiveProjects = projects.some((project) => isActiveProjectStatus(project.latest_run?.status));
    if (hasActiveDocuments || hasActiveProjects || connectionError) {
      pollTimer = setTimeout(async () => {
        try {
          await refresh();
        } catch (error) {
          console.warn("Document processor polling failed.", error);
        }
      }, Math.min(3000 * 2 ** pollFailures, 30000));
    }
  }

  function showNotice(kind: Notice["kind"], message: string) {
    notice = { kind, message };
    if (noticeTimer) {
      clearTimeout(noticeTimer);
      noticeTimer = null;
    }
    if (kind !== "error") {
      noticeTimer = setTimeout(() => {
        if (notice?.message === message) {
          notice = null;
        }
        noticeTimer = null;
      }, 3500);
    }
  }

  function requestConfirmation(nextConfirmation: Confirmation) {
    confirmation = nextConfirmation;
  }

  async function confirmPendingAction() {
    if (!confirmation || confirmationBusy) return;
    confirmationBusy = true;
    const action = confirmation.action;
    try {
      await action();
      confirmation = null;
    } finally {
      confirmationBusy = false;
    }
  }

  async function refreshNow() {
    refreshBusy = true;
    try {
      await refresh();
      showNotice("info", "Library refreshed.");
    } catch {
      // The connection notice owns retry and disappears after recovery.
    } finally {
      refreshBusy = false;
    }
  }

  async function runAction(action: () => Promise<void>, fallback: string) {
    try {
      await action();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      showNotice("error", error instanceof Error ? error.message : fallback);
    }
  }

  async function handleUpload(file: File) {
    uploadBusy = true;
    await runAction(async () => {
      await uploadDocument(file, settings);
      uploadResetToken += 1;
      showNotice("success", "Document saved; processing started.");
      await refresh();
    }, "Document upload failed.");
    uploadBusy = false;
  }

  async function handleSavePrompts(nextPrompts: PromptSettings) {
    promptSaving = true;
    await runAction(async () => {
      const payload = await savePromptSettings(nextPrompts);
      promptSections = payload.sections;
      showNotice("success", "Prompt settings saved.");
    }, "Failed to save prompt settings.");
    promptSaving = false;
  }

  async function handleCreateProject(name: string) {
    creatingProject = true;
    await runAction(async () => {
      const created = await createProject(name, 2);
      await refresh();
      activeProjectId = created.id;
      showNotice("success", "Project created.");
    }, "Failed to create project.");
    creatingProject = false;
  }

  function handleDeleteDocument(documentId: string) {
    const target = documents.find((document) => document.id === documentId);
    requestConfirmation({
      title: "Delete document?",
      description: `"${target?.filename || "This document"}" and its generated output will be removed. This cannot be undone.`,
      confirmLabel: "Delete document",
      action: () => runAction(async () => {
        await deleteDocument(documentId);
        showNotice("success", "Document deleted.");
        await refresh();
      }, "Failed to delete document.")
    });
  }

  async function handleResumeDocument(documentId: string) {
    await runAction(async () => {
      await processDocument(documentId);
      await refresh();
    }, "Failed to start document processing.");
  }

  async function handleControlDocument(documentId: string, action: "pause" | "stop") {
    if (action === "stop") {
      requestConfirmation({
        title: "Stop extraction?",
        description: "The worker will stop at the next safe boundary. Any recoverable partial output remains available for a later retry.",
        confirmLabel: "Stop extraction",
        action: () => executeDocumentControl(documentId, action)
      });
      return;
    }
    await executeDocumentControl(documentId, action);
  }

  async function executeDocumentControl(documentId: string, action: "pause" | "stop") {
    await runAction(async () => {
      await controlDocument(documentId, action);
      await refresh();
    }, `Failed to ${action} document processing.`);
  }

  function handleDeleteProject(projectId: string) {
    const target = projects.find((project) => project.id === projectId);
    requestConfirmation({
      title: "Delete extraction project?",
      description: `"${target?.name || "This project"}" will be removed. Its documents remain in the library.`,
      confirmLabel: "Delete project",
      action: () => runAction(async () => {
        await deleteProject(projectId);
        activeProjectId = null;
        showNotice("success", "Project deleted.");
        await refresh();
      }, "Failed to delete project.")
    });
  }

  async function handleUpdateConcurrency(projectId: string, value: number) {
    await runAction(async () => {
      await updateProject(projectId, { document_concurrency: Math.max(1, Math.min(10, value || 1)) });
      await refresh();
    }, "Failed to update project concurrency.");
  }

  async function handleAddProjectDocument(projectId: string, documentId: string) {
    await runAction(async () => {
      await addProjectDocuments(projectId, [documentId]);
      await refresh();
    }, "Failed to add document.");
  }

  async function handleUploadProjectDocuments(projectId: string, files: FileList) {
    if (projectUploadBusy) return;
    projectUploadBusy = true;
    try {
      await runAction(async () => {
        await uploadProjectDocuments(projectId, files, settings);
        showNotice("success", "Documents uploaded to project.");
        await refresh();
      }, "Project upload failed.");
    } finally {
      projectUploadBusy = false;
    }
  }

  async function handleRunProject(projectId: string) {
    await runAction(async () => {
      await startProjectRun(projectId);
      await refresh();
    }, "Failed to start project run.");
  }

  async function handleControlProjectRun(runId: string, action: "pause" | "resume" | "stop") {
    if (action === "stop") {
      requestConfirmation({
        title: "Stop project run?",
        description: "Queued documents will be cancelled and active documents will stop at their next safe boundary.",
        confirmLabel: "Stop project run",
        action: () => executeProjectRunControl(runId, action)
      });
      return;
    }
    await executeProjectRunControl(runId, action);
  }

  async function executeProjectRunControl(runId: string, action: "pause" | "resume" | "stop") {
    await runAction(async () => {
      await controlProjectRun(runId, action);
      await refresh();
    }, `Failed to ${action} project run.`);
  }

  async function handleMoveProjectDocument(project: DocumentProject, documentId: string, offset: number) {
    const ids = project.documents.map((entry) => entry.document_id);
    const index = ids.indexOf(documentId);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= ids.length) {
      return;
    }
    [ids[index], ids[target]] = [ids[target], ids[index]];
    await runAction(async () => {
      await reorderProjectDocuments(project.id, ids);
      await refresh();
    }, "Failed to reorder project documents.");
  }

  async function handleRemoveProjectDocument(projectId: string, documentId: string) {
    await runAction(async () => {
      await removeProjectDocument(projectId, documentId);
      await refresh();
    }, "Failed to remove document from project.");
  }

  function closeReviewModals() {
    cropDocumentId = null;
    imageReviewDocumentId = null;
  }
</script>

<main class="dp-shell">
  <Sidebar
    bind:settings
    {engines}
    {models}
    {projects}
    {activeProjectId}
    {uploadBusy}
    {promptSaving}
    {creatingProject}
    {uploadResetToken}
    on:upload={(event) => handleUpload(event.detail)}
    on:openPromptStudio={() => (promptStudioOpen = true)}
    on:selectLibrary={() => (activeProjectId = null)}
    on:selectProject={(event) => (activeProjectId = event.detail)}
    on:createProject={(event) => handleCreateProject(event.detail)}
  />

  <section class="dp-main">
    <div class="dp-workspace">
      <PageHeader
        eyebrow="Document operations"
        title="Document Processor"
        description="Extract documents, check the results, and prepare them for reading or translation."
      >
        {#snippet actions()}
          <Button variant="outline" size="sm" disabled={refreshBusy} onclick={refreshNow}>
            <Icon name="refresh" size={15} class={refreshBusy ? "dp-spin" : ""} />
            {refreshBusy ? "Refreshing" : "Refresh"}
          </Button>
        {/snippet}
      </PageHeader>

      <div class="dp-summary-strip" role="list" aria-label="Document library summary">
        <div role="listitem"><span>Library</span><strong>{documents.length}</strong><small>total documents</small></div>
        <div role="listitem"><span>Processing</span><strong>{activeDocumentCount}</strong><small>active now</small></div>
        <div role="listitem"><span>Ready</span><strong>{completedDocumentCount}</strong><small>completed</small></div>
        <div role="listitem"><span>Review</span><strong>{attentionDocumentCount}</strong><small>need attention</small></div>
      </div>

      <NoticeBar {notice} />
      {#if connectionError}<div class="dp-notice warning" role="status">{connectionError} <button type="button" class="button compact" disabled={refreshBusy} on:click={refreshNow}>Retry now</button></div>{/if}
      {#if activeProject}
        <ProjectView
        project={activeProject}
        {documents}
        uploadBusy={projectUploadBusy}
        on:back={() => (activeProjectId = null)}
        on:deleteProject={(event) => handleDeleteProject(event.detail)}
        on:exportProject={(event) => (exportProjectId = event.detail)}
        on:updateConcurrency={(event) => handleUpdateConcurrency(activeProject.id, event.detail)}
        on:addDocument={(event) => handleAddProjectDocument(activeProject.id, event.detail)}
        on:uploadDocuments={(event) => handleUploadProjectDocuments(activeProject.id, event.detail)}
        on:runProject={(event) => handleRunProject(event.detail)}
        on:controlRun={(event) => handleControlProjectRun(event.detail.runId, event.detail.action)}
        on:removeDocument={(event) => handleRemoveProjectDocument(activeProject.id, event.detail)}
        on:moveDocument={(event) => handleMoveProjectDocument(activeProject, event.detail.documentId, event.detail.offset)}
        on:crop={(event) => (cropDocumentId = event.detail)}
        on:reviewImages={(event) => (imageReviewDocumentId = event.detail)}
        on:preview={(event) => (previewDocumentId = event.detail)}
        on:pdfPreview={(event) => {
          const doc = documents.find(d => d.id === event.detail);
          pdfPreviewDocument = doc ? { id: doc.id, filename: doc.filename } : { id: event.detail, filename: "" };
        }}
        on:export={(event) => (exportDocumentId = event.detail)}
        on:delete={(event) => handleDeleteDocument(event.detail)}
        on:resume={(event) => handleResumeDocument(event.detail)}
        on:pause={(event) => handleControlDocument(event.detail, "pause")}
        on:stop={(event) => handleControlDocument(event.detail, "stop")}
        />
      {:else}
        <LibraryView
        {documents}
        {loading}
        on:crop={(event) => (cropDocumentId = event.detail)}
        on:reviewImages={(event) => (imageReviewDocumentId = event.detail)}
        on:preview={(event) => (previewDocumentId = event.detail)}
        on:pdfPreview={(event) => {
          const doc = documents.find(d => d.id === event.detail);
          pdfPreviewDocument = doc ? { id: doc.id, filename: doc.filename } : { id: event.detail, filename: "" };
        }}
        on:export={(event) => (exportDocumentId = event.detail)}
        on:delete={(event) => handleDeleteDocument(event.detail)}
        on:resume={(event) => handleResumeDocument(event.detail)}
        on:pause={(event) => handleControlDocument(event.detail, "pause")}
        on:stop={(event) => handleControlDocument(event.detail, "stop")}
        />
      {/if}
    </div>
  </section>
</main>

<PromptStudioModal
  open={promptStudioOpen}
  sections={promptSections}
  {settings}
  saving={promptSaving}
  on:close={() => (promptStudioOpen = false)}
  on:save={(event) => handleSavePrompts(event.detail)}
/>

<CropModal
  open={Boolean(cropDocumentId)}
  documentId={cropDocumentId}
  on:close={closeReviewModals}
  on:saved={async () => {
    closeReviewModals();
    await refresh();
  }}
/>

<ImageReviewModal
  open={Boolean(imageReviewDocumentId)}
  documentId={imageReviewDocumentId}
  completed={["completed", "completed_with_errors"].includes(documents.find((item) => item.id === imageReviewDocumentId)?.status || "")}
  on:close={closeReviewModals}
  on:saved={async () => {
    closeReviewModals();
    await refresh();
  }}
/>

<DocumentPreviewModal
  open={Boolean(previewDocumentId)}
  documentId={previewDocumentId}
  initialAnchor={previewAnchor}
  on:close={() => { previewDocumentId = null; previewAnchor = ""; }}
  on:export={(event) => (exportDocumentId = event.detail)}
  on:saved={refresh}
/>

<ExportModal open={Boolean(exportDocumentId)} documentId={exportDocumentId} document={documents.find((item) => item.id === exportDocumentId) || null} on:close={() => (exportDocumentId = null)} on:inspect={(event) => { exportDocumentId = null; previewAnchor = event.detail.anchor; previewDocumentId = event.detail.documentId; }} />
<ProjectExportModal open={Boolean(exportProjectId)} projectId={exportProjectId} on:close={() => (exportProjectId = null)} />
<PdfPreviewModal
  open={Boolean(pdfPreviewDocument)}
  documentId={pdfPreviewDocument?.id ?? null}
  filename={pdfPreviewDocument?.filename ?? ""}
  on:close={() => (pdfPreviewDocument = null)}
/>

{#if confirmation}
  {@const pendingConfirmation = confirmation}
  <Dialog
    open={true}
    title={pendingConfirmation.title}
    description="Please review this irreversible action."
    size="sm"
    onclose={() => {
      if (!confirmationBusy) confirmation = null;
    }}
  >
    <p class="dp-confirmation-copy">{pendingConfirmation.description}</p>
    {#snippet footer()}
      <Button variant="quiet" disabled={confirmationBusy} onclick={() => (confirmation = null)}>
        Cancel
      </Button>
      <Button variant="danger" disabled={confirmationBusy} onclick={confirmPendingAction}>
        {confirmationBusy ? "Working…" : pendingConfirmation.confirmLabel}
      </Button>
    {/snippet}
  </Dialog>
{/if}
