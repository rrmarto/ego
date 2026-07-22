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

navToggle?.addEventListener("click", () => {
  const open = navToggle.getAttribute("aria-expanded") !== "true";
  navToggle.setAttribute("aria-expanded", String(open));
  siteNav?.classList.toggle("is-open", open);
});

for (const link of document.querySelectorAll("#site-nav a")) {
  link.addEventListener("click", () => {
    navToggle?.setAttribute("aria-expanded", "false");
    siteNav?.classList.remove("is-open");
  });
}

const observedSections = [...document.querySelectorAll("main section[id]")];
const navLinks = [...document.querySelectorAll("#site-nav a")];
const sectionObserver = new IntersectionObserver(
  (entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
    if (!visible) return;
    for (const link of navLinks) {
      link.classList.toggle("is-active", link.hash === `#${visible.target.id}`);
    }
  },
  { rootMargin: "-20% 0px -65%", threshold: [0.05, 0.25, 0.5] },
);

for (const section of observedSections) sectionObserver.observe(section);
