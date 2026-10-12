// Drives public/index.html in jsdom against a real local server that uses a fake model.
//
//     cd ui_test && npm install && node test_ui.mjs
//
// jsdom is not a browser: there is no layout and no real download. It runs the page's
// JavaScript for real, against the real API, which is what this checks.

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const FIX = join(ROOT, "fixtures");
const TRUST = "00_marlowe_trust_original.pdf";
const AMENDMENTS = [
  ["01_marlowe_first_amendment.pdf", 1, "First Amendment (2004)"],
  ["02_marlowe_second_amendment.pdf", 2, "Second Amendment (2009)"],
  ["03_marlowe_third_amendment_and_restatement.pdf", 3, "Third Amendment and Restatement (2013)"],
  ["04_marlowe_fourth_amendment.pdf", 4, "Fourth Amendment (2016)"],
];

let passed = 0;
function check(condition, message) {
  if (!condition) throw new Error(`FAILED: ${message}`);
  passed += 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pdf = (name) => new File([readFileSync(join(FIX, name))], name, { type: "application/pdf" });

async function startServer(port, env = {}) {
  const proc = spawn("python3", ["dev_stub_server.py"], {
    cwd: ROOT,
    env: { ...process.env, PORT: String(port), STUB_PAUSE_SECONDS: "0.2", ...env },
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

async function openPage(port, { fetchOverride } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (e) => errors.push(String(e.message || e)));
  virtualConsole.on("error", (e) => errors.push(String(e)));
  const base = `http://127.0.0.1:${port}`;
  const blobs = [];
  const downloads = [];

  const dom = await JSDOM.fromURL(`${base}/`, {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(window) {
      // The override replaces only the upload; the page's /api/health call still hits the server.
      window.fetch = (url, opts) => {
        const target = new URL(url, base).href;
        return (fetchOverride && target.includes("/api/annotate") ? fetchOverride : fetch)(target, opts);
      };
      window.FormData = FormData;
      window.Blob = Blob;
      window.File = File;
      window.TextDecoder = TextDecoder;
      window.URL.createObjectURL = (blob) => { blobs.push(blob); return `blob:fake-${blobs.length}`; };
      window.URL.revokeObjectURL = () => {};
      // A real browser downloads on <a download>.click(); jsdom can't, so record it.
      window.HTMLAnchorElement.prototype.click = function () { downloads.push({ href: this.getAttribute("href"), name: this.getAttribute("download") }); };
    },
  });
  await sleep(300);                                // let the health fetch resolve
  const doc = dom.window.document;
  const $ = (id) => doc.getElementById(id);

  const setFiles = (input, files) => {
    Object.defineProperty(input, "files", { value: files, configurable: true });
    input.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  };
  const setValue = (input, value) => { input.value = value; };

  const page = {
    dom, doc, $, errors, blobs, downloads,
    trust: (file) => setFiles($("trust"), [file]),
    addAmendment: (file, number, label = "") => {
      $("add").click();
      const rows = doc.querySelectorAll(".amd-row");
      const row = rows[rows.length - 1];
      if (file) setFiles(row.querySelector(".amd-file"), [file]);
      setValue(row.querySelector(".amd-number"), String(number));
      setValue(row.querySelector(".amd-label"), label);
      return row;
    },
    submit: async () => {
      $("form").dispatchEvent(new dom.window.Event("submit", { cancelable: true, bubbles: true }));
      for (let i = 0; i < 100; i++) {
        await sleep(100);
        if (!$("result").hidden || !$("error").hidden) break;
      }
    },
    steps: () => [...doc.querySelectorAll("#steps li")].map((li) => li.textContent),
    error: () => ($("error").hidden ? null : $("error").textContent),
    resultShown: () => !$("result").hidden,
  };
  return page;
}

function ndjsonResponse(lines, { close = true } = {}) {
  let controller;
  const encoder = new TextEncoder();
  const body = new ReadableStream({ start(c) { controller = c; for (const l of lines) c.enqueue(encoder.encode(JSON.stringify(l) + "\n")); if (close) c.close(); } });
  return { response: new Response(body, { status: 200, headers: { "content-type": "application/x-ndjson" } }), push: (l) => controller.enqueue(encoder.encode(JSON.stringify(l) + "\n")), end: () => controller.close() };
}

const server = await startServer(8765);
const gated = await startServer(8766, { CANETRUST_ACCESS_CODE: "sesame" });
try {
  // ---------------------------------------------------------------- initial state
  let p = await openPage(8765);
  check(p.$("amendments").children.length === 0, "starts with no amendment rows");
  check(p.$("size").textContent === "Total size: 0.0 MB of 4.0 MB allowed", "shows the size limit from /api/health");
  check(p.$("go").textContent === "Generate annotation" && !p.$("go").disabled, "the submit button is ready");

  // ---------------------------------------------------------------- amendment rows
  p.$("add").click();
  p.$("add").click();
  check(p.$("amendments").children.length === 2, "'Add an amendment' adds a row");
  check([...p.doc.querySelectorAll(".amd-number")].map((i) => i.value).join() === "1,2", "new rows are numbered 1, 2 by default");
  p.doc.querySelector(".amd-row .remove").click();
  p.$("add").click();
  check([...p.doc.querySelectorAll(".amd-number")].map((i) => i.value).join() === "2,1", "a freed number is offered again");
  for (let i = 0; i < 6; i++) p.$("add").click();
  check(p.$("amendments").children.length === 8 && p.$("add").disabled, "stops at 8 amendments");
  p.doc.querySelector(".amd-row .remove").click();
  check(p.$("amendments").children.length === 7 && !p.$("add").disabled, "removing a row frees a slot");

  // ---------------------------------------------------------------- client-side validation
  p = await openPage(8765);
  await p.submit();
  check(p.error() === "Choose the trust document.", "no trust file -> asked to choose one");
  p.trust(pdf(TRUST));
  p.addAmendment(null, 1);
  await p.submit();
  check(p.error().startsWith("Amendment 1 has no file"), "an amendment row with no file is caught");
  p.doc.querySelector(".amd-row .remove").click();
  p.addAmendment(pdf(AMENDMENTS[0][0]), 1);
  p.addAmendment(pdf(AMENDMENTS[1][0]), 1);
  await p.submit();
  check(p.error() === "Two amendments are numbered 1. Each number can be used once.", "duplicate numbers are caught");
  p = await openPage(8765);
  p.trust(new File(["x"], "notes.docx"));
  await p.submit();
  check(p.error() === "notes.docx is not a PDF or .txt file.", "an unsupported file type is caught");
  p = await openPage(8765);
  p.trust(new File([new Uint8Array(5 * 1048576)], "big.pdf", { type: "application/pdf" }));
  check(p.$("size").className === "over", "the size readout turns red over the limit");
  await p.submit();
  check(p.error().includes("over the 4.0 MB limit"), "an oversized selection is blocked before uploading");

  // ---------------------------------------------------------------- the page's look and what it sends
  p = await openPage(8765);
  check(p.doc.querySelector("header h1").textContent === "CaneTrust AI", "header: CaneTrust AI");
  check(p.doc.querySelector("header h1 span").textContent === "Trust", "header: the orange accent word");
  check(p.doc.querySelector("footer").textContent.trim() === "Miami AI & Law Lab", "footer is kept");
  check(p.$("legal-status").value === "OPERATIVE", "legal status defaults to OPERATIVE, as before");

  let sent = null;
  const done = ndjsonResponse([{ type: "done", filename: "x.xlsx", route: "single", warnings: [], xlsx_base64: "UEs=" }]);
  p = await openPage(8765, { fetchOverride: async (url, opts) => { sent = { url, opts }; return done.response; } });
  p.trust(pdf(TRUST));
  p.$("trust-label").value = "Marlowe Trust (1998)";
  p.$("legal-status").value = "IN FORCE";
  p.$("code").value = "pw";
  await p.submit();
  check(sent && sent.url.endsWith("/api/annotate") && sent.opts.method === "POST", "posts to /api/annotate");
  check(sent.opts.headers.Accept === "application/x-ndjson", "asks for the streaming (NDJSON) mode");
  const body = sent.opts.body;
  check(body.get("trust_label") === "Marlowe Trust (1998)" && body.get("status") === "IN FORCE" && body.get("access_code") === "pw",
    "sends the label, the legal status and the access code");
  check(body.get("trust").name === TRUST && body.get("amendments_meta") === "[]", "sends the trust file and empty amendment metadata");
  check(p.downloads.length === 1 && p.downloads[0].name === "x.xlsx", "a done event starts the download");

  // ---------------------------------------------------------------- single path, real API
  p = await openPage(8765);
  p.trust(pdf(TRUST));
  await p.submit();
  check(p.resultShown() && p.error() === null, "single: the workbook is delivered");
  check(p.steps().some((s) => s.includes("single-document pipeline")), "single: progress shows the triage decision");
  check(p.$("download").getAttribute("download") === "00_marlowe_trust_original_annotation.xlsx", "single: download name");
  check(p.downloads.length === 1 && p.downloads[0].name === "00_marlowe_trust_original_annotation.xlsx", "single: the download starts by itself");
  check(p.$("download").getAttribute("href").startsWith("blob:"), "single: the link points at the generated file");
  const bytes = new Uint8Array(await p.blobs[0].arrayBuffer());
  check(bytes[0] === 0x50 && bytes[1] === 0x4b && bytes.length > 5000, "single: the file is a real xlsx (zip) of plausible size");
  check(p.$("go").disabled === false, "the button is re-enabled afterwards");

  // ---------------------------------------------------------------- multi path, amendments out of order
  p = await openPage(8765);
  p.trust(pdf(TRUST));
  for (const i of [3, 1, 0, 2]) p.addAmendment(pdf(AMENDMENTS[i][0]), AMENDMENTS[i][1], AMENDMENTS[i][2]);
  await p.submit();
  check(p.resultShown(), "multi: the workbook is delivered");
  check(p.steps().some((s) => s.includes("4 amendment(s)") && s.includes("multi-document")), "multi: progress shows 4 amendments");
  check(p.$("download").getAttribute("download") === "00_marlowe_trust_original_annotation_with_amendments.xlsx", "multi: download name");
  check(p.errors.length === 0, `no script errors in the page (${p.errors.join("; ")})`);

  // ---------------------------------------------------------------- server-side errors
  p = await openPage(8765);
  p.trust(pdf("scan_like_blank.pdf"));
  await p.submit();
  check(p.error().includes("no text layer") && p.error().includes("scan_like_blank.pdf"), "a scanned PDF gets the server's explanation, by file name");
  check(!p.resultShown() && !p.$("go").disabled, "and the form is usable again");

  // ---------------------------------------------------------------- access code
  p = await openPage(8766);
  p.trust(pdf(TRUST));
  await p.submit();
  check(p.error() === "That access code is not correct.", "gated server: no code -> clear message");
  p.$("code").value = "sesame";
  await p.submit();
  check(p.resultShown() && p.error() === null, "gated server: the right code is accepted (error cleared)");

  // ---------------------------------------------------------------- streaming edge cases
  const stream = ndjsonResponse([{ type: "progress", message: "Documents accepted." }, { type: "heartbeat", elapsed: 12 }], { close: false });
  p = await openPage(8765, { fetchOverride: async () => stream.response });
  p.trust(pdf(TRUST));
  p.$("form").dispatchEvent(new p.dom.window.Event("submit", { cancelable: true, bubbles: true }));
  await sleep(400);
  check(p.$("live").textContent === "Still working (12 seconds)...", "a heartbeat shows elapsed time");
  stream.end();
  await sleep(300);
  check(p.error().includes("connection closed before the workbook was ready"), "a stream that ends without a result is explained, not silent");

  p = await openPage(8765, { fetchOverride: async () => { throw new TypeError("network down"); } });
  p.trust(pdf(TRUST));
  await p.submit();
  check(p.error() === "Could not reach the server. Check your connection and try again.", "a network failure is reported plainly");

  p = await openPage(8765, { fetchOverride: async () => new Response("Request Entity Too Large", { status: 413 }) });
  p.trust(pdf(TRUST));
  await p.submit();
  check(p.error() === "The files are too large for one upload. Send fewer or smaller files.", "the platform's own 413 (not JSON) is translated");

  const failing = ndjsonResponse([{ type: "progress", message: "Documents accepted." }, { type: "error", code: "rate_limited", message: "The model provider is rate-limiting requests. Wait a minute and try again." }]);
  p = await openPage(8765, { fetchOverride: async () => failing.response });
  p.trust(pdf(TRUST));
  await p.submit();
  check(p.error().startsWith("The model provider is rate-limiting"), "a model error event is shown to the user");
  check(!p.resultShown(), "and no download is offered");

  console.log(`\nALL ${passed} UI CHECKS PASSED`);
} finally {
  server.proc.kill();
  gated.proc.kill();
}
process.exit(0);
