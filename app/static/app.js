document.documentElement.classList.add("js");

const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
const sidebarClose = document.querySelector("[data-sidebar-close]");

function setSidebar(open) {
  document.body.classList.toggle("sidebar-open", open);
  if (sidebarToggle) {
    sidebarToggle.setAttribute("aria-expanded", String(open));
    sidebarToggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  }
}

sidebarToggle?.addEventListener("click", () => {
  setSidebar(!document.body.classList.contains("sidebar-open"));
});
sidebarClose?.addEventListener("click", () => setSidebar(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setSidebar(false);
});

const selectionToolbar = document.querySelector("[data-selection-toolbar]");
const selectionCount = document.querySelector("[data-selection-count]");
const selectionSubmit = selectionToolbar?.querySelector("button[type='submit']");
const rowCheckboxes = Array.from(document.querySelectorAll("[data-row-select]"));
const selectAll = document.querySelector("[data-select-all]");

function updateSelection() {
  const selected = rowCheckboxes.filter((checkbox) => checkbox.checked).length;
  if (selectionCount) selectionCount.textContent = String(selected);
  if (selectionToolbar) selectionToolbar.classList.toggle("is-active", selected > 0);
  if (selectionSubmit) selectionSubmit.disabled = selected === 0;
  if (selectAll) {
    selectAll.checked = selected > 0 && selected === rowCheckboxes.length;
    selectAll.indeterminate = selected > 0 && selected < rowCheckboxes.length;
  }
  rowCheckboxes.forEach((checkbox) => {
    checkbox.closest("tr")?.classList.toggle("selected", checkbox.checked);
  });
}

selectAll?.addEventListener("change", () => {
  rowCheckboxes.forEach((checkbox) => {
    checkbox.checked = selectAll.checked;
  });
  updateSelection();
});
rowCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", updateSelection));
updateSelection();
