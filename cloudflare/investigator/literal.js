// Match eligible value spans in a frozen excerpt. Pandect uses Unicode code-point
// offsets. JavaScript RegExp returns UTF-16 positions, including for astral text.
const wordClass = "[\\p{Letter}\\p{Number}_]";
const word = new RegExp(wordClass, "u");

export function literalOffsets({ literal, records }) {
  const chars = Array.from(literal);
  if (!chars.length || chars.length > 80 || records.length > 4) {
    throw new Error("Invalid literal review");
  }
  const left = word.test(chars[0]);
  const right = word.test(chars.at(-1));
  // Preserve the Unicode case-insensitive equivalence class used in saved
  // reviews for dotted and dotless i. All other metacharacters are literals.
  const pattern = chars.map(ch =>
    "iIıİ".includes(ch) ? "[iIıİ]" : ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
  ).join("");
  const regex = new RegExp(
    (left ? `(?<!${wordClass})` : "") + pattern + (right ? `(?!${wordClass})` : ""),
    "giu",
  );
  return records.map(({ text, start, length, ranges }) => {
    const positions = new Map([[0, 0]]);
    let units = 0;
    let points = 0;
    for (const ch of text) {
      units += ch.length;
      points += 1;
      positions.set(units, points);
    }
    const offsets = [];
    regex.lastIndex = 0;
    let match;
    while ((match = regex.exec(text)) !== null) {
      const offset = positions.get(match.index);
      const from = start + offset;
      const end = from + chars.length;
      const clipped = (left && offset === 0 && start !== 0) ||
        (right && offset + chars.length === points && end < length);
      if (!clipped && ranges.some(([a, b]) => a <= from && end <= b)) {
        offsets.push(offset);
        if (offsets.length === 4) break;
      } else {
        // A rejected match can overlap an eligible match at the start of a
        // retained field. Resume one code point after its start.
        regex.lastIndex = match.index + (text.codePointAt(match.index) > 0xffff ? 2 : 1);
      }
    }
    return offsets;
  });
}
