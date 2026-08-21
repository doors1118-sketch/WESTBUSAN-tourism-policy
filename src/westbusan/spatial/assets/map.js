(() => {
  "use strict";
  const features = [...document.querySelectorAll(".grid-feature,.facility-feature")];
  const filters = {
    district: document.getElementById("district-filter"),
    dong: document.getElementById("dong-filter"),
    period: document.getElementById("period-filter"),
    component: document.getElementById("component-filter"),
    grade: document.getElementById("grade-filter"),
  };

  function addOptions(select, values) {
    [...new Set(values.filter(Boolean))].sort().forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    });
  }
  addOptions(filters.district, features.map((node) => node.dataset.district));
  addOptions(filters.dong, features.map((node) => node.dataset.dong));
  addOptions(filters.period, features.map((node) => node.dataset.period));

  function visible(node) {
    if (filters.district.value && node.dataset.district !== filters.district.value) return false;
    if (filters.dong.value && node.dataset.dong !== filters.dong.value) return false;
    if (filters.period.value && node.dataset.period !== filters.period.value) return false;
    if (filters.grade.value && node.dataset.grade !== filters.grade.value) return false;
    const component = filters.component.value;
    if (component === "small-scale" && node.dataset.smallScale === "unavailable") return false;
    if (component === "aged" && node.dataset.aged === "unavailable") return false;
    if (component === "context" && node.dataset.context === "unavailable") return false;
    return true;
  }

  function apply() {
    features.forEach((node) => node.classList.toggle("is-hidden", !visible(node)));
    document.getElementById("visible-grid-count").textContent = features.filter(
      (node) => node.dataset.kind === "grid" && visible(node),
    ).length;
    document.getElementById("visible-facility-count").textContent = features.filter(
      (node) => node.dataset.kind === "facility" && visible(node),
    ).length;
  }
  Object.values(filters).forEach((select) => select.addEventListener("change", apply));

  const viewport = document.getElementById("map-viewport");
  const svg = document.getElementById("spatial-map");
  let scale = 1;
  let tx = 0;
  let ty = 0;
  function transform() {
    viewport.setAttribute("transform", `translate(${tx} ${ty}) scale(${scale})`);
  }
  function zoom(delta) {
    scale = Math.max(.75, Math.min(8, scale * delta));
    transform();
  }
  document.getElementById("zoom-in").addEventListener("click", () => zoom(1.25));
  document.getElementById("zoom-out").addEventListener("click", () => zoom(.8));
  document.getElementById("zoom-reset").addEventListener("click", () => {
    scale = 1;
    tx = 0;
    ty = 0;
    transform();
  });
  svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoom(event.deltaY < 0 ? 1.1 : .9);
  }, { passive: false });
  let origin = null;
  svg.addEventListener("pointerdown", (event) => {
    origin = [event.clientX - tx, event.clientY - ty];
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", (event) => {
    if (origin) {
      tx = event.clientX - origin[0];
      ty = event.clientY - origin[1];
      transform();
    }
  });
  svg.addEventListener("pointerup", () => { origin = null; });
  apply();
})();
