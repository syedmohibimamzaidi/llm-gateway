export function normalizeGatewayKey(raw) {
  let value = raw.trim()
  const assignment = /^(?:export\s+)?GATEWAY_API_KEY\s*=\s*(.*)$/.exec(value)
  if (assignment) value = assignment[1].trim()
  if (value.length >= 2 && value[0] === value.at(-1) && (value[0] === '"' || value[0] === "'")) {
    value = value.slice(1, -1)
  }
  return value
}
