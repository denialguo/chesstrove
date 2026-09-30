// Named-mate form labels: Textbook and Variant are badged, Characteristic (the normal form) is not.
// Run: npm run check
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { FORM_HELP, FORM_NAME, MATE_FORMS, formBadge, formNote } from "../src/lib/motifs";

assert.deepEqual(MATE_FORMS, ["textbook", "characteristic", "variant"]);
assert.equal(formBadge("textbook"), "Textbook");
assert.equal(formBadge("characteristic"), null);
assert.equal(formBadge("variant"), "Variant");
assert.equal(formNote({ form: "variant", short_of: ["no_extra_helpers"] })?.note, "Other pieces help close the net.");
assert.equal(formNote({}), null);

// no retired tier names in anything a player can read
const stale = /\b(canonical|core|classic|pure|standard)\b/i;
for (const s of [...Object.values(FORM_NAME), ...Object.values(FORM_HELP)]) assert.doesNotMatch(s, stale, s);
for (const dir of ["src/pages", "src/components"]) {
  for (const f of readdirSync(dir)) {
    const text = readFileSync(`${dir}/${f}`, "utf8");
    assert.doesNotMatch(text, /Classic form|Core form|"canonical"|\bcanonical\b/, `${dir}/${f}`);
  }
}
console.log("forms check ok");
