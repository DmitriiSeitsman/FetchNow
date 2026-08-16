import { MediaApi } from "./api";
import {
  isDownloadEligible,
  type DownloadJob,
  type InspectionJob,
  type InspectionResult,
  type MediaFormat,
  type ProgressStage,
} from "./contracts";
import { projectCapabilityUi } from "./capabilities";
import { flowStatusText, STAGE_LABEL } from "./progress";
import { pickHighestEligibleFormat, reconcileSelectedFormatId } from "./quality";
import { generateAccessToken } from "./credentials";
import {
  fileSystemAccessSupported,
  computeGrantRefreshDelayMs,
  GRANT_HANDOFF_SAFETY_MS,
  GRANT_REISSUE_BUFFER_MS,
  isSecureDeliveryContext,
  PickerCancelledError,
  saveArtifactStream,
} from "./download";
import {
  FlowError,
  flowErrorFromCode,
  isAbortError,
  userMessageForCode,
} from "./errors";
import { pollUntilTerminal, jobProgressSemanticKey, isActiveDownloadProgress } from "./poller";
import { FlowSession, type RecoveryRecord } from "./session";
import {
  FlowMachine,
  isRestorablePhase,
  isTerminalPhase,
  type FlowPhase,
} from "./state-machine";

export type FlowSnapshot = {
  phase: FlowPhase;
  statusText: string;
  errorText: string | null;
  result: InspectionResult | null;
  formats: MediaFormat[];
  selectedFormatId: string | null;
  downloadEligible: boolean;
  canSelectQuality?: boolean;
  muxingBlocked: boolean;
  httpsRequired: boolean;
  grantArming: boolean;
  canNativeDownload: boolean;
  canRetryGrant: boolean;
  canSaveAs: boolean;
  downloadHref: string | null;
  nativeDownloadHandoff: boolean;
  /** The browser cannot pick a save location; a capability, not a phase. */
  browserUnsupported: boolean;
  busy: boolean;
  canSubmit: boolean;
  canStartOver: boolean;
  canCancelTask: boolean;
  restored: boolean;
  progressStage: ProgressStage | null;
  progressPercent: number | null;
  artifactBytes: number | null;
};

export type ControllerHooks = {
  api?: MediaApi;
  session?: FlowSession;
  machine?: FlowMachine;
  save?: typeof saveArtifactStream;
  generateToken?: () => string;
  documentHidden?: () => boolean;
  pickerSupported?: () => boolean;
  secureContext?: () => boolean;
  onChange?: (snapshot: FlowSnapshot) => void;
  now?: () => number;
};

export class MediaFlowController {
  private readonly api: MediaApi;
  private readonly session: FlowSession;
  private readonly machine: FlowMachine;
  private readonly save: typeof saveArtifactStream;
  private readonly generateToken: () => string;
  private readonly documentHidden: () => boolean;
  private readonly pickerSupported: () => boolean;
  private readonly secureContext: () => boolean;
  private readonly now: () => number;
  private readonly onChange?: (snapshot: FlowSnapshot) => void;

  private abort: AbortController | null = null;
  private grantAbort: AbortController | null = null;
  private grantRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  private token: string | null = null;
  private mediaJob: InspectionJob | null = null;
  private downloadJob: DownloadJob | null = null;
  private selectedFormatId: string | null = null;
  private errorText: string | null = null;
  private grantArming = false;
  private grantNeedsRetry = false;
  private grantExpiresAt: string | null = null;
  private downloadPath: string | null = null;
  private nativeDownloadHandoff = false;
  private grantGeneration = 0;
  private resumeDownloadId: string | null = null;
  private restored = false;

  constructor(hooks: ControllerHooks = {}) {
    this.api = hooks.api ?? new MediaApi();
    this.session = hooks.session ?? new FlowSession();
    this.machine = hooks.machine ?? new FlowMachine();
    this.save = hooks.save ?? saveArtifactStream;
    this.generateToken = hooks.generateToken ?? generateAccessToken;
    this.documentHidden =
      hooks.documentHidden ?? (() => globalThis.document?.hidden === true);
    this.pickerSupported = hooks.pickerSupported ?? fileSystemAccessSupported;
    this.secureContext = hooks.secureContext ?? isSecureDeliveryContext;
    this.now = hooks.now ?? Date.now;
    this.onChange = hooks.onChange;
  }

  snapshot(): FlowSnapshot {
    const result = this.mediaJob?.state === "inspected" ? this.mediaJob.result : null;
    const formats = result?.formats ?? [];
    const muxingBlocked = result?.muxingRequired === true;
    const capabilities = projectCapabilityUi(this.mediaJob?.providerCapabilities);
    const selected = formats.find((f) => f.formatOptionId === this.selectedFormatId);
    const downloadEligible =
      capabilities.canDownloadVideo &&
      !muxingBlocked &&
      selected !== undefined &&
      isDownloadEligible(selected);
    const phase = this.machine.current;
    const httpsRequired = phase === "ready" && !this.secureContext();
    const armed =
      !httpsRequired &&
      this.downloadPath !== null &&
      !this.grantArming &&
      this.grantStillValid(GRANT_HANDOFF_SAFETY_MS);
    const canRetryGrant =
      phase === "ready" &&
      !httpsRequired &&
      !this.grantArming &&
      !armed &&
      (this.grantNeedsRetry || this.downloadPath === null);
    return {
      phase,
      statusText: this.statusText(),
      errorText: this.errorText,
      result,
      formats,
      selectedFormatId: this.selectedFormatId,
      downloadEligible,
      canSelectQuality: capabilities.canSelectQuality,
      muxingBlocked,
      httpsRequired,
      grantArming: this.grantArming,
      canNativeDownload: phase === "ready" && armed,
      canRetryGrant,
      canSaveAs: phase === "ready" && this.pickerSupported(),
      downloadHref: armed ? this.downloadPath : null,
      nativeDownloadHandoff: this.nativeDownloadHandoff,
      browserUnsupported: !this.pickerSupported(),
      busy:
        this.machine.isBusy() ||
        this.grantArming ||
        [
          "submitting",
          "inspecting",
          "enqueueing_download",
          "downloading",
          "saving",
        ].includes(phase),
      canSubmit: phase === "idle",
      canStartOver: phase !== "idle",
      canCancelTask:
        Boolean(this.downloadJob?.cancellable) &&
        (phase === "downloading" || phase === "enqueueing_download"),
      restored: this.restored,
      progressStage: this.downloadJob?.progressStage ?? null,
      progressPercent: this.downloadJob?.progressPercent ?? null,
      artifactBytes: this.downloadJob?.artifactBytes ?? null,
    };
  }

  private statusText(): string {
    if (this.errorText) {
      return this.errorText;
    }
    if (this.machine.current === "ready") {
      if (!this.secureContext()) {
        return userMessageForCode("HTTPS_REQUIRED").text;
      }
      if (this.grantArming) {
        return "Preparing secure download…";
      }
      if (this.nativeDownloadHandoff && this.grantStillValid(GRANT_HANDOFF_SAFETY_MS)) {
        return "Sent to your browser";
      }
      if (this.grantNeedsRetry || this.downloadPath === null) {
        return "Download access needs to be refreshed.";
      }
    }
    if (this.restored && this.machine.current === "inspected") {
      return "Restored an existing download task. Choose a quality or continue.";
    }
    if (this.restored && this.machine.current === "downloading") {
      return (
        flowStatusText(
          this.machine.current,
          this.downloadJob?.progressStage ?? null,
          {
            progressPercent: this.downloadJob?.progressPercent ?? null,
            artifactBytes: this.downloadJob?.artifactBytes ?? null,
            formats: this.mediaJob?.result?.formats ?? [],
          },
        ) || "Restored an existing download task."
      );
    }
    if (this.machine.current === "cancelled") {
      return STAGE_LABEL.cancelled;
    }
    return flowStatusText(this.machine.current, this.downloadJob?.progressStage ?? null, {
      progressPercent: this.downloadJob?.progressPercent ?? null,
      artifactBytes: this.downloadJob?.artifactBytes ?? null,
      formats: this.mediaJob?.result?.formats ?? [],
    });
  }

  private emit(): void {
    this.onChange?.(this.snapshot());
  }

  private persist(): void {
    if (!this.token || !isRestorablePhase(this.machine.current)) {
      this.session.clear();
      return;
    }
    const expiresAt = this.downloadJob?.expiresAt ?? this.mediaJob?.expiresAt ?? null;
    if (!expiresAt) {
      return;
    }
    const record: RecoveryRecord = {
      v: 2,
      token: this.token,
      mediaJobId: this.mediaJob?.id ?? null,
      downloadJobId: this.downloadJob?.id ?? null,
      formatOptionId: this.selectedFormatId ?? this.downloadJob?.formatOptionId ?? null,
      phase: this.machine.current,
      expiresAt,
    };
    this.session.write(record);
  }

  private fail(phase: FlowPhase, err: unknown, generation: number): void {
    if (!this.machine.isCurrentGeneration(generation)) {
      return;
    }
    if (isAbortError(err)) {
      return;
    }
    const mapped = err instanceof FlowError ? err : flowErrorFromCode("INTERNAL_ERROR");
    this.errorText = mapped.userMessage;
    try {
      this.machine.transition(phase, generation);
    } catch {
      this.machine.resetToIdle();
    }
    if (isTerminalPhase(this.machine.current)) {
      this.clearGrantState();
      this.session.clear();
      this.token = null;
    }
    this.machine.endAction();
    this.emit();
  }

  private returnToReady(generation: number): void {
    if (!this.machine.isCurrentGeneration(generation)) {
      return;
    }
    if (this.machine.current === "saving") {
      this.machine.transition("ready", generation);
    }
    this.machine.endAction();
    this.persist();
    this.emit();
  }

  private clearResumeDownloadId(): void {
    this.resumeDownloadId = null;
  }

  private clearGrantRefreshTimer(): void {
    if (this.grantRefreshTimer !== null) {
      clearTimeout(this.grantRefreshTimer);
      this.grantRefreshTimer = null;
    }
  }

  private clearGrantState(): void {
    this.grantAbort?.abort();
    this.grantAbort = null;
    this.clearGrantRefreshTimer();
    this.grantArming = false;
    this.grantNeedsRetry = false;
    this.grantExpiresAt = null;
    this.downloadPath = null;
    this.nativeDownloadHandoff = false;
    this.grantGeneration += 1;
  }

  private grantStillValid(safetyMs = 0): boolean {
    if (!this.grantExpiresAt || !this.downloadPath) {
      return false;
    }
    const expiresMs = Date.parse(this.grantExpiresAt);
    return Number.isFinite(expiresMs) && expiresMs - safetyMs > this.now();
  }

  private invalidateExpiredGrantHref(): void {
    if (this.grantStillValid(GRANT_HANDOFF_SAFETY_MS)) {
      return;
    }
    this.clearGrantRefreshTimer();
    this.downloadPath = null;
    this.grantExpiresAt = null;
    this.nativeDownloadHandoff = false;
    if (this.machine.current === "ready" && this.secureContext()) {
      this.grantNeedsRetry = true;
    }
  }

  private scheduleGrantRefresh(generation: number): void {
    this.clearGrantRefreshTimer();
    if (!this.grantExpiresAt || this.machine.current !== "ready") {
      return;
    }
    const expiresMs = Date.parse(this.grantExpiresAt);
    if (!Number.isFinite(expiresMs)) {
      return;
    }
    const delay = computeGrantRefreshDelayMs(expiresMs, this.now());
    if (delay === null) {
      // Already expired — never setTimeout(0). Resume/click/retry re-arms.
      this.invalidateExpiredGrantHref();
      this.emit();
      return;
    }
    this.grantRefreshTimer = setTimeout(() => {
      this.grantRefreshTimer = null;
      void this.armNativeDownload(generation);
    }, delay);
  }

  disconnect(): void {
    this.abort?.abort();
    this.grantAbort?.abort();
    this.clearGrantRefreshTimer();
  }

  onPageHide(): void {
    this.persist();
    this.abort?.abort();
    this.grantAbort?.abort();
    this.clearGrantRefreshTimer();
  }

  onForegroundResume(): void {
    if (this.machine.current !== "ready") {
      return;
    }
    this.invalidateExpiredGrantHref();
    this.emit();
    if (!this.secureContext() || this.grantArming) {
      return;
    }
    if (!this.grantStillValid(GRANT_REISSUE_BUFFER_MS)) {
      void this.armNativeDownload();
    } else {
      this.scheduleGrantRefresh(this.machine.generationId);
    }
  }

  onPageShow(persisted: boolean): Promise<void> {
    if (!persisted) {
      this.onForegroundResume();
      return Promise.resolve();
    }
    this.abort?.abort();
    this.abort = null;
    this.clearGrantState();
    this.token = null;
    this.mediaJob = null;
    this.downloadJob = null;
    this.selectedFormatId = null;
    this.errorText = null;
    this.restored = false;
    this.clearResumeDownloadId();
    this.machine.resetToIdle();
    return this.restore();
  }

  startOver(): void {
    this.machine.resetToIdle();
    this.abort?.abort();
    this.abort = null;
    this.clearGrantState();
    this.token = null;
    this.mediaJob = null;
    this.downloadJob = null;
    this.selectedFormatId = null;
    this.errorText = null;
    this.restored = false;
    this.clearResumeDownloadId();
    this.session.clear();
    this.emit();
  }

  restore(): Promise<void> {
    const record = this.session.read(this.now());
    if (!record) {
      return Promise.resolve();
    }
    if (!record.mediaJobId) {
      this.session.clear();
      return Promise.resolve();
    }
    this.token = record.token;
    this.selectedFormatId = record.formatOptionId;
    this.resumeDownloadId = record.downloadJobId;
    this.restored = true;
    return this.resume(record);
  }

  private async resume(record: RecoveryRecord): Promise<void> {
    if (!this.machine.beginAction()) {
      return;
    }
    this.machine.transition("submitting");
    const generation = this.machine.generationId;
    this.abort = new AbortController();
    try {
      const job = await this.api.getInspectionJob(
        record.mediaJobId!,
        record.token,
        this.abort.signal,
      );
      if (!this.machine.isCurrentGeneration(generation)) {
        return;
      }
      this.mediaJob = job;
      this.machine.transition("inspecting", generation);
      await this.pollInspection(generation);
    } catch (err) {
      this.clearResumeDownloadId();
      this.fail("network_error", err, generation);
    }
  }

  async submit(url: string): Promise<void> {
    if (!this.machine.beginAction()) {
      return;
    }
    this.errorText = null;
    this.mediaJob = null;
    this.downloadJob = null;
    this.selectedFormatId = null;
    this.clearGrantState();
    this.clearResumeDownloadId();
    try {
      this.machine.transition("submitting");
    } catch {
      this.machine.endAction();
      return;
    }
    const generation = this.machine.generationId;
    this.abort?.abort();
    this.abort = new AbortController();
    this.token = this.generateToken();
    this.emit();
    try {
      const job = await this.api.createInspectionJob(
        url,
        this.token,
        this.abort.signal,
      );
      if (!this.machine.isCurrentGeneration(generation)) {
        return;
      }
      this.mediaJob = job;
      this.persist();
      this.machine.transition("inspecting", generation);
      this.emit();
      await this.pollInspection(generation);
    } catch (err) {
      const code = err instanceof FlowError ? err.code : "NETWORK_ERROR";
      const phase =
        code === "UNSUPPORTED_PROVIDER" ||
        code === "WRAPPER_UNSUPPORTED" ||
        code === "WRAPPER_UNRESOLVED" ||
        code === "RESOLVED_PROVIDER_UNSUPPORTED"
          ? "unsupported"
          : code === "NETWORK_ERROR"
            ? "network_error"
            : "inspection_failed";
      this.fail(phase, err, generation);
    }
  }

  private async pollInspection(generation: number): Promise<void> {
    if (!this.token || !this.mediaJob) {
      throw flowErrorFromCode("INTERNAL_ERROR");
    }
    const token = this.token;
    const jobId = this.mediaJob.id;
    const job = await pollUntilTerminal({
      read: (signal) => this.api.getInspectionJob(jobId, token, signal),
      isTerminal: (value) =>
        value.state === "inspected" ||
        value.state === "failed" ||
        value.state === "expired",
      expiresAt: () => this.mediaJob?.expiresAt ?? valueExpires(),
      signal: this.abort!.signal,
      hidden: this.documentHidden,
      semanticKey: (value) =>
        jobProgressSemanticKey({
          state: value.state,
          progressStage: null,
          progressPercent: null,
          attempt: null,
          updatedAt: value.updatedAt,
        }),
      onTick: (value) => {
        if (!this.machine.isCurrentGeneration(generation)) {
          return;
        }
        this.mediaJob = value;
        this.persist();
        this.emit();
      },
    });
    if (!this.machine.isCurrentGeneration(generation)) {
      return;
    }
    this.mediaJob = job;
    if (job.state === "expired") {
      this.clearResumeDownloadId();
      this.fail("expired", flowErrorFromCode("JOB_EXPIRED"), generation);
      return;
    }
    if (job.state === "failed") {
      this.clearResumeDownloadId();
      this.fail("inspection_failed", flowErrorFromCode(job.errorCode), generation);
      return;
    }
    this.machine.transition("inspected", generation);
    const formats = job.result?.formats ?? [];
    const muxingBlocked = job.result?.muxingRequired === true;
    this.selectedFormatId = reconcileSelectedFormatId(formats, this.selectedFormatId, {
      muxingBlocked,
    });
    if (!this.selectedFormatId) {
      this.selectedFormatId = pickHighestEligibleFormat(formats, { muxingBlocked });
    }
    const resumeId = this.resumeDownloadId;
    this.clearResumeDownloadId();
    if (resumeId && this.token) {
      try {
        const download = await this.api.getDownloadJob(
          resumeId,
          this.token,
          this.abort!.signal,
        );
        if (!this.machine.isCurrentGeneration(generation)) {
          return;
        }
        this.downloadJob = download;
        this.selectedFormatId = download.formatOptionId;
        this.machine.transition("enqueueing_download", generation);
        this.machine.transition("downloading", generation);
        this.emit();
        await this.pollDownload(generation);
        return;
      } catch (err) {
        this.downloadJob = null;
        if (!this.machine.isCurrentGeneration(generation)) {
          return;
        }
        this.machine.endAction();
        this.persist();
        this.emit();
        if (isAbortError(err)) {
          return;
        }
        this.errorText =
          err instanceof FlowError
            ? err.userMessage
            : userMessageForCode("INTERNAL_ERROR").text;
        this.emit();
        return;
      }
    }
    this.machine.endAction();
    this.persist();
    this.emit();
  }

  selectFormat(formatOptionId: string): void {
    if (this.machine.current !== "inspected") {
      return;
    }
    if (!projectCapabilityUi(this.mediaJob?.providerCapabilities).canSelectQuality) {
      return;
    }
    const format = this.mediaJob?.result?.formats.find(
      (item) => item.formatOptionId === formatOptionId,
    );
    if (
      !format ||
      !isDownloadEligible(format) ||
      this.mediaJob?.result?.muxingRequired
    ) {
      return;
    }
    this.selectedFormatId = format.formatOptionId;
    this.persist();
    this.emit();
  }

  async enqueueDownload(): Promise<void> {
    const snapshot: FlowPhase = this.machine.current;
    if (snapshot !== "inspected" || !this.machine.beginAction()) {
      return;
    }
    const format = this.mediaJob?.result?.formats.find(
      (item) => item.formatOptionId === this.selectedFormatId,
    );
    if (
      !this.token ||
      !this.mediaJob ||
      !projectCapabilityUi(this.mediaJob.providerCapabilities).canDownloadVideo ||
      !format ||
      !isDownloadEligible(format) ||
      this.mediaJob.result?.muxingRequired
    ) {
      this.machine.endAction();
      return;
    }
    this.errorText = null;
    this.clearGrantState();
    try {
      this.machine.transition("enqueueing_download");
    } catch {
      this.machine.endAction();
      return;
    }
    const generation = this.machine.generationId;
    this.abort?.abort();
    this.abort = new AbortController();
    this.emit();
    try {
      const job = await this.api.createDownloadJob(
        this.mediaJob.id,
        format.formatOptionId,
        this.token,
        this.abort.signal,
      );
      if (!this.machine.isCurrentGeneration(generation)) {
        return;
      }
      this.downloadJob = job;
      this.machine.transition("downloading", generation);
      this.persist();
      this.emit();
      await this.pollDownload(generation);
    } catch (err) {
      if (!this.machine.isCurrentGeneration(generation)) {
        return;
      }
      if (this.machine.current === "cancelled") {
        return;
      }
      if (isAbortError(err) && this.abort?.signal.aborted === true) {
        return;
      }
      this.fail("download_failed", err, generation);
    }
  }

  private async pollDownload(generation: number): Promise<void> {
    if (!this.token || !this.downloadJob) {
      throw flowErrorFromCode("INTERNAL_ERROR");
    }
    const token = this.token;
    const jobId = this.downloadJob.id;
    const job = await pollUntilTerminal({
      read: (signal) => this.api.getDownloadJob(jobId, token, signal),
      isTerminal: (value) =>
        value.state === "ready" ||
        value.state === "failed" ||
        value.state === "cancelled" ||
        value.state === "expired",
      expiresAt: () => this.downloadJob?.expiresAt ?? valueExpires(),
      signal: this.abort!.signal,
      hidden: this.documentHidden,
      deadlineMs: 20 * 60_000,
      semanticKey: jobProgressSemanticKey,
      isActiveProgress: isActiveDownloadProgress,
      activeMaxIntervalMs: 1_000,
      onTick: (value) => {
        if (!this.machine.isCurrentGeneration(generation)) {
          return;
        }
        if (this.downloadJob && isStaleDownloadPoll(value, this.downloadJob)) {
          return;
        }
        this.downloadJob = value;
        this.persist();
        this.emit();
      },
    });
    if (!this.machine.isCurrentGeneration(generation)) {
      return;
    }
    this.downloadJob = job;
    if (job.state === "expired") {
      this.fail("expired", flowErrorFromCode("DOWNLOAD_EXPIRED"), generation);
      return;
    }
    if (job.state === "cancelled") {
      this.errorText = null;
      this.clearGrantState();
      this.machine.transition("cancelled", generation);
      this.session.clear();
      this.token = null;
      this.machine.endAction();
      this.emit();
      return;
    }
    if (job.state === "failed") {
      this.fail("download_failed", flowErrorFromCode(job.errorCode), generation);
      return;
    }
    this.nativeDownloadHandoff = false;
    this.machine.transition("ready", generation);
    this.machine.endAction();
    this.persist();
    this.emit();
    void this.armNativeDownload(generation);
  }

  async armNativeDownload(generation?: number): Promise<void> {
    const activeGeneration = generation ?? this.machine.generationId;
    if (this.machine.current !== "ready" || !this.secureContext()) {
      return;
    }
    if (!this.token || !this.downloadJob) {
      return;
    }
    // Collapse concurrent retries / refreshes into one in-flight request.
    if (this.grantArming) {
      return;
    }
    const now = this.now();
    if (
      this.downloadPath &&
      this.grantExpiresAt &&
      Date.parse(this.grantExpiresAt) - now > GRANT_REISSUE_BUFFER_MS
    ) {
      this.grantNeedsRetry = false;
      this.scheduleGrantRefresh(activeGeneration);
      return;
    }
    const previousPath = this.downloadPath;
    const previousExpires = this.grantExpiresAt;
    const hadValidGrant = this.grantStillValid(GRANT_HANDOFF_SAFETY_MS);
    this.grantAbort?.abort();
    const grantAbort = new AbortController();
    this.grantAbort = grantAbort;
    const grantGeneration = ++this.grantGeneration;
    this.grantArming = true;
    // Clear href while re-arming so stale anchors are not clickable mid-flight.
    this.downloadPath = null;
    this.grantExpiresAt = null;
    this.emit();
    try {
      const grant = await this.api.createBrowserGrant(
        this.downloadJob.id,
        this.token,
        grantAbort.signal,
      );
      if (
        !this.machine.isCurrentGeneration(activeGeneration) ||
        grantGeneration !== this.grantGeneration ||
        this.machine.current !== "ready"
      ) {
        return;
      }
      this.downloadPath = grant.downloadPath;
      this.grantExpiresAt = grant.expiresAt;
      this.grantArming = false;
      this.grantNeedsRetry = false;
      this.errorText = null;
      this.emit();
      this.scheduleGrantRefresh(activeGeneration);
    } catch (err) {
      if (
        !this.machine.isCurrentGeneration(activeGeneration) ||
        grantGeneration !== this.grantGeneration
      ) {
        return;
      }
      if (isAbortError(err)) {
        // Ownership abort: restore prior valid grant if we still had one.
        this.grantArming = false;
        if (hadValidGrant && previousPath && previousExpires) {
          this.downloadPath = previousPath;
          this.grantExpiresAt = previousExpires;
          this.scheduleGrantRefresh(activeGeneration);
        }
        this.emit();
        return;
      }
      this.grantArming = false;
      // Failed re-arm must not destroy an already-valid grant.
      if (hadValidGrant && previousPath && previousExpires) {
        this.downloadPath = previousPath;
        this.grantExpiresAt = previousExpires;
        this.grantNeedsRetry = false;
        this.scheduleGrantRefresh(activeGeneration);
      } else {
        this.downloadPath = null;
        this.grantExpiresAt = null;
        this.grantNeedsRetry = true;
      }
      this.errorText =
        err instanceof FlowError
          ? err.userMessage
          : userMessageForCode("INTERNAL_ERROR").text;
      this.emit();
    }
  }

  /**
   * Validate a native download click. Returns true when the browser should
   * continue with genuine anchor navigation; false when the click must be
   * cancelled and the grant re-armed.
   */
  onNativeDownloadClick(): boolean {
    if (this.machine.current !== "ready" || this.grantArming) {
      return false;
    }
    if (
      !this.downloadPath ||
      !this.grantStillValid(GRANT_HANDOFF_SAFETY_MS)
    ) {
      this.invalidateExpiredGrantHref();
      this.emit();
      void this.armNativeDownload();
      return false;
    }
    this.nativeDownloadHandoff = true;
    this.persist();
    this.emit();
    return true;
  }

  retryGrantAccess(): void {
    if (this.machine.current !== "ready" || this.grantArming) {
      return;
    }
    this.errorText = null;
    this.grantNeedsRetry = true;
    this.emit();
    void this.armNativeDownload();
  }

  async saveFile(): Promise<void> {
    if (this.machine.current !== "ready" || !this.machine.beginAction()) {
      return;
    }
    if (!this.pickerSupported()) {
      this.machine.endAction();
      this.emit();
      return;
    }
    if (!this.token || !this.downloadJob) {
      this.machine.endAction();
      return;
    }
    this.errorText = null;
    try {
      this.machine.transition("saving");
    } catch {
      this.machine.endAction();
      return;
    }
    const generation = this.machine.generationId;
    this.abort?.abort();
    this.abort = new AbortController();
    this.emit();
    try {
      await this.save({
        downloadJobId: this.downloadJob.id,
        token: this.token,
        container: this.downloadJob.selectedFormat.container,
        suggestedFilename: this.downloadJob.suggestedFilename,
        signal: this.abort.signal,
      });
      if (!this.machine.isCurrentGeneration(generation)) {
        return;
      }
      this.clearGrantState();
      this.machine.transition("completed", generation);
      this.session.clear();
      this.token = null;
      this.machine.endAction();
      this.emit();
    } catch (err) {
      if (!this.machine.isCurrentGeneration(generation)) {
        return;
      }
      const controllerAborted = this.abort?.signal.aborted === true;
      if (err instanceof PickerCancelledError) {
        this.returnToReady(generation);
        return;
      }
      if (isAbortError(err) && controllerAborted) {
        return;
      }
      if (isAbortError(err)) {
        this.fail("download_failed", flowErrorFromCode("SAVE_FAILED"), generation);
        return;
      }
      this.fail("download_failed", err, generation);
    }
  }

  async cancelTask(): Promise<void> {
    if (!this.token || !this.downloadJob?.cancellable) {
      return;
    }
    if (this.machine.current !== "downloading" && this.machine.current !== "enqueueing_download") {
      return;
    }
    const generation = this.machine.generationId;
    try {
      const job = await this.api.cancelDownloadJob(
        this.downloadJob.id,
        this.token,
        this.abort?.signal,
      );
      if (!this.machine.isCurrentGeneration(generation)) {
        return;
      }
      this.downloadJob = job;
      this.emit();
      if (job.state === "cancelled") {
        this.errorText = null;
        this.clearGrantState();
        this.machine.transition("cancelled", generation);
        this.session.clear();
        this.token = null;
        this.machine.endAction();
        this.abort?.abort();
        this.emit();
      }
    } catch (err) {
      if (!this.machine.isCurrentGeneration(generation)) {
        return;
      }
      if (isAbortError(err)) {
        return;
      }
      this.errorText =
        err instanceof FlowError
          ? err.userMessage
          : userMessageForCode("INTERNAL_ERROR").text;
      this.emit();
    }
  }
}

function valueExpires(): string {
  return new Date(Date.now() + 60_000).toISOString();
}

export function isStaleDownloadPoll(
  incoming: DownloadJob,
  current: DownloadJob,
): boolean {
  if (incoming.attempt < current.attempt) {
    return true;
  }
  if (incoming.attempt > current.attempt) {
    return false;
  }
  const incomingMs = Date.parse(incoming.updatedAt);
  const currentMs = Date.parse(current.updatedAt);
  if (!Number.isFinite(incomingMs) || !Number.isFinite(currentMs)) {
    return true;
  }
  if (incomingMs < currentMs) {
    return true;
  }
  if (incomingMs > currentMs) {
    return false;
  }
  if (
    incoming.progressStage === current.progressStage &&
    incoming.progressPercent !== null &&
    current.progressPercent !== null &&
    incoming.progressPercent < current.progressPercent
  ) {
    return true;
  }
  return false;
}
