import { MediaApi } from "./api";
import {
  isDownloadEligible,
  type DownloadJob,
  type InspectionJob,
  type InspectionResult,
  type MediaFormat,
  type ProgressStage,
  type FreeQuota,
  type PremiumStatus,
  isFormatOptionId,
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
import {
  pollUntilTerminal,
  jobProgressSemanticKey,
  isActiveDownloadProgress,
} from "./poller";
import { FlowSession, type RecoveryRecord } from "./session";
import {
  FlowMachine,
  isRestorablePhase,
  isTerminalPhase,
  type FlowPhase,
} from "./state-machine";
import {
  createIdempotencyKey,
  storePaymentOrder,
  submitServerPaymentForm,
} from "../premium/payment";

export type PremiumUiState = "loading" | "free" | "active" | "error";

function canUseFormat(format: MediaFormat, premiumActive: boolean): boolean {
  if (!isFormatOptionId(format.formatOptionId)) return false;
  if (format.mediaKind === "normal_video") return isDownloadEligible(format);
  if (!premiumActive || !format.requiresPremium) return false;
  return format.mediaKind === "video_only"
    ? format.hasVideo && !format.hasAudio
    : format.mediaKind === "audio_only" && format.hasAudio && !format.hasVideo;
}

export type FlowSnapshot = {
  phase: FlowPhase;
  statusText: string;
  errorText: string | null;
  result: InspectionResult | null;
  formats: MediaFormat[];
  selectedFormatId: string | null;
  selectedFormat?: MediaFormat | null;
  deliveryRateBytesPerSecond?: number | null;
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
  freeQuota?: FreeQuota | null;
  quotaLoading?: boolean;
  premiumState?: PremiumUiState;
  premiumStatus?: PremiumStatus | null;
  testCheckoutAvailable?: boolean;
  checkoutBusy?: boolean;
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
  createIdempotencyKey?: () => string;
  storePaymentOrder?: (orderId: string) => void;
  submitPaymentForm?: typeof submitServerPaymentForm;
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
  private readonly makeIdempotencyKey: () => string;
  private readonly savePaymentOrder: (orderId: string) => void;
  private readonly submitPaymentForm: typeof submitServerPaymentForm;

  private abort: AbortController | null = null;
  private quotaAbort = new AbortController();
  private closed = false;
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
  private freeQuota: FreeQuota | null = null;
  private quotaLoading = false;
  private quotaRefresh: Promise<void> | null = null;
  private premiumRefresh: Promise<void> | null = null;
  private paymentConfigRefresh: Promise<void> | null = null;
  private premiumState: PremiumUiState = "loading";
  private premiumStatus: PremiumStatus | null = null;
  private testCheckoutAvailable = false;
  private checkoutBusy = false;

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
    this.makeIdempotencyKey = hooks.createIdempotencyKey ?? createIdempotencyKey;
    this.savePaymentOrder = hooks.storePaymentOrder ?? storePaymentOrder;
    this.submitPaymentForm = hooks.submitPaymentForm ?? submitServerPaymentForm;
  }

  snapshot(): FlowSnapshot {
    const result = this.mediaJob?.state === "inspected" ? this.mediaJob.result : null;
    const formats = result?.formats ?? [];
    const muxingBlocked = result?.muxingRequired === true;
    const capabilities = projectCapabilityUi(this.mediaJob?.providerCapabilities);
    const selected = formats.find((f) => f.formatOptionId === this.selectedFormatId);
    const selectedFormat = this.downloadJob?.selectedFormat ?? selected ?? null;
    const deliveryRateBytesPerSecond =
      this.downloadJob?.deliveryRateBytesPerSecond ?? null;
    const downloadEligible =
      (selected?.mediaKind === "audio_only"
        ? capabilities.canExtractAudio
        : capabilities.canDownloadVideo) &&
      !muxingBlocked &&
      selected !== undefined &&
      canUseFormat(selected, this.premiumState === "active");
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
      selectedFormat,
      deliveryRateBytesPerSecond,
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
      freeQuota: this.freeQuota,
      quotaLoading: this.quotaLoading,
      premiumState: this.premiumState,
      premiumStatus: this.premiumStatus,
      testCheckoutAvailable: this.testCheckoutAvailable,
      checkoutBusy: this.checkoutBusy,
    };
  }

  async initializeAccount(): Promise<void> {
    await this.refreshQuota(true);
    await Promise.all([this.refreshPremium(true), this.refreshPaymentConfig(true)]);
  }

  async initializeQuota(): Promise<void> {
    await this.refreshQuota(true);
  }

  async refreshPremium(silent = true): Promise<void> {
    if (this.premiumRefresh !== null) {
      await this.premiumRefresh;
      return;
    }
    if (this.closed || typeof this.api.getPremiumStatus !== "function") return;
    const run = async () => {
      this.premiumState = "loading";
      this.emit();
      try {
        const status = await this.api.getPremiumStatus(this.quotaAbort.signal);
        if (this.closed) return;
        this.premiumStatus = status;
        this.premiumState = status.active ? "active" : "free";
      } catch (err) {
        if (isAbortError(err) || this.closed) return;
        this.premiumStatus = null;
        this.premiumState = "error";
        if (!silent) this.errorText = "Не удалось обновить статус Premium.";
      } finally {
        this.emit();
      }
    };
    this.premiumRefresh = run();
    try {
      await this.premiumRefresh;
    } finally {
      this.premiumRefresh = null;
    }
  }

  private async refreshPaymentConfig(silent = true): Promise<void> {
    if (this.paymentConfigRefresh !== null) {
      await this.paymentConfigRefresh;
      return;
    }
    if (this.closed || typeof this.api.getPaymentConfig !== "function") return;
    const run = async () => {
      try {
        const config = await this.api.getPaymentConfig(this.quotaAbort.signal);
        if (!this.closed) this.testCheckoutAvailable = config.testCheckoutAvailable;
      } catch (err) {
        if (!isAbortError(err) && !this.closed) {
          this.testCheckoutAvailable = false;
          if (!silent) this.errorText = "Тестовая оплата сейчас недоступна.";
        }
      } finally {
        this.emit();
      }
    };
    this.paymentConfigRefresh = run();
    try {
      await this.paymentConfigRefresh;
    } finally {
      this.paymentConfigRefresh = null;
    }
  }

  async startTestCheckout(): Promise<void> {
    if (
      this.checkoutBusy ||
      !this.testCheckoutAvailable ||
      this.premiumState !== "free" ||
      typeof this.api.createPaymentOrder !== "function"
    ) return;
    this.checkoutBusy = true;
    this.errorText = null;
    this.emit();
    try {
      const created = await this.api.createPaymentOrder(
        this.makeIdempotencyKey(),
        this.quotaAbort.signal,
      );
      if (this.closed) return;
      this.savePaymentOrder(created.orderId);
      this.submitPaymentForm(created);
    } catch (err) {
      if (isAbortError(err) || this.closed) return;
      this.errorText = err instanceof FlowError
        ? err.userMessage
        : "Не удалось начать тестовую оплату. Попробуйте ещё раз.";
      await this.refreshPaymentConfig(true);
    } finally {
      this.checkoutBusy = false;
      this.emit();
    }
  }

  private async refreshQuota(silent: boolean): Promise<void> {
    if (this.quotaRefresh !== null) {
      await this.quotaRefresh;
      return;
    }
    if (this.closed) {
      return;
    }
    // Older focused test doubles predate the optional quota endpoint. The real
    // MediaApi always provides it; preserving this guard keeps unrelated flow
    // tests scoped to their original contracts.
    if (typeof this.api.getFreeQuota !== "function") {
      return;
    }
    const run = async () => {
      if (this.closed) {
        return;
      }
      this.quotaLoading = true;
      this.emit();
      try {
        const next = await this.api.getFreeQuota(this.quotaAbort.signal);
        if (this.closed) {
          return;
        }
        this.freeQuota = next;
      } catch (err) {
        if (isAbortError(err) || this.closed) {
          return;
        }
        if (!silent) {
          throw err;
        }
      } finally {
        this.quotaLoading = false;
        this.emit();
      }
    };
    this.quotaRefresh = run();
    try {
      await this.quotaRefresh;
    } finally {
      this.quotaRefresh = null;
    }
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
        return "Готовим безопасное скачивание…";
      }
      if (this.nativeDownloadHandoff && this.grantStillValid(GRANT_HANDOFF_SAFETY_MS)) {
        return "Отправлено в браузер";
      }
      if (this.grantNeedsRetry || this.downloadPath === null) {
        return "Нужно обновить доступ к скачиванию.";
      }
    }
    if (this.restored && this.machine.current === "inspected") {
      return "Восстановлена текущая задача. Выберите качество или продолжите.";
    }
    if (this.restored && this.machine.current === "downloading") {
      return (
        flowStatusText(this.machine.current, this.downloadJob?.progressStage ?? null, {
          progressPercent: this.downloadJob?.progressPercent ?? null,
          artifactBytes: this.downloadJob?.artifactBytes ?? null,
          formats: this.mediaJob?.result?.formats ?? [],
        }) || "Восстановлена текущая задача."
      );
    }
    if (this.machine.current === "cancelled") {
      return STAGE_LABEL.cancelled;
    }
    return flowStatusText(
      this.machine.current,
      this.downloadJob?.progressStage ?? null,
      {
        progressPercent: this.downloadJob?.progressPercent ?? null,
        artifactBytes: this.downloadJob?.artifactBytes ?? null,
        formats: this.mediaJob?.result?.formats ?? [],
      },
    );
  }

  private emit(): void {
    if (this.closed) {
      return;
    }
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

  private returnToReady(generation: number, errorText: string | null = null): void {
    if (!this.machine.isCurrentGeneration(generation)) {
      return;
    }
    this.errorText = errorText;
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
    this.closed = true;
    this.quotaAbort.abort();
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
    void this.refreshPremium(true);
    void this.refreshPaymentConfig(true);
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

  async onPageShow(persisted: boolean): Promise<void> {
    if (!persisted) {
      this.onForegroundResume();
      return;
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
    await Promise.all([this.restore(), this.initializeAccount()]);
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
      (format.mediaKind === "audio_only"
        ? !projectCapabilityUi(this.mediaJob?.providerCapabilities).canExtractAudio
        : !projectCapabilityUi(this.mediaJob?.providerCapabilities).canDownloadVideo) ||
      !canUseFormat(format, this.premiumState === "active") ||
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
      (format?.mediaKind === "audio_only"
        ? !projectCapabilityUi(this.mediaJob.providerCapabilities).canExtractAudio
        : !projectCapabilityUi(this.mediaJob.providerCapabilities).canDownloadVideo) ||
      !format ||
      !canUseFormat(format, this.premiumState === "active") ||
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
      await this.refreshQuota(false);
      if (this.freeQuota?.tier === "free" && this.freeQuota.downloadsRemaining === 0) {
        throw flowErrorFromCode("FREE_DOWNLOAD_QUOTA_EXHAUSTED");
      }
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
      if (err instanceof FlowError && err.code === "MEDIA_CAPABILITY_REQUIRES_PREMIUM") {
        await this.refreshPremium(true);
        this.errorText = "Срок Premium истёк или доступ не подтверждён. Обновите статус и попробуйте снова.";
        this.machine.transition("inspected", generation);
        this.machine.endAction();
        this.emit();
        return;
      }
      this.fail("download_failed", err, generation);
      void this.refreshQuota(true);
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
      void this.refreshQuota(true);
      this.fail("expired", flowErrorFromCode("DOWNLOAD_EXPIRED"), generation);
      return;
    }
    if (job.state === "cancelled") {
      void this.refreshQuota(true);
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
      void this.refreshQuota(true);
      this.fail("download_failed", flowErrorFromCode(job.errorCode), generation);
      return;
    }
    this.nativeDownloadHandoff = false;
    this.machine.transition("ready", generation);
    this.machine.endAction();
    this.persist();
    this.emit();
    void this.refreshQuota(true);
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
    if (!this.downloadPath || !this.grantStillValid(GRANT_HANDOFF_SAFETY_MS)) {
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
        expectedArtifactBytes: this.downloadJob.artifactBytes,
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
      if (err instanceof FlowError && err.code === "DOWNLOAD_EXPIRED") {
        this.fail("expired", err, generation);
        return;
      }
      // The server-side job was already READY before this local FSA attempt.
      // A fetch, stream, metadata, permission, write, or close failure must not
      // discard that prepared artifact or its ordinary browser-download grant.
      this.returnToReady(generation, userMessageForCode("SAVE_FAILED").text);
    }
  }

  async cancelTask(): Promise<void> {
    if (!this.token || !this.downloadJob?.cancellable) {
      return;
    }
    if (
      this.machine.current !== "downloading" &&
      this.machine.current !== "enqueueing_download"
    ) {
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
        void this.refreshQuota(true);
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
