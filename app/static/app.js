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

const peopleForm = document.querySelector("[data-people-form]");
const peopleBody = document.querySelector("[data-people-body]");
const selectionToolbar = document.querySelector("[data-selection-toolbar]");
const selectionCount = document.querySelector("[data-selection-count]");
const selectionSubmit = selectionToolbar?.querySelector("button[type='submit']");
const selectAll = document.querySelector("[data-select-all]");
const selectedPeople = new Set();

function rowCheckboxes() {
  return Array.from(document.querySelectorAll("[data-row-select]"));
}

function updateSelection() {
  const checkboxes = rowCheckboxes();
  checkboxes.forEach((checkbox) => {
    checkbox.checked = selectedPeople.has(checkbox.value);
    checkbox.closest("tr")?.classList.toggle("selected", checkbox.checked);
  });
  if (selectionCount) selectionCount.textContent = String(selectedPeople.size);
  if (selectionToolbar) selectionToolbar.classList.toggle("is-active", selectedPeople.size > 0);
  if (selectionSubmit) selectionSubmit.disabled = selectedPeople.size === 0;
  if (selectAll) {
    const checked = checkboxes.filter((checkbox) => checkbox.checked).length;
    selectAll.checked = checkboxes.length > 0 && checked === checkboxes.length;
    selectAll.indeterminate = checked > 0 && checked < checkboxes.length;
  }
}

selectAll?.addEventListener("change", () => {
  rowCheckboxes().forEach((checkbox) => {
    if (selectAll.checked) selectedPeople.add(checkbox.value);
    else selectedPeople.delete(checkbox.value);
  });
  updateSelection();
});

peopleForm?.addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-row-select]");
  if (!checkbox) return;
  if (checkbox.checked) selectedPeople.add(checkbox.value);
  else selectedPeople.delete(checkbox.value);
  updateSelection();
});
updateSelection();

let infiniteObserver;
let loadingPeople = false;

async function loadMorePeople(sentinel) {
  if (loadingPeople || !sentinel?.dataset.nextUrl) return;
  loadingPeople = true;
  sentinel.classList.add("is-loading");
  try {
    const response = await fetch(sentinel.dataset.nextUrl, {
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`Could not load contacts (${response.status})`);
    const html = await response.text();
    infiniteObserver?.unobserve(sentinel);
    sentinel.remove();
    peopleBody.insertAdjacentHTML("beforeend", html);
    updateSelection();
    observeInfiniteSentinel();
  } catch (error) {
    sentinel.classList.remove("is-loading");
    sentinel.querySelector(".loading-indicator").textContent =
      "Could not load more contacts. Scroll away and try again.";
    console.error(error);
  } finally {
    loadingPeople = false;
  }
}

function observeInfiniteSentinel() {
  const sentinel = document.querySelector("[data-infinite-sentinel]");
  if (!sentinel || !("IntersectionObserver" in window)) return;
  infiniteObserver ||= new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) loadMorePeople(entry.target);
    });
  }, { rootMargin: "500px 0px" });
  infiniteObserver.observe(sentinel);
}
observeInfiniteSentinel();

const interactionDialog = document.querySelector("[data-interaction-dialog]");
const interactionForm = interactionDialog?.querySelector("[data-interaction-form]");
const interactionPerson = interactionDialog?.querySelector("[data-interaction-person]");
const returnTo = interactionDialog?.querySelector("[data-return-to]");

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-quick-interaction]");
  if (trigger && interactionDialog && interactionForm) {
    interactionForm.action = `/people/${encodeURIComponent(trigger.dataset.personId)}/interactions`;
    interactionPerson.textContent = trigger.dataset.personName;
    returnTo.value = `${window.location.pathname}${window.location.search}`;
    interactionDialog.showModal();
    interactionDialog.querySelector("select, input, textarea")?.focus();
    return;
  }
  if (event.target.closest("[data-dialog-close]")) {
    interactionDialog?.close();
  }
});

interactionDialog?.addEventListener("click", (event) => {
  if (event.target === interactionDialog) interactionDialog.close();
});

document.querySelector("[data-print]")?.addEventListener("click", () => window.print());
