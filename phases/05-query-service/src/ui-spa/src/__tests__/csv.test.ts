import { describe, expect, it } from "vitest";
import { csvCell, serializeCsv } from "../lib/csv";

describe("safe CSV serialization", () => {
  it.each(["=1+1", "+cmd", "-2+3", "@SUM(A1:A2)", "\t=cmd", "\r=cmd"])(
    "neutralizes spreadsheet formula input %j",
    (value) => expect(csvCell(value)).toBe(`"'${value.replace(/"/g, '""')}"`),
  );

  it("quotes commas, quotes and newlines without changing numeric negatives", () => {
    expect(serializeCsv(["name", "value"], [["a,\"b\n", -2]])).toBe(
      '"name","value"\r\n"a,""b\n","-2"\r\n',
    );
  });
});
