export const FLOW_PHASES = [
  "idle",
  "submitting",
  "inspecting",
  "inspected",
  "enqueueing_download",
  "downloading",
  "ready",
  "saving",
  "completed",
  "inspection_failed",
  "download_failed",
  "expired",
  "unsupported",
  "cancelled",
  "network_error",
  "internal_error",
] as const;

export type FlowPhase = (typeof FLOW_PHASES)[number];

const TERMINAL: ReadonlySet<FlowPhase> = new Set([
  "completed",
  "inspection_failed",
  "download_failed",
  "expired",
  "unsupported",
  "cancelled",
  "network_error",
  "internal_error",
]);

const ALLOWED: Record<FlowPhase, readonly FlowPhase[]> = {
  idle: ["submitting"],
  submitting: [
    "inspecting",
    "inspection_failed",
    "unsupported",
    "network_error",
    "internal_error",
    "cancelled",
    "expired",
  ],
  inspecting: [
    "inspected",
    "inspection_failed",
    "expired",
    "network_error",
    "internal_error",
    "cancelled",
    "unsupported",
  ],
  inspected: ["enqueueing_download", "cancelled", "expired", "unsupported"],
  enqueueing_download: [
    "downloading",
    "download_failed",
    "network_error",
    "internal_error",
    "cancelled",
    "expired",
    "unsupported",
  ],
  downloading: [
    "ready",
    "download_failed",
    "expired",
    "network_error",
    "internal_error",
    "cancelled",
  ],
  ready: ["saving", "cancelled", "expired"],
  saving: [
    "completed",
    "ready",
    "download_failed",
    "cancelled",
    "internal_error",
    "expired",
    "unsupported",
  ],
  completed: [],
  inspection_failed: ["idle"],
  download_failed: ["idle"],
  expired: ["idle"],
  unsupported: ["idle"],
  cancelled: ["idle"],
  network_error: ["idle"],
  internal_error: ["idle"],
};

export class IllegalTransitionError extends Error {
  constructor(from: FlowPhase, to: FlowPhase) {
    super("Illegal flow transition.");
    this.name = "IllegalTransitionError";
    void from;
    void to;
  }
}

export function isTerminalPhase(phase: FlowPhase): boolean {
  return TERMINAL.has(phase);
}

const RESTORABLE: ReadonlySet<FlowPhase> = new Set([
  "submitting",
  "inspecting",
  "inspected",
  "enqueueing_download",
  "downloading",
  "ready",
  "saving",
]);

export function isRestorablePhase(phase: FlowPhase): boolean {
  return RESTORABLE.has(phase);
}

export function canTransition(from: FlowPhase, to: FlowPhase): boolean {
  return ALLOWED[from].includes(to);
}

export class FlowMachine {
  private phase: FlowPhase = "idle";
  private generation = 0;
  private busy = false;

  get current(): FlowPhase {
    return this.phase;
  }

  get generationId(): number {
    return this.generation;
  }

  isBusy(): boolean {
    return this.busy;
  }

  beginAction(): boolean {
    if (this.busy) {
      return false;
    }
    this.busy = true;
    return true;
  }

  endAction(): void {
    this.busy = false;
  }

  transition(to: FlowPhase, generation?: number): boolean {
    if (generation !== undefined && generation !== this.generation) {
      return false;
    }
    if (!canTransition(this.phase, to)) {
      throw new IllegalTransitionError(this.phase, to);
    }
    this.phase = to;
    if (to === "submitting") {
      this.generation += 1;
    }
    if (to === "idle") {
      this.busy = false;
    }
    return true;
  }

  isCurrentGeneration(generation: number): boolean {
    return generation === this.generation;
  }

  resetToIdle(): void {
    this.phase = "idle";
    this.busy = false;
    this.generation += 1;
  }
}
