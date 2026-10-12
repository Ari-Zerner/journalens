const $ = (id) => document.getElementById(id);
const statusLine = $("status");
const buttons = [$("upload"), $("rerun")];

function show(text) {
  statusLine.textContent = text;
}

function setBusy(busy) {
  for (const b of buttons) b.disabled = busy;
}

async function poll() {
  let s;
  try {
    const r = await fetch("/status");
    if (r.status === 401) return location.reload();
    s = await r.json();
  } catch {
    return setTimeout(poll, 5000);
  }
  if (s.state === "queued" || s.state === "running") {
    setBusy(true);
    show(s.step ? `Working: ${s.step}…` : "Queued…");
    return setTimeout(poll, 5000);
  }
  setBusy(false);
  if (s.state === "done" && watching) location.href = `/reports/${s.report}`;
  else if (s.state === "error") show(`Last run failed: ${s.error}`);
  else if (watching) show("");
}

let watching = false;

async function run() {
  const r = await fetch("/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topics: $("topics").value }),
  });
  if (!r.ok) {
    setBusy(false);
    return show((await r.json().catch(() => ({}))).error || `Could not start (${r.status}).`);
  }
  watching = true;
  poll();
}

$("rerun").addEventListener("click", () => {
  setBusy(true);
  show("Starting…");
  run();
});

$("upload").addEventListener("click", () => {
  const file = $("file").files[0];
  if (!file) return show("Choose an export file first.");
  setBusy(true);
  const bar = $("progress");
  bar.hidden = false;
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", "/upload");
  xhr.upload.onprogress = (e) => {
    bar.value = e.loaded / e.total;
    show(`Uploading… ${Math.round((100 * e.loaded) / e.total)}%`);
  };
  xhr.onload = () => {
    bar.hidden = true;
    if (xhr.status === 201) return run();
    setBusy(false);
    let message = `Upload failed (${xhr.status}).`;
    try {
      message = JSON.parse(xhr.responseText).error || message;
    } catch {}
    show(message);
  };
  xhr.onerror = () => {
    bar.hidden = true;
    setBusy(false);
    show("Upload failed. Check your connection and try again.");
  };
  xhr.send(file);
});

poll();
