export function answerText(value: any): string {
  if (value && typeof value === "object" && !Array.isArray(value)) return (value.choices || []).join(", ") + ": " + value.text;
  return Array.isArray(value) ? value.join(", ") || "None selected" : String(value);
}

