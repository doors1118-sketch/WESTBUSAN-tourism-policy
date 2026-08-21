(() => {
  "use strict";
  const features = [...document.querySelectorAll(".grid-feature,.facility-feature")];
  const filters = {
    district: document.getElementById("district-filter"),
    dong: document.getElementById("dong-filter"),
    period: document.getElementById("period-filter"),
  };
  const layerButtons = [...document.querySelectorAll(".layer-button")];
  let activeLayer = "tourism_supply_gap";

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
    return true;
  }

  function colour(value) {
    if (!Number.isFinite(value)) return "#8b959d";
    if (value >= 75) return "#c53b2d";
    if (value >= 50) return "#e98528";
    if (value >= 25) return "#e0bb32";
    return "#3779b6";
  }

  function layerValue(node) {
    if (activeLayer === "facility_density") return Number(node.dataset.facilityDensity);
    if (activeLayer === "aged_facilities") return Number(node.dataset.agedShare) * 100;
    return Number(node.dataset.tourismSupplyGap);
  }

  function setLayerEncoding() {
    features.filter((node) => node.dataset.kind === "grid").forEach((node) => {
      node.style.fill = colour(layerValue(node));
    });
  }

  function apply() {
    features.forEach((node) => node.classList.toggle("is-hidden", !visible(node)));
    document.getElementById("visible-grid-count").textContent = features.filter(
      (node) => node.dataset.kind === "grid" && visible(node),
    ).length;
    document.getElementById("visible-facility-count").textContent = features.filter(
      (node) => node.dataset.kind === "facility" && visible(node),
    ).length;
    setLayerEncoding();
  }
  Object.values(filters).forEach((select) => select.addEventListener("change", apply));
  layerButtons.forEach((button) => button.addEventListener("click", () => {
    activeLayer = button.dataset.layer;
    layerButtons.forEach((node) => node.classList.toggle("is-active", node === button));
    apply();
  }));

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
