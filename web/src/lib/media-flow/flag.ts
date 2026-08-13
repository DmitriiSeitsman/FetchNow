/** Build-time public UI flag. Only the exact string "true" enables the flow. */

export function parseMediaFlowFlag(raw: string | boolean | undefined): boolean {
  return raw === true || raw === "true";
}

export function isMediaFlowEnabled(
  raw: string | boolean | undefined = import.meta.env.PUBLIC_MEDIA_FLOW_ENABLED,
): boolean {
  return parseMediaFlowFlag(raw);
}
