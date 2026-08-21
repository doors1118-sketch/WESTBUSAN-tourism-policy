(() => {
  "use strict";
  const features = [...document.querySelectorAll(".grid-feature,.facility-feature")];
  const clusters = [...document.querySelectorAll(".facility-cluster")];
  const filterable = [...features, ...clusters];
  const westDistricts = new Set(["강서구", "사하구", "북구", "사상구"]);
  const filters = {
    district: document.getElementById("district-filter"),
    dong: document.getElementById("dong-filter"),
    period: document.getElementById("period-filter"),
  };
  const layerButtons = [...document.querySelectorAll(".layer-button")];
  let activeLayer = "policy_priority";

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
    if (
      activeLayer === "policy_priority"
      && node.dataset.kind === "cluster"
      && !westDistricts.has(node.dataset.district)
    ) return false;
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

  function policyColour(kind) {
    return {
      new_supply: "#c53b2d",
      remodel: "#e98528",
      transport_quality: "#168b89",
      tourism_product: "#6b5ac6",
    }[kind] || "#8b959d";
  }

  function layerValue(node) {
    let raw = node.dataset.tourismSupplyGap;
    if (activeLayer === "facility_density") raw = node.dataset.facilityDensity;
    if (activeLayer === "aged_facilities") raw = node.dataset.agedShare;
    if (raw === undefined || raw === "") return Number.NaN;
    const value = Number(raw);
    return activeLayer === "aged_facilities" ? value * 100 : value;
  }

  function setLayerEncoding() {
    document.body.classList.toggle("policy-layer", activeLayer === "policy_priority");
    features.filter((node) => node.dataset.kind === "grid").forEach((node) => {
      if (activeLayer === "policy_priority") {
        node.style.fill = policyColour(node.dataset.policyKind);
        node.style.fillOpacity = node.dataset.policyKind ? ".48" : ".025";
        return;
      }
      const value = layerValue(node);
      node.style.fill = colour(value);
      node.style.fillOpacity = Number.isFinite(value) ? ".58" : ".035";
    });
    const explanations = {
      policy_priority: "서부산 4개 구의 수요·공급 구조에 따라 우선 검토할 정책방향입니다.",
      tourism_supply_gap: "구별 방문수요/객실 지표와 500m 숙박공급을 결합한 공급부족도입니다.",
      facility_density: "주소 좌표가 확인된 숙박시설이 공간적으로 모여 있는 정도입니다.",
      aged_facilities: "건물연수가 확인된 숙박시설 중 노후시설 비율입니다.",
    };
    document.getElementById("layer-explainer").textContent = explanations[activeLayer];
  }

  function apply() {
    filterable.forEach((node) => node.classList.toggle("is-filtered", !visible(node)));
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
    document.body.classList.toggle("detail-mode", scale >= 2.25);
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
