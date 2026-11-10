// Drives public/index.html in jsdom against a real local server that uses a fake model.
//
//     cd ui_test && npm install && node test_ui.mjs
//
// The stub server runs under $PYTHON if set, else the repo's .venv/bin/python, else python3.
//
// jsdom is not a browser: there is no layout and no real download. It runs the page's
// JavaScript for real, against the real API (triage, the confirm screen, annotation), which is
// what this checks. Some cases replace one endpoint with a canned response to reach a state.

import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const FIX = join(ROOT, "fixtures");
const VENV_PYTHON = join(ROOT, ".venv", "bin", "python");
const PYTHON = process.env.PYTHON || (existsSync(VENV_PYTHON) ? VENV_PYTHON : "python3");
const TRUST = "00_marlowe_trust_original.pdf";
const MARLOWE = [TRUST, "01_marlowe_first_amendment.pdf", "02_marlowe_second_amendment.pdf",
  "03_marlowe_third_amendment_and_restatement.pdf", "04_marlowe_fourth_amendment.pdf"];

let passed = 0;
function check(condition, message) {
  if (!condition) throw new Error(`FAILED: ${message}`);
  passed += 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pdf = (name) => new File([readFileSync(join(FIX, name))], name, { type: "application/pdf" });

async function startServer(port, env = {}) {
  const proc = spawn(PYTHON, ["dev_stub_server.py", "--no-browser"], {
    cwd: ROOT,
    env: { ...process.env, PORT: String(port), STUB_PAUSE_SECONDS: "0.1", CANETRUST_DISABLE_DOTENV: "1", ...env },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let stderr = "";
  proc.stderr.on("data", (d) => (stderr += d));
  for (let i = 0; i < 80; i++) {
    try {
      if ((await fetch(`http://127.0.0.1:${port}/api/health`)).ok) return { proc, stderr: () => stderr };
    } catch {}
    await sleep(150);
  }
  proc.kill();
  throw new Error(`server did not start: ${stderr}`);
}

function ndjson(lines, { close = true } = {}) {
  let controller;
  const encoder = new TextEncoder();
  const body = new ReadableStream({ start(c) { controller = c; for (const l of lines) c.enqueue(encoder.encode(JSON.stringify(l) + "\n")); if (close) c.close(); } });
  return { response: new Response(body, { status: 200, headers: { "content-type": "application/x-ndjson" } }),
           push: (l) => controller.enqueue(encoder.encode(JSON.stringify(l) + "\n")), end: () => controller.close() };
}

// overrides: { triage: async (url, opts) => Response, annotate: ... }; the rest hits the server.
async function openPage(port, overrides = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (e) => errors.push(String(e.message || e)));
  virtualConsole.on("error", (e) => errors.push(String(e)));
  const base = `http://127.0.0.1:${port}`;
  const blobs = [], downloads = [], sent = [];

  const dom = await JSDOM.fromURL(`${base}/`, {
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole,
    beforeParse(window) {
      window.fetch = (url, opts) => {
        const target = new URL(url, base).href;
        const which = target.includes("/api/triage") ? "triage" : target.includes("/api/annotate") ? "annotate" : null;
        if (which) sent.push({ which, url: target, opts });
        return (which && overrides[which] ? overrides[which] : fetch)(target, opts);
      };
      window.FormData = FormData;
      window.Blob = Blob;
      window.File = File;
      window.TextDecoder = TextDecoder;
      window.URL.createObjectURL = (blob) => { blobs.push(blob); return `blob:fake-${blobs.length}`; };
      window.URL.revokeObjectURL = () => {};
      window.HTMLAnchorElement.prototype.click = function () { downloads.push({ href: this.getAttribute("href"), name: this.getAttribute("download") }); };
    },
  });
  await sleep(300);
  const doc = dom.window.document;
  const $ = (id) => doc.getElementById(id);
  const fire = (el, type) => el.dispatchEvent(new dom.window.Event(type, { bubbles: true }));
  const until = async (cond, ms = 10000) => { for (let i = 0; i < ms / 50; i++) { if (cond()) return true; await sleep(50); } return false; };

  const page = {
    dom, doc, $, errors, blobs, downloads, sent, fire, until,
    choose: (...files) => { Object.defineProperty($("files"), "files", { value: files, configurable: true }); fire($("files"), "change"); },
    submit: async () => {
      $("form").dispatchEvent(new dom.window.Event("submit", { cancelable: true, bubbles: true }));
      await until(() => !$("confirm").hidden || !$("result").hidden || !$("error").hidden);
    },
    annotate: async () => { $("annotate").click(); await until(() => !$("result").hidden || !$("error").hidden); },
    rows: () => [...doc.querySelectorAll("#docs .doc")],
    row: (id) => doc.querySelector(`#docs .doc[data-id="${id}"]`),
    steps: () => [...doc.querySelectorAll("#steps li")].map((li) => li.textContent),
    error: () => ($("error").hidden ? null : $("error").textContent),
    resultShown: () => !$("result").hidden,
    lastBody: () => JSON.parse(sent.filter((s) => s.which === "annotate").at(-1).opts.body),
  };
  return page;
}

function setSelect(page, el, value) { el.value = value; page.fire(el, "change"); }

// A canned triage answer with flags, a four-sheet document and two principals.
function cannedTriage({ matters = false } = {}) {
  const doc = (id, file, first, last, type, date, extra = {}) => ({
    id, file_id: file, first_sheet: first, last_sheet: last, doc_type: type, title: `${type} title`, principal: "P",
    date, refers_to: [], revokes: [], appears_to_gift_to_university: "no", treatment: type === "accounting" ? "log_only" : "annotate",
    treatment_reason: "default", confidence: "high", excluded: false, amends: null, superseded: false, note: "", flags: [], labels: [], ...extra });
  const plan = {
    version: 1, name: "Canned", needs_attention: false, edits: [],
    files: [{ file_id: "a.txt", filename: "a.txt", sheets: 4 }, { file_id: "b.txt", filename: "b.txt", sheets: 2 }],
    flags: matters ? [{ code: "possible_multiple_matters", document: null, detail: "2 distinct principals" }] : [],
    documents: [
      doc("d1", "a.txt", 1, 4, "trust", "2001-01-01"),
      doc("d2", "b.txt", 1, 1, "amendment", "2002-02-02"),
      doc("d3", "b.txt", 2, 2, "accounting", null, { confidence: "low", flags: [{ code: "low_confidence", document: "d3", detail: "b.txt sheet 2" }] }),
    ],
  };
  const pages = { "a.txt": ["one", "two", "three", "four"], "b.txt": ["one", "two"] };
  return () => Promise.resolve(ndjson([{ type: "progress", message: "Files accepted." },
    { type: "triaged", plan, pages, skip_confirm: false, warnings: [] }]).response);
}

const server = await startServer(8765);
const gated = await startServer(8766, { CANETRUST_ACCESS_CODE: "sesame" });
try {
  // ---------------------------------------------------------------- initial state and the look
  let p = await openPage(8765);
  check(p.doc.querySelector("header h1").textContent === "CaneTrust AI", "header: CaneTrust AI");
  check(p.doc.querySelector("header h1 span").textContent === "Trust", "header: the orange accent word");
  check(p.doc.querySelector("footer").textContent.trim() === "Miami AI & Law Lab", "footer is kept");
  check(p.$("files").multiple === true && p.$("drop").textContent.includes("Drop files here"), "one multi-file drop zone");
  check(p.$("size").textContent === "Total size: 0.0 MB of 4.0 MB allowed", "shows the size limit from /api/health");
  check(p.$("go").textContent === "Read the documents" && !p.$("go").disabled, "the submit button is ready");
  check(p.$("confirm").hidden && p.$("result").hidden, "the confirm screen and result start hidden");

  // ---------------------------------------------------------------- choosing files
  p.choose(pdf(MARLOWE[0]), pdf(MARLOWE[1]));
  p.choose(pdf(MARLOWE[1]), pdf(MARLOWE[2]));
  check(p.doc.querySelectorAll("#file-list li").length === 3, "files are added; choosing the same file twice keeps one");
  p.doc.querySelector("#file-list .remove").click();
  check([...p.doc.querySelectorAll("#file-list .name")].map((n) => n.textContent).join() === `${MARLOWE[1]},${MARLOWE[2]}`,
    "a file can be removed");
  const dropEvent = new p.dom.window.Event("drop", { bubbles: true, cancelable: true });
  dropEvent.dataTransfer = { files: [pdf(MARLOWE[3])] };
  p.$("drop").dispatchEvent(dropEvent);
  check(p.doc.querySelectorAll("#file-list li").length === 3, "dropping files adds them");

  // ---------------------------------------------------------------- client-side validation
  p = await openPage(8765);
  await p.submit();
  check(p.error() === "Choose at least one file.", "no files -> asked to choose one");
  p.choose(new File(["x"], "notes.docx"));
  await p.submit();
  check(p.error() === "notes.docx is not a PDF or .txt file.", "an unsupported file type is caught");
  p = await openPage(8765);
  p.choose(new File([new Uint8Array(5 * 1048576)], "big.pdf", { type: "application/pdf" }));
  check(p.$("size").className === "over", "the size readout turns red over the limit");
  await p.submit();
  check(p.error().includes("over the 4.0 MB limit"), "an oversized selection is blocked before uploading");
  check(p.sent.length === 0, "nothing is sent when the selection is invalid");

  // ---------------------------------------------------------------- the whole flow, real API
  p = await openPage(8765);
  p.choose(...MARLOWE.slice().reverse().map(pdf));
  p.$("doc-name").value = "Marlowe";
  p.$("code").value = "pw";
  await p.submit();
  const triageSent = p.sent[0];
  check(triageSent.which === "triage" && triageSent.opts.headers.Accept === "application/x-ndjson", "posts to /api/triage asking for NDJSON");
  check(triageSent.opts.body.getAll("files").length === 5 && triageSent.opts.body.get("access_code") === "pw"
    && triageSent.opts.body.get("doc_name") === "Marlowe", "sends every file, the access code and the matter name");
  check(!p.$("confirm").hidden && p.$("form").hidden && p.$("main").classList.contains("wide"), "the confirm screen replaces the form");
  check(p.steps().some((s) => s.includes("Found 5 document(s)")) && p.$("progress").hidden, "triage progress was shown, then put away");
  check([...p.doc.querySelectorAll("#file-rows tr")].map((r) => r.children[0].textContent).join() === MARLOWE.slice().reverse().join(),
    "files are listed in upload order");
  check(p.rows().length === 5 && p.rows().map((r) => r.dataset.id).join() === "d1,d2,d3,d4,d5", "documents in file order");
  check(p.$("summary").textContent === "5 documents: 5 annotate, 0 facts only, 0 log only.", "a summary line counts treatments");
  const r1 = p.row("d1");
  check(r1.querySelector(".doc-where").textContent === `${MARLOWE[4]}, sheet 1` && r1.querySelector(".doc-type").value === "amendment"
    && r1.querySelector(".doc-date").value === "2016-10-17" && r1.querySelector(".doc-treatment").value === "annotate",
    "each row shows pages, type, date and treatment");

  setSelect(p, p.row("d5").querySelector(".doc-treatment"), "log_only");
  check(p.$("summary").textContent === "5 documents: 4 annotate, 0 facts only, 1 log only.", "the summary updates live");
  p.row("d4").querySelector(".doc-excluded").click();
  check(p.$("summary").textContent.endsWith("1 log only, 1 excluded.") && p.row("d4").classList.contains("excluded"),
    "excluding a document");
  p.row("d3").querySelector(".doc-superseded").click();
  setSelect(p, p.row("d2").querySelector(".doc-amends"), "d3");
  const note = p.row("d2").querySelector(".doc-note"); note.value = "checked by hand"; p.fire(note, "change");
  const date = p.row("d1").querySelector(".doc-date"); date.value = "2016-10-18"; p.fire(date, "change");
  setSelect(p, p.row("d1").querySelector(".doc-type"), "codicil");
  check(p.row("d1").querySelector(".doc-type").value === "codicil", "the type can be changed");

  await p.annotate();
  const body = p.lastBody();
  const sentAnnotate = p.sent.filter((s) => s.which === "annotate")[0];
  check(sentAnnotate.opts.headers["Content-Type"] === "application/json" && body.access_code === "pw", "posts the confirmed plan as JSON with the code");
  const docs = Object.fromEntries(body.plan.documents.map((d) => [d.id, d]));
  check(docs.d5.treatment === "log_only" && docs.d4.excluded === true && docs.d3.superseded === true && docs.d2.amends === "d3"
    && docs.d2.note === "checked by hand" && docs.d1.date === "2016-10-18" && docs.d1.doc_type === "codicil",
    "every edit reaches the plan");
  check(body.plan.edits.length === 7 && body.plan.edits.some((e) => e.document === "d5" && e.field === "treatment" && e.from === "annotate" && e.to === "log_only"),
    "every edit is recorded for the audit trail");
  check(Object.keys(body.pages).length === 5 && body.pages[MARLOWE[0]].length === 2, "the page texts held since triage are sent back");
  check(p.resultShown() && p.error() === null, "the workbook is delivered");
  check(p.$("download").getAttribute("download") === "Marlowe_annotation.xlsx" && p.downloads.at(-1).name === "Marlowe_annotation.xlsx",
    "the download starts by itself, named for the matter");
  const bytes = new Uint8Array(await p.blobs[0].arrayBuffer());
  check(bytes[0] === 0x50 && bytes[1] === 0x4b && bytes.length > 5000, "the file is a real xlsx (zip)");
  check([...p.doc.querySelectorAll("#result-summary li")].some((li) => li.textContent.includes("1 excluded")),
    "a summary is shown with the result");
  check(p.errors.length === 0, `no script errors in the page (${p.errors.join("; ")})`);
  p.$("again").click();
  check(!p.$("form").hidden && p.doc.querySelectorAll("#file-list li").length === 0 && p.$("result").hidden, "start over for another matter");

  // ---------------------------------------------------------------- one confident document: no confirm screen
  p = await openPage(8765);
  p.choose(pdf(TRUST));
  await p.submit();
  await p.until(() => p.resultShown() || p.error());
  check(p.resultShown() && p.$("confirm").hidden, "one document, high confidence, no flags: the confirm screen is skipped");
  check(p.sent.map((s) => s.which).join() === "triage,annotate", "triage then annotate, with no stop");
  check(p.steps().some((s) => s.includes("no check needed")) && p.doc.querySelectorAll("#result-summary li").length >= 1,
    "the summary is shown afterwards");

  // ---------------------------------------------------------------- attention first, split and merge
  p = await openPage(8765, { triage: cannedTriage() });
  p.choose(new File(["x"], "a.txt"));
  await p.submit();
  check(p.rows().map((r) => r.dataset.id).join() === "d3,d1,d2", "documents needing a decision come first");
  check(p.row("d3").classList.contains("attention") && p.row("d3").querySelector(".flag").textContent === "low confidence",
    "a flagged document is marked and its flag is shown");
  const at = p.row("d1").querySelector(".split-at"); at.value = "3";
  p.row("d1").querySelector(".split-btn").click();
  check(p.rows().length === 4 && p.row("d4") && p.row("d1").querySelector(".doc-where").textContent === "a.txt, sheets 1-2"
    && p.row("d4").querySelector(".doc-where").textContent === "a.txt, sheets 3-4", "split a page range in two");
  check(p.row("d4").classList.contains("attention"), "the new piece has no date, so it needs a decision");
  p.row("d1").querySelector(".merge-btn").click();
  check(p.rows().length === 3 && p.row("d1").querySelector(".doc-where").textContent === "a.txt, sheets 1-4", "merge it back");
  check(p.row("d2").querySelector(".merge-btn").textContent === "Merge with d3", "adjacent documents in a file can be merged");
  p.row("d1").querySelector(".split-at").value = "9";
  p.row("d1").querySelector(".split-btn").click();
  check(p.error().startsWith("Choose a sheet from 2 to 4"), "a split outside the range is refused");

  // ---------------------------------------------------------------- two matters: ask before continuing
  p = await openPage(8765, { triage: cannedTriage({ matters: true }) });
  p.choose(new File(["x"], "a.txt"));
  await p.submit();
  check(!p.$("matters").hidden && p.$("annotate").disabled, "possible multiple matters: annotation waits for a decision");
  p.$("one-matter").click();
  check(!p.$("annotate").disabled, "confirming one matter enables annotation");

  // ---------------------------------------------------------------- the server refuses a plan
  const refused = () => Promise.resolve(new Response(JSON.stringify({ error: "The document map has problems: x", code: "invalid_plan",
    problems: ["a.txt: 1 sheet(s) are in no document (first: sheet 4)"] }), { status: 422, headers: { "content-type": "application/json" } }));
  p = await openPage(8765, { triage: cannedTriage(), annotate: refused });
  p.choose(new File(["x"], "a.txt"));
  await p.submit();
  await p.annotate();
  check(p.error().includes("1 sheet(s) are in no document") && !p.$("confirm").hidden && !p.$("annotate").disabled,
    "a refused plan shows the server's problems and the screen stays editable");

  // ---------------------------------------------------------------- server-side errors, real API
  p = await openPage(8765);
  p.choose(pdf("scan_like_blank.pdf"));
  await p.submit();
  check(p.error().includes("no text layer") && p.error().includes("scan_like_blank.pdf"), "a scanned PDF gets the server's explanation, by file name");
  check(!p.$("go").disabled && !p.$("form").hidden, "and the form is usable again");

  p = await openPage(8766);
  p.choose(pdf(TRUST));
  await p.submit();
  check(p.error() === "That access code is not correct.", "gated server: no code -> clear message");
  p.$("code").value = "sesame";
  await p.submit();
  await p.until(() => p.resultShown());
  check(p.resultShown() && p.error() === null, "gated server: the right code is accepted for both steps");

  // ---------------------------------------------------------------- streaming edge cases
  const stream = ndjson([{ type: "progress", message: "Files accepted." }, { type: "heartbeat", elapsed: 12 }], { close: false });
  p = await openPage(8765, { triage: async () => stream.response });
  p.choose(pdf(TRUST));
  p.$("form").dispatchEvent(new p.dom.window.Event("submit", { cancelable: true, bubbles: true }));
  await sleep(400);
  check(p.$("live").textContent === "Still working (12 seconds)...", "a heartbeat shows elapsed time");
  stream.end();
  await sleep(300);
  check(p.error().includes("connection closed before the work was finished"), "a stream that ends without a result is explained");

  p = await openPage(8765, { triage: async () => { throw new TypeError("network down"); } });
  p.choose(pdf(TRUST));
  await p.submit();
  check(p.error() === "Could not reach the server. Check your connection and try again.", "a network failure is reported plainly");

  p = await openPage(8765, { triage: async () => new Response("Request Entity Too Large", { status: 413 }) });
  p.choose(pdf(TRUST));
  await p.submit();
  check(p.error() === "The files are too large for one upload. Send fewer or smaller files.", "the platform's own 413 (not JSON) is translated");

  const failing = () => Promise.resolve(ndjson([{ type: "progress", message: "Plan accepted." },
    { type: "error", code: "rate_limited", message: "The model provider is rate-limiting requests. Wait a minute and try again." }]).response);
  p = await openPage(8765, { triage: cannedTriage(), annotate: failing });
  p.choose(new File(["x"], "a.txt"));
  await p.submit();
  await p.annotate();
  check(p.error().startsWith("The model provider is rate-limiting") && !p.resultShown(), "a model error event is shown, no download offered");

  console.log(`\nALL ${passed} UI CHECKS PASSED`);
} finally {
  server.proc.kill();
  gated.proc.kill();
}
process.exit(0);
