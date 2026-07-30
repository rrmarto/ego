const lensButtons = document.querySelectorAll("[data-lens]");
const architectureLayers = document.querySelectorAll("[data-lenses]");

for (const button of lensButtons) {
  button.addEventListener("click", () => {
    const lens = button.dataset.lens;
    for (const candidate of lensButtons) {
      const active = candidate === button;
      candidate.classList.toggle("is-active", active);
      candidate.setAttribute("aria-pressed", String(active));
    }
    for (const layer of architectureLayers) {
      const focused = lens === "all" || layer.dataset.lenses.split(" ").includes(lens);
      layer.classList.toggle("is-focused", lens !== "all" && focused);
      layer.classList.toggle("is-dimmed", !focused);
    }
  });
}

const navToggle = document.querySelector(".nav-toggle");
const siteNav = document.querySelector("#site-nav");

function setNavigationOpen(open) {
  navToggle?.setAttribute("aria-expanded", String(open));
  siteNav?.classList.toggle("is-open", open);
}

navToggle?.addEventListener("click", () => {
  const open = navToggle.getAttribute("aria-expanded") !== "true";
  setNavigationOpen(open);
});

for (const link of document.querySelectorAll("#site-nav a")) {
  link.addEventListener("click", () => {
    setNavigationOpen(false);
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || navToggle?.getAttribute("aria-expanded") !== "true") return;
  setNavigationOpen(false);
  navToggle.focus();
});

const observedSections = [...document.querySelectorAll("main section[id]")];
const navLinks = [...document.querySelectorAll("#site-nav a")];
const sectionObserver = new IntersectionObserver(
  (entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
    if (!visible) return;
    for (const link of navLinks) {
      const active = link.hash === `#${visible.target.id}`;
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "location");
      } else {
        link.removeAttribute("aria-current");
      }
    }
  },
  { rootMargin: "-20% 0px -65%", threshold: [0.05, 0.25, 0.5] },
);

for (const section of observedSections) sectionObserver.observe(section);
