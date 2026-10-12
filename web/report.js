const article = document.querySelector("article");
const saved = document.getElementById("saved");
const buttons = { save: document.getElementById("save"), copy: document.getElementById("copy"), share: document.getElementById("share") };

function collect(selector, key) {
  return [...article.querySelectorAll(selector)]
    .map((box) => ({ [key]: box.dataset[key], text: box.value.trim() }))
    .filter((item) => item.text);
}

function general() {
  return article.querySelector("textarea[data-general]").value.trim();
}

// The report as Markdown with the answers and notes written into it.
function compose() {
  const parts = [article.dataset.title];
  for (const section of article.querySelectorAll("section[data-heading]")) {
    let md = `## ${section.dataset.heading}\n\n${section.dataset.md}`;
    section.querySelectorAll("textarea[data-question]").forEach((box, i) => {
      md += `\n\n${i + 1}. ${box.dataset.question}\n\n   **Answer:** ${box.value.trim()}`;
    });
    const note = section.querySelector("textarea[data-section]").value.trim();
    if (note) md += `\n\n**My note:** ${note}`;
    parts.push(md);
  }
  if (general()) parts.push(`## My notes\n\n${general()}`);
  return parts.join("\n\n") + "\n";
}

async function save() {
  try {
    const r = await fetch(`/reports/${article.dataset.report}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        answers: collect("textarea[data-question]", "question"),
        notes: collect("textarea[data-section]", "section"),
        general: general(),
      }),
    });
    if (r.status === 401) return "Signed out. Copy your text, then reload and sign in.";
    if (!r.ok) return `Could not save (${r.status}).`;
    return "";
  } catch {
    return "Could not save. Check your connection and try again.";
  }
}

article.addEventListener("input", () => {
  saved.textContent = "Unsaved changes.";
});

buttons.save.addEventListener("click", async () => {
  buttons.save.disabled = true;
  saved.textContent = "Saving…";
  saved.textContent = (await save()) || "Saved. The next report will read these.";
  buttons.save.disabled = false;
});

// Copy and share act at once, while the browser still counts the tap as the
// reason, and save alongside so the journal copy and the site agree.
buttons.copy.addEventListener("click", async () => {
  let outcome = "Copied, answers and notes included. Paste it into a new journal entry.";
  try {
    await navigator.clipboard.writeText(compose());
  } catch {
    outcome = "Could not copy: the browser refused clipboard access.";
  }
  saved.textContent = (await save()) || outcome;
});

if (navigator.share) {
  buttons.share.hidden = false;
  buttons.share.addEventListener("click", async () => {
    try {
      await navigator.share({ title: article.dataset.title.replace(/^#\s*/, ""), text: compose() });
    } catch {
      // Dismissing the share sheet lands here too; nothing to report.
    }
    const problem = await save();
    if (problem) saved.textContent = problem;
  });
}
