export type CsvValue = string | number | boolean | null | undefined;

/** Quote one RFC 4180 cell and neutralize spreadsheet formula prefixes. */
export function csvCell(value: CsvValue): string {
  let text = value == null ? "" : String(value);
  if (typeof value === "string" && /^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

export function serializeCsv(headers: CsvValue[], rows: CsvValue[][]): string {
  return [headers, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

export function downloadCsv(filename: string, headers: CsvValue[], rows: CsvValue[][]): void {
  const url = URL.createObjectURL(
    new Blob([serializeCsv(headers, rows)], { type: "text/csv;charset=utf-8" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
