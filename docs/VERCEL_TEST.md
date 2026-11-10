# Manual test of the Vercel preview

Run this on the preview deployment of the `unified-pipeline` branch after each push. It takes
about 15 minutes and spends a little on model calls (the Marlowe set is fictional and small).

Before you start, in the Vercel project settings, confirm for the **Preview** environment:
`ANTHROPIC_API_KEY` is set, and `CANETRUST_ACCESS_CODE` is set (call its value CODE below).
Find the preview URL on the project's Deployments page (the newest deployment of the branch),
called PREVIEW below. If the project uses Vercel Authentication (deployment protection), log in
to Vercel in the same browser first.

## 1. Health check

Open `PREVIEW/api/health`.

Expected JSON: `"ok": true`, `"assets_present": true`, `"api_key_set": true`,
`"access_code_required": true`, `"max_upload_mb": 4.0`, `"max_state_mb": 3.0`,
`"supported_types": [".pdf", ".txt"]`, and `"model"` naming the model in use. The key itself never
appears.

If `assets_present` is false, the bundle is missing a file from `assets/`: run
`python tools/check_bundle.py` locally. If `api_key_set` is false, the key is not set for Preview.

## 2. The Marlowe set through the confirm screen

1. Open `PREVIEW/`. Expected: the green header "CaneTrust AI" with "Trust" in orange, one dashed
   drop zone, a "Matter name" field, an "Access code" field, the line "Total size: 0.0 MB of 4.0 MB
   allowed", and an orange "Read the documents" button.
2. Drop (or choose) the five files `fixtures/00_marlowe_trust_original.pdf` to
   `04_marlowe_fourth_amendment.pdf`, in any order. Expected: five rows with sizes; the size line
   well under 4 MB. Type CODE and click "Read the documents".
3. Expected within about 30 seconds: progress lines ("Files accepted.", "Sorting 7 sheet(s) in 5
   file(s) into documents.", "Found 5 document(s)."), then the confirm screen:
   - Files: the five files in the order you chose them, 2, 1, 1, 2 and 1 sheets.
   - Summary line: "5 documents: 5 annotate, 0 facts only, 0 log only."
   - Five rows: the 1998 trust (Trust, 1998-03-12), the first and second amendments (Trust
     amendment, 2004-06-21 and 2009-09-04), the third amendment and restatement (Restated trust,
     2013-05-02) and the fourth amendment (Trust amendment, 2016-10-17), all "Annotate". No row
     should be marked as needing a decision. Triage reads content, so the order is by file order,
     not by date.
4. Try the controls without changing the outcome: change one treatment to "Log only" (the summary
   updates at once), then set it back to "Annotate".
5. Click "Annotate". Expected: progress ("Plan accepted.", "1 chain(s) to annotate.", "Annotating
   c1 (trust, 4 amendments)." (the revoked restatement keeps its column), "Contacts and court facts read.", "c1 annotated.", "Filling in the
   workbook."), a heartbeat line ("Still working (N seconds)...") while the model works, then the
   download starts by itself and a summary appears. This step can take 2 to 4 minutes.
6. Open the workbook. Expected:
   - Sheets: Instructions, Packet map, and one chain sheet named after the 1998 trust.
   - The fourth amendment revokes the 2013 restatement, so the chain is the 1998 trust (base) with
     the first, second, third (marked "REVOKED by Amendment 4") and fourth amendments in columns
     G to J; columns K to N hidden. Row 11 headers carry "revival assumed" on the revived
     instruments, and the "Chain notes" at the top say revival is assumed (a person must confirm
     it).
   - Packet map: five rows with file, sheets, type, date, treatment "annotate", the chain column
     ("c1 base", "c1 Amendment 1" ... "c1 Amendment 3 (revoked by d5)"), and `revival_assumed`
     flags.
   - Citations name printed pages ("p. 1", "p. 2") with the instrument ("Trust, Art. ...",
     "Amendment 2, ..."). No "PDF page".
   - Section 10 (contacts) at the bottom of the chain sheet: the trustee and successor trustee
     with sources and dates in column C.

If step 5 ends with "This is taking longer than the 280 seconds the server allows", the chain
was too long for Hobby's 300 s: set `CANETRUST_EFFORT=medium` for Preview, redeploy, and retry.

## 3. A scan is refused

Start over, drop `fixtures/scan_like_blank.pdf`, type CODE, click "Read the documents".
Expected within a few seconds, before any model call: the red message "scan_like_blank.pdf: this
PDF has no text layer (it looks like a scan). Scanned documents are not supported yet. ...". The
form stays usable.

## 4. A wrong access code

Choose `00_marlowe_trust_original.pdf`, type a wrong code, click "Read the documents".
Expected: "That access code is not correct." Then type CODE and submit again. Expected: this one
file is a single document, so if triage reads it with high confidence and no flags the confirm
screen is skipped ("One document, read with confidence: no check needed."), the workbook
downloads, and a summary is shown. (If triage was less than sure, the confirm screen appears
with one row instead: that is correct too; click "Annotate".)

## 5. A file over 4 MB

Choose any PDF larger than 4 MB (or several files that add up to more). Expected: the size line
turns red, and clicking the button shows "The files are over the 4.0 MB limit for one upload.
Send fewer or smaller files." Nothing is uploaded. (If the browser check is bypassed, the server
answers 413 with the same advice, and Vercel itself refuses bodies over 4.5 MB.)

## 6. The function logs

In Vercel: Project, then Deployments, open the preview deployment, then the **Logs** tab (or
**Observability > Logs**), filter to `/api/triage` and `/api/annotate`.

Expected: one line per request with status and duration (triage tens of seconds, annotation
minutes). Error lines look like `[error] RateLimitError: HTTP 429` or `[error] ValueError: (message
withheld)`. No line may contain any document text: no names, no clauses, no page text. A log line
with document text is a bug; report the line's timestamp, not its content.
