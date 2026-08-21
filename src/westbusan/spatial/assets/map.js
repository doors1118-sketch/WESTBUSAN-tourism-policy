(() => {
  "use strict";
  const grids = [...document.querySelectorAll(".grid-feature")];
  const facilities = [...document.querySelectorAll(".facility-feature")];
  const clusters = [...document.querySelectorAll(".facility-cluster")];
  const policyLabels = [...document.querySelectorAll(".district-policy-label")];
  const filterable = [...grids, ...facilities, ...clusters, ...policyLabels];
  const westDistricts = new Set(["강서구", "사하구", "북구", "사상구"]);
  const eastDistricts = new Set(["해운대구", "수영구", "기장군"]);
  const filters = {
    district: document.getElementById("district-filter"),
    dong: document.getElementById("dong-filter"),
    period: document.getElementById("period-filter"),
  };
  const layerButtons = [...document.querySelectorAll(".layer-button")];
  const legend = document.getElementById("dynamic-legend");
  const candidateLayer = document.getElementById("candidate-markers");
  let candidateMarkers = [];
  let activeLayer = "policy_priority";

  function addOptions(select, values) {
    [...new Set(values.filter(Boolean))].sort().forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    });
  }
  addOptions(filters.district, grids.map((node) => node.dataset.district));
  addOptions(filters.period, grids.map((node) => node.dataset.period));

  function refreshDongOptions() {
    const selected = filters.dong.value;
    filters.dong.replaceChildren(new Option("전체", ""));
    addOptions(filters.dong, grids
      .filter((node) => !filters.district.value || node.dataset.district === filters.district.value)
      .map((node) => node.dataset.dong));
    if ([...filters.dong.options].some((option) => option.value === selected)) filters.dong.value = selected;
  }
  refreshDongOptions();

  function visible(node) {
    if (filters.district.value && node.dataset.district !== filters.district.value) return false;
    if (filters.dong.value && node.dataset.dong !== filters.dong.value) return false;
    if (filters.period.value && node.dataset.period && node.dataset.period !== filters.period.value) return false;
    return true;
  }

  function policyColour(kind) {
    return { new_supply: "#c53b2d", remodel: "#e98528", investment_caution: "#667784" }[kind] || "#8b959d";
  }

  function layerValue(node) {
    let raw = node.dataset.tourismSupplyGap;
    if (activeLayer === "facility_density") raw = node.dataset.mappedFacilityCount;
    if (activeLayer === "aged_facilities") {
      if (Number(node.dataset.ageKnown || 0) <= 0) return Number.NaN;
      raw = node.dataset.agedCount;
    }
    if (raw === undefined || raw === "") return Number.NaN;
    return Number(raw);
  }

  function metricColour(value) {
    if (!Number.isFinite(value)) return "#8b959d";
    if (activeLayer === "facility_density") {
      if (value >= 20) return "#762a83";
      if (value >= 8) return "#9970ab";
      if (value >= 3) return "#c2a5cf";
      if (value >= 1) return "#e7d4e8";
      return "#eef1f3";
    }
    if (activeLayer === "aged_facilities") {
      if (value >= 10) return "#8c2d04";
      if (value >= 5) return "#d94801";
      if (value >= 2) return "#f16913";
      if (value >= 1) return "#fdae6b";
      return "#eef1f3";
    }
    if (value >= 75) return "#b2182b";
    if (value >= 50) return "#ef8a62";
    if (value >= 25) return "#fddbc7";
    return "#67a9cf";
  }

  function setLegend(items) {
    legend.replaceChildren(...items.map(([colour, label]) => {
      const item = document.createElement("li");
      const swatch = document.createElement("i");
      swatch.style.background = colour;
      const text = document.createElement("span");
      text.textContent = label;
      item.append(swatch, text);
      return item;
    }));
  }

  function setLayerEncoding() {
    document.body.classList.toggle("policy-layer", activeLayer === "policy_priority");
    document.body.classList.toggle("facility-layer", activeLayer === "facility_locations");
    document.body.dataset.activeLayer = activeLayer;
    grids.forEach((node) => {
      if (activeLayer === "policy_priority") {
        node.style.fill = policyColour(node.dataset.recommendation);
        node.style.fillOpacity = node.dataset.recommendation ? ".68" : ".015";
      } else if (activeLayer === "facility_locations") {
        node.style.fill = "#ffffff";
        node.style.fillOpacity = ".012";
      } else {
        const value = layerValue(node);
        node.style.fill = metricColour(value);
        node.style.fillOpacity = Number.isFinite(value) && value > 0 ? ".72" : ".02";
      }
    });
    const explanations = {
      policy_priority: "서부산의 읍면동·500m 단위 수요·공급·노후 신호로 후보지역을 좁힙니다. 번호를 누르면 해당 생활권으로 이동합니다.",
      tourism_supply_gap: "공급부족도 = 구별 방문수요 점수 − 해당 500m 객실공급 점수입니다. 값이 클수록 수요에 비해 확인 객실이 적습니다.",
      facility_density: "500m 안의 주소 확인 숙박시설 수입니다. 면적이 작은 해안 경계 때문에 값이 과장되지 않도록 시설 개수를 사용합니다.",
      aged_facilities: "500m 안의 20년 이상 숙박시설 수입니다. 클릭하면 건물연수가 확인된 전체 표본도 함께 표시됩니다.",
      facility_locations: "숙박시설 위치 레이어입니다. 평소에는 읍면동 단위로 묶고 확대하면 개별 주소 좌표를 표시합니다.",
    };
    document.getElementById("layer-explainer").textContent = explanations[activeLayer];
    const legends = {
      policy_priority: [["#c53b2d", "500m 신규공급 후보"], ["#e98528", "500m 노후시설 개선·전환 후보"], ["#667784", "추가 근거 확인 필요"], ["#ffffff", "근거 부족·후보 아님"]],
      tourism_supply_gap: [["#b2182b", "75 이상 · 공급부족 매우 높음"], ["#ef8a62", "50–74 · 공급부족 높음"], ["#fddbc7", "25–49 · 추가 검토"], ["#67a9cf", "0–24 · 상대적 부족 낮음"]],
      facility_density: [["#762a83", "20개 이상"], ["#9970ab", "8–19개"], ["#c2a5cf", "3–7개"], ["#e7d4e8", "1–2개"]],
      aged_facilities: [["#8c2d04", "20년 이상 10개 이상"], ["#d94801", "5–9개"], ["#f16913", "2–4개"], ["#fdae6b", "1개"]],
      facility_locations: [["#0d3b59", "숫자 원 · 읍면동 시설 수"], ["#496173", "확대 점 · 개별 숙박시설"]],
    };
    setLegend(legends[activeLayer]);
  }

  function buildCandidateRanking() {
    const groups = new Map();
    grids.filter((node) => westDistricts.has(node.dataset.district)
      && (!filters.district.value || node.dataset.district === filters.district.value)
      && (!filters.period.value || node.dataset.period === filters.period.value))
      .forEach((node) => {
        const kind = node.dataset.recommendation;
        if (!kind) return;
        const key = `${node.dataset.district}|${node.dataset.dong}`;
        const item = groups.get(key) || {
          district: node.dataset.district, dong: node.dataset.dong, newSupply: 0,
          remodel: 0, caution: 0, gap: 0, aged: 0, facilities: 0, bounds: [],
        };
        if (kind === "new_supply") item.newSupply += 1;
        else if (kind === "remodel") item.remodel += 1;
        else item.caution += 1;
        item.gap = Math.max(item.gap, Number(node.dataset.tourismSupplyGap || 0));
        item.aged += Number(node.dataset.agedCount || 0);
        item.facilities += Number(node.dataset.mappedFacilityCount || 0);
        item.bounds.push(node.dataset.mapBounds.split(",").map(Number));
        groups.set(key, item);
      });
    const ranked = [...groups.values()].map((item) => {
      const signal = item.newSupply > 0 ? 3 : item.remodel > 0 ? 2 : 1;
      const action = item.newSupply >= item.remodel && item.newSupply > 0
        ? "신규 관광숙박 공급 검토" : item.remodel > 0
          ? "노후시설 개선·전환 검토" : "추가 근거 확인";
      return { ...item, action, score: signal * 1000 + item.gap * 10 + item.aged + item.facilities * .1 };
    }).sort((a, b) => b.score - a.score || a.dong.localeCompare(b.dong, "ko")).slice(0, 5);
    const list = document.getElementById("candidate-rank-list");
    list.replaceChildren();
    candidateLayer.replaceChildren();
    candidateMarkers = [];
    ranked.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      const rank = document.createElement("b"); rank.textContent = String(index + 1);
      const label = document.createElement("span");
      label.textContent = `${item.district} ${item.dong}`;
      const detail = document.createElement("small");
      detail.textContent = `${item.action} · 시설 ${formatNumber(item.facilities)}개 · 20년+ ${formatNumber(item.aged)}개`;
      label.append(detail); button.append(rank, label);
      button.addEventListener("click", () => selectRegion(item.district, item.dong));
      list.append(button);

      const validBounds = item.bounds.filter((value) => value.length === 4 && value.every(Number.isFinite));
      if (!validBounds.length) return;
      const x = (Math.min(...validBounds.map((value) => value[0])) + Math.max(...validBounds.map((value) => value[2]))) / 2;
      const y = (Math.min(...validBounds.map((value) => value[1])) + Math.max(...validBounds.map((value) => value[3]))) / 2;
      const marker = document.createElementNS("http://www.w3.org/2000/svg", "g");
      marker.setAttribute("class", "candidate-marker");
      marker.setAttribute("tabindex", "0");
      marker.setAttribute("role", "button");
      marker.dataset.x = String(x); marker.dataset.y = String(y);
      marker.setAttribute("transform", `translate(${x} ${y})`);
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle"); circle.setAttribute("r", "14");
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text"); text.setAttribute("y", "4"); text.textContent = String(index + 1);
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title"); title.textContent = `${item.district} ${item.dong} · ${item.action}`;
      marker.append(circle, text, title);
      marker.addEventListener("click", () => selectRegion(item.district, item.dong));
      marker.addEventListener("keydown", (event) => { if (event.key === "Enter") selectRegion(item.district, item.dong); });
      candidateLayer.append(marker); candidateMarkers.push(marker);
    });
    if (!ranked.length) {
      const empty = document.createElement("li"); empty.textContent = "현재 필터에서 확인 가능한 후보가 없습니다."; list.append(empty);
    }
  }

  function matchingGrids(district, dong) {
    return grids.filter((node) => (!district || node.dataset.district === district)
      && (!dong || node.dataset.dong === dong)
      && (!filters.period.value || node.dataset.period === filters.period.value));
  }
  function numeric(node, key) {
    const value = Number(node.dataset[key]);
    return Number.isFinite(value) ? value : null;
  }
  function formatNumber(value, digits = 0) {
    return Number.isFinite(value) ? value.toLocaleString("ko-KR", { maximumFractionDigits: digits }) : "자료 없음";
  }

  function renderRegionSummary(district = filters.district.value, dong = filters.dong.value) {
    const nodes = matchingGrids(district, dong);
    const name = [district || "부산 전체", dong].filter(Boolean).join(" · ");
    const sum = (key) => nodes.reduce((total, node) => total + (numeric(node, key) || 0), 0);
    const known = (key) => nodes.map((node) => numeric(node, key)).filter((value) => value !== null);
    const facilityCount = sum("mappedFacilityCount");
    const agedCount = sum("agedCount");
    const ageKnown = sum("ageKnown");
    const roomCount = sum("roomCount");
    const gapValues = known("tourismSupplyGap");
    const gap = gapValues.length ? gapValues.reduce((a, b) => a + b, 0) / gapValues.length : null;
    const occupied = nodes.filter((node) => (numeric(node, "mappedFacilityCount") || 0) > 0).length;
    document.getElementById("region-summary-title").textContent = `${name} · 선택 지역 상세`;
    document.getElementById("region-facility-count").textContent = `${formatNumber(facilityCount)}개`;
    document.getElementById("region-aged-count").textContent = `${formatNumber(agedCount)}개 / ${formatNumber(ageKnown)}개 확인`;
    document.getElementById("region-room-count").textContent = `${formatNumber(roomCount)}실`;
    document.getElementById("region-gap-score").textContent = gap === null ? "자료 없음" : `${formatNumber(gap, 1)}점`;
    const summaries = {
      policy_priority: `${name}의 500m 후보지역은 공급부족·노후시설·신규공급 또는 개선 권고가 함께 나타난 곳입니다. 구 전체 판정이 아니라 읍면동과 세부 생활권을 좁히는 선별 근거입니다.`,
      tourism_supply_gap: `${name}의 공급부족도는 방문수요 점수에서 객실공급 점수를 뺀 값입니다. 색이 진한 500m 지역일수록 수요 신호에 비해 확인 객실이 상대적으로 적습니다.`,
      facility_density: `${name}에는 주소가 확인된 숙박시설 ${formatNumber(facilityCount)}개가 ${formatNumber(occupied)}개 500m 지역에 분포합니다. 진한 영역을 클릭해 시설 수와 노후시설 수를 함께 비교하세요.`,
      aged_facilities: `${name}에서 건물연수가 확인된 ${formatNumber(ageKnown)}개 중 20년 이상 시설은 ${formatNumber(agedCount)}개입니다. 표본 수와 함께 리모델링 후보 집중지역을 검토해야 합니다.`,
      facility_locations: `${name}의 위치 확인 시설은 ${formatNumber(facilityCount)}개입니다. 확대하면 읍면동 숫자 원이 개별 시설점으로 바뀝니다.`,
    };
    document.getElementById("region-summary-text").textContent = summaries[activeLayer];
    document.getElementById("region-ai-result").hidden = true;
  }

  function renderFacilitySummary(node) {
    const name = node.dataset.publicName || "숙박시설";
    const rooms = Number(node.dataset.roomCount);
    const age = Number(node.dataset.buildingAge);
    document.getElementById("region-summary-title").textContent = `${name} · 개별 시설 상세`;
    document.getElementById("region-facility-count").textContent = "1개 시설";
    document.getElementById("region-aged-count").textContent = Number.isFinite(age) ? `${formatNumber(age, 1)}년` : "자료 없음";
    document.getElementById("region-room-count").textContent = Number.isFinite(rooms) ? `${formatNumber(rooms)}실` : "자료 없음";
    document.getElementById("region-gap-score").textContent = "지역 카드 참조";
    document.getElementById("region-summary-text").textContent = `${node.dataset.publicAddress || "주소 자료 없음"} · ${node.dataset.district || ""} ${node.dataset.dong || ""}. 개별 시설의 적합성·권리관계·사업성은 별도 확인이 필요합니다.`;
    document.getElementById("region-ai-result").hidden = true;
  }

  function apply() {
    filterable.forEach((node) => node.classList.toggle("is-filtered", !visible(node)));
    document.getElementById("visible-grid-count").textContent = grids.filter(visible).length;
    document.getElementById("visible-facility-count").textContent = facilities.filter(visible).length;
    setLayerEncoding();
    buildCandidateRanking();
    renderRegionSummary();
    updateOverlayScale();
  }

  const viewport = document.getElementById("map-viewport");
  const svg = document.getElementById("spatial-map");
  let scale = 1;
  let tx = 0;
  let ty = 0;

  function updateOverlayScale() {
    const inverse = 1 / scale;
    [...clusters, ...policyLabels, ...candidateMarkers].forEach((node) => node.setAttribute("transform", `translate(${node.dataset.x} ${node.dataset.y}) scale(${inverse})`));
    facilities.forEach((node) => node.setAttribute("r", String(Number(node.dataset.baseRadius || 3) * inverse)));
  }
  function transform() {
    viewport.setAttribute("transform", `translate(${tx} ${ty}) scale(${scale})`);
    document.body.classList.toggle("detail-mode", scale >= 2.25);
    updateOverlayScale();
  }
  function focusSelection(district, dong) {
    const bounds = matchingGrids(district, dong).map((node) => node.dataset.mapBounds.split(",").map(Number)).filter((item) => item.length === 4 && item.every(Number.isFinite));
    if (!bounds.length || (!district && !dong)) { scale = 1; tx = 0; ty = 0; transform(); return; }
    const minX = Math.min(...bounds.map((item) => item[0]));
    const minY = Math.min(...bounds.map((item) => item[1]));
    const maxX = Math.max(...bounds.map((item) => item[2]));
    const maxY = Math.max(...bounds.map((item) => item[3]));
    scale = Math.max(1, Math.min(4, Math.min(820 / Math.max(40, maxX - minX), 560 / Math.max(40, maxY - minY))));
    tx = 500 - ((minX + maxX) / 2) * scale;
    ty = 350 - ((minY + maxY) / 2) * scale;
    transform();
  }
  function selectRegion(district, dong) {
    filters.district.value = district || "";
    refreshDongOptions();
    filters.dong.value = dong || "";
    apply();
    focusSelection(district, dong);
  }
  grids.forEach((node) => {
    node.addEventListener("click", () => selectRegion(node.dataset.district, node.dataset.dong));
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") selectRegion(node.dataset.district, node.dataset.dong);
    });
  });
  clusters.forEach((node) => node.addEventListener("click", () => selectRegion(node.dataset.district, node.dataset.dong)));
  facilities.forEach((node) => node.addEventListener("click", (event) => { event.stopPropagation(); renderFacilitySummary(node); }));
  filters.district.addEventListener("change", () => { refreshDongOptions(); apply(); focusSelection(filters.district.value, ""); });
  filters.dong.addEventListener("change", () => { apply(); focusSelection(filters.district.value, filters.dong.value); });
  filters.period.addEventListener("change", () => { apply(); focusSelection(filters.district.value, filters.dong.value); });
  layerButtons.forEach((button) => button.addEventListener("click", () => {
    activeLayer = button.dataset.layer;
    layerButtons.forEach((node) => node.classList.toggle("is-active", node === button));
    apply();
  }));

  function zoomAt(delta, anchorX = 500, anchorY = 350) {
    const previous = scale;
    scale = Math.max(.75, Math.min(4, scale * delta));
    const ratio = scale / previous;
    tx = anchorX - (anchorX - tx) * ratio;
    ty = anchorY - (anchorY - ty) * ratio;
    transform();
  }
  document.getElementById("zoom-in").addEventListener("click", () => zoomAt(1.25));
  document.getElementById("zoom-out").addEventListener("click", () => zoomAt(.8));
  document.getElementById("zoom-reset").addEventListener("click", () => { scale = 1; tx = 0; ty = 0; transform(); });
  svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    const box = svg.getBoundingClientRect();
    const anchorX = (event.clientX - box.left) * 1000 / box.width;
    const anchorY = (event.clientY - box.top) * 700 / box.height;
    zoomAt(event.deltaY < 0 ? 1.1 : .9, anchorX, anchorY);
  }, { passive: false });
  function svgPoint(event) {
    const box = svg.getBoundingClientRect();
    return [(event.clientX - box.left) * 1000 / box.width, (event.clientY - box.top) * 700 / box.height];
  }
  let origin = null;
  svg.addEventListener("pointerdown", (event) => { const point = svgPoint(event); origin = [point[0] - tx, point[1] - ty]; svg.setPointerCapture(event.pointerId); });
  svg.addEventListener("pointermove", (event) => { if (origin) { const point = svgPoint(event); tx = point[0] - origin[0]; ty = point[1] - origin[1]; transform(); } });
  svg.addEventListener("pointerup", () => { origin = null; });

  document.getElementById("region-ai-button").addEventListener("click", async () => {
    const result = document.getElementById("region-ai-result");
    const district = filters.district.value;
    const region = !district ? "all" : westDistricts.has(district) ? "west" : eastDistricts.has(district) ? "east" : "other";
    result.hidden = false;
    result.textContent = "검증된 발행지표로 권역 해석을 생성하고 있습니다.";
    try {
      const dashboard = await fetch("/tourism/data.json", { cache: "no-store" }).then((response) => response.json());
      const response = await fetch("/tourism/api/insights", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ region, period: "latest", published_run: dashboard.publishedRun }),
      });
      if (!response.ok) throw new Error("insight unavailable");
      const insight = await response.json();
      result.replaceChildren();
      const title = document.createElement("strong"); title.textContent = insight.headline;
      const summary = document.createElement("p"); summary.textContent = insight.executive_summary;
      const note = document.createElement("small"); note.textContent = "선택 구의 개별 판정이 아닌 해당 권역의 발행지표 기반 AI 해석입니다.";
      result.append(title, summary, note);
    } catch (_) {
      result.textContent = "AI 권역 해석을 불러오지 못했습니다. 위 지역별 산식 해석은 계속 사용할 수 있습니다.";
    }
  });

  apply();
  transform();
})();
