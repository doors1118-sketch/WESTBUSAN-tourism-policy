(() => {
  "use strict";

  const TILE_SIZE = 256;
  const BASE_ZOOM = 10;
  const MIN_ZOOM = 7;
  const MAX_ZOOM = 19;
  const slippyMap = document.getElementById("slippy-map");
  const tileLayer = document.getElementById("vworld-tile-layer");
  const svg = document.getElementById("spatial-map");
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
  const tileTemplate = slippyMap.dataset.tileTemplate;
  const tileNodes = new Map();
  let candidateMarkers = [];
  let activeLayer = "policy_priority";
  let selectedGridNode = null;
  let dragOrigin = null;

  function worldPixel(lon, lat, zoom) {
    const size = TILE_SIZE * (2 ** zoom);
    const clippedLat = Math.max(-85.05112878, Math.min(85.05112878, lat));
    const sin = Math.sin(clippedLat * Math.PI / 180);
    return [
      (lon + 180) / 360 * size,
      (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size,
    ];
  }

  function lonLatFromWorld(x, y, zoom) {
    const size = TILE_SIZE * (2 ** zoom);
    const lon = x / size * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y / size;
    return [lon, 180 / Math.PI * Math.atan(Math.sinh(n))];
  }

  const initialCenter = slippyMap.dataset.mapCenter.split(",").map(Number);
  const initialWorld = worldPixel(initialCenter[0], initialCenter[1], BASE_ZOOM);
  const mapState = { zoom: BASE_ZOOM, centerX: initialWorld[0], centerY: initialWorld[1] };

  function tileUrl(z, x, y) {
    return tileTemplate.replace("{z}", z).replace("{x}", x).replace("{y}", y);
  }

  function renderTiles(width, height) {
    const left = mapState.centerX - width / 2;
    const top = mapState.centerY - height / 2;
    const limit = 2 ** mapState.zoom;
    const minX = Math.floor(left / TILE_SIZE);
    const maxX = Math.floor((left + width) / TILE_SIZE);
    const minY = Math.max(0, Math.floor(top / TILE_SIZE));
    const maxY = Math.min(limit - 1, Math.floor((top + height) / TILE_SIZE));
    const visibleKeys = new Set();
    for (let tileY = minY; tileY <= maxY; tileY += 1) {
      for (let tileX = minX; tileX <= maxX; tileX += 1) {
        const wrappedX = ((tileX % limit) + limit) % limit;
        const key = `${mapState.zoom}/${wrappedX}/${tileY}`;
        visibleKeys.add(key);
        let image = tileNodes.get(key);
        if (!image) {
          image = new Image();
          image.alt = "";
          image.decoding = "async";
          image.draggable = false;
          image.src = tileUrl(mapState.zoom, wrappedX, tileY);
          tileNodes.set(key, image);
          tileLayer.append(image);
        }
        image.style.left = `${tileX * TILE_SIZE - left}px`;
        image.style.top = `${tileY * TILE_SIZE - top}px`;
      }
    }
    tileNodes.forEach((image, key) => {
      if (!visibleKeys.has(key)) {
        image.remove();
        tileNodes.delete(key);
      }
    });
  }

  function mapCoordinate(lon, lat) {
    const point = worldPixel(lon, lat, BASE_ZOOM);
    return [point[0] - initialWorld[0] + 500, point[1] - initialWorld[1] + 350];
  }

  function updateOverlayScale() {
    const scale = 2 ** (mapState.zoom - BASE_ZOOM);
    const inverse = 1 / scale;
    [...clusters, ...policyLabels, ...candidateMarkers].forEach((node) => {
      node.setAttribute("transform", `translate(${node.dataset.x} ${node.dataset.y}) scale(${inverse})`);
    });
    facilities.forEach((node) => node.setAttribute("r", String(Number(node.dataset.baseRadius || 3) * inverse)));
    document.body.classList.toggle("detail-mode", mapState.zoom >= 15);
  }

  function renderMap() {
    const width = Math.max(320, slippyMap.clientWidth);
    const height = Math.max(320, slippyMap.clientHeight);
    renderTiles(width, height);
    const center = lonLatFromWorld(mapState.centerX, mapState.centerY, mapState.zoom);
    const mapCenter = mapCoordinate(center[0], center[1]);
    const scale = 2 ** (mapState.zoom - BASE_ZOOM);
    svg.setAttribute("viewBox", `${mapCenter[0] - width / (2 * scale)} ${mapCenter[1] - height / (2 * scale)} ${width / scale} ${height / scale}`);
    svg.setAttribute("preserveAspectRatio", "none");
    slippyMap.dataset.currentZoom = String(mapState.zoom);
    updateOverlayScale();
  }

  function setCenter(lon, lat, zoom = mapState.zoom) {
    mapState.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(zoom)));
    [mapState.centerX, mapState.centerY] = worldPixel(lon, lat, mapState.zoom);
    renderMap();
  }

  function fitGeographicBounds(bounds, maximumZoom = MAX_ZOOM) {
    if (!bounds || bounds.length !== 4 || !bounds.every(Number.isFinite)) return;
    const [minLon, minLat, maxLon, maxLat] = bounds;
    const width = Math.max(320, slippyMap.clientWidth) * 0.72;
    const height = Math.max(320, slippyMap.clientHeight) * 0.72;
    let zoom = Math.min(MAX_ZOOM, maximumZoom);
    while (zoom > MIN_ZOOM) {
      const northWest = worldPixel(minLon, maxLat, zoom);
      const southEast = worldPixel(maxLon, minLat, zoom);
      if (Math.abs(southEast[0] - northWest[0]) <= width && Math.abs(southEast[1] - northWest[1]) <= height) break;
      zoom -= 1;
    }
    setCenter((minLon + maxLon) / 2, (minLat + maxLat) / 2, zoom);
  }

  function geographicBounds(nodes) {
    const values = nodes.map((node) => (node.dataset.geoBounds || "").split(",").map(Number))
      .filter((item) => item.length === 4 && item.every(Number.isFinite));
    if (!values.length) return null;
    return [
      Math.min(...values.map((v) => v[0])), Math.min(...values.map((v) => v[1])),
      Math.max(...values.map((v) => v[2])), Math.max(...values.map((v) => v[3])),
    ];
  }

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
    addOptions(filters.dong, grids.filter((node) => !filters.district.value || node.dataset.district === filters.district.value)
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

  function numeric(node, key) {
    const value = Number(node.dataset[key]);
    return Number.isFinite(value) ? value : null;
  }

  function formatNumber(value, digits = 0) {
    return Number.isFinite(value) ? value.toLocaleString("ko-KR", { maximumFractionDigits: digits }) : "자료 없음";
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
    return raw === undefined || raw === "" ? Number.NaN : Number(raw);
  }

  function metricColour(value) {
    if (!Number.isFinite(value)) return "#8b959d";
    if (activeLayer === "facility_density") return value >= 20 ? "#762a83" : value >= 8 ? "#9970ab" : value >= 3 ? "#c2a5cf" : value >= 1 ? "#e7d4e8" : "#eef1f3";
    if (activeLayer === "aged_facilities") return value >= 10 ? "#8c2d04" : value >= 5 ? "#d94801" : value >= 2 ? "#f16913" : value >= 1 ? "#fdae6b" : "#eef1f3";
    return value >= 75 ? "#b2182b" : value >= 50 ? "#ef8a62" : value >= 25 ? "#fddbc7" : "#67a9cf";
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
      policy_priority: "서부산 500m 단위 수요·공급·노후 신호로 후보지역을 좁힙니다. 번호를 누르면 해당 생활권의 근거와 AI 정책 아이디어가 열립니다.",
      tourism_supply_gap: "공급부족도 = 구별 방문수요 점수 − 해당 500m 객실공급 점수입니다. 값이 클수록 수요에 비해 확인 객실이 적습니다.",
      facility_density: "500m 안의 주소 확인 숙박시설 수입니다. 진한 지역을 클릭하면 정확한 개수와 객실·노후 표본을 확인합니다.",
      aged_facilities: "500m 안의 20년 이상 숙박시설 수입니다. 색상 영역을 클릭하면 건물연수 확인 표본을 함께 표시합니다.",
      facility_locations: "숙박시설 위치입니다. 15레벨 이상 확대하면 묶음 대신 개별 시설점을 표시합니다.",
    };
    document.getElementById("layer-explainer").textContent = explanations[activeLayer];
    const legends = {
      policy_priority: [["#c53b2d", "신규공급 후보"], ["#e98528", "노후시설 개선·전환 후보"], ["#667784", "추가 근거 확인 필요"]],
      tourism_supply_gap: [["#b2182b", "75 이상 · 매우 높음"], ["#ef8a62", "50–74 · 높음"], ["#fddbc7", "25–49 · 검토"], ["#67a9cf", "0–24 · 낮음"]],
      facility_density: [["#762a83", "20개 이상"], ["#9970ab", "8–19개"], ["#c2a5cf", "3–7개"], ["#e7d4e8", "1–2개"]],
      aged_facilities: [["#8c2d04", "20년 이상 10개+"], ["#d94801", "5–9개"], ["#f16913", "2–4개"], ["#fdae6b", "1개"]],
      facility_locations: [["#0d3b59", "저배율 · 읍면동 묶음"], ["#496173", "15레벨+ · 개별 시설"]],
    };
    setLegend(legends[activeLayer]);
  }

  function matchingGrids(district, dong) {
    return grids.filter((node) => (!district || node.dataset.district === district)
      && (!dong || node.dataset.dong === dong)
      && (!filters.period.value || node.dataset.period === filters.period.value));
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
    document.getElementById("region-summary-title").textContent = `${name} · 선택 지역 상세`;
    document.getElementById("region-facility-count").textContent = `${formatNumber(facilityCount)}개`;
    document.getElementById("region-aged-count").textContent = `${formatNumber(agedCount)}개 / ${formatNumber(ageKnown)}개 확인`;
    document.getElementById("region-room-count").textContent = `${formatNumber(roomCount)}실`;
    document.getElementById("region-gap-score").textContent = gap === null ? "자료 없음" : `${formatNumber(gap, 1)}점`;
    document.getElementById("region-summary-text").textContent = `${name} 집계입니다. 색상 영역이나 후보 번호를 선택하면 500m 단위로 좁혀 정확한 공급·노후·수요 신호를 확인할 수 있습니다.`;
    document.getElementById("region-ai-result").hidden = true;
  }

  function renderGridSummary(node) {
    const name = `${node.dataset.district} ${node.dataset.dong}`;
    const facilityCount = numeric(node, "mappedFacilityCount") || 0;
    const aged = numeric(node, "agedCount") || 0;
    const known = numeric(node, "ageKnown") || 0;
    const rooms = numeric(node, "roomCount") || 0;
    const gap = numeric(node, "tourismSupplyGap");
    document.getElementById("region-summary-title").textContent = `${name} · 500m 후보지역 상세`;
    document.getElementById("region-facility-count").textContent = `${formatNumber(facilityCount)}개`;
    document.getElementById("region-aged-count").textContent = `${formatNumber(aged)}개 / ${formatNumber(known)}개 확인`;
    document.getElementById("region-room-count").textContent = `${formatNumber(rooms)}실`;
    document.getElementById("region-gap-score").textContent = gap === null ? "자료 없음" : `${formatNumber(gap, 1)}점`;
    const action = { new_supply: "신규 관광숙박 공급", remodel: "노후시설 개선·전환", investment_caution: "추가 근거 확인" }[node.dataset.recommendation] || "정책 검토";
    document.getElementById("region-summary-text").textContent = `${name}의 500m 분석지역입니다. 현재 권고는 ${action}이며, 수요·공급·노후 지표를 함께 사용한 1차 검토 결과입니다.`;
  }

  function renderFacilitySummary(node) {
    const rooms = numeric(node, "roomCount");
    const age = numeric(node, "buildingAge");
    document.getElementById("region-summary-title").textContent = `${node.dataset.publicName || "숙박시설"} · 개별 시설 상세`;
    document.getElementById("region-facility-count").textContent = "1개 시설";
    document.getElementById("region-aged-count").textContent = age === null ? "자료 없음" : `${formatNumber(age, 1)}년`;
    document.getElementById("region-room-count").textContent = rooms === null ? "자료 없음" : `${formatNumber(rooms)}실`;
    document.getElementById("region-gap-score").textContent = "지역 카드 참조";
    document.getElementById("region-summary-text").textContent = `${node.dataset.publicAddress || "주소 자료 없음"} · 개별 시설의 적합성·권리관계·사업성은 별도 확인이 필요합니다.`;
    document.getElementById("region-ai-result").hidden = true;
  }

  function buildCandidateRanking() {
    const ranked = grids.filter((node) => westDistricts.has(node.dataset.district) && visible(node) && node.dataset.recommendation)
      .map((node) => {
        const kind = node.dataset.recommendation;
        const signal = kind === "new_supply" ? 3 : kind === "remodel" ? 2 : 1;
        const gap = numeric(node, "tourismSupplyGap") || 0;
        const aged = numeric(node, "agedCount") || 0;
        const facilityCount = numeric(node, "mappedFacilityCount") || 0;
        const action = kind === "new_supply" ? "신규 관광숙박 공급 검토" : kind === "remodel" ? "노후시설 개선·전환 검토" : "추가 근거 확인";
        return {
          node,
          gridKey: node.dataset.key,
          district: node.dataset.district,
          dong: node.dataset.dong,
          kind,
          gap,
          aged,
          facilities: facilityCount,
          action,
          score: signal * 1000 + gap * 10 + aged + facilityCount * .1,
        };
      }).sort((a, b) => b.score - a.score || a.gridKey.localeCompare(b.gridKey)).slice(0, 5);
    const list = document.getElementById("candidate-rank-list");
    list.replaceChildren();
    candidateLayer.replaceChildren();
    candidateMarkers = [];
    ranked.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      const rank = document.createElement("b");
      rank.textContent = String(index + 1);
      const label = document.createElement("span");
      label.textContent = `${item.district} ${item.dong}`;
      const detail = document.createElement("small");
      detail.textContent = `${item.action} · 시설 ${formatNumber(item.facilities)}개 · 20년+ ${formatNumber(item.aged)}개`;
      label.append(detail);
      button.append(rank, label);
      button.addEventListener("click", () => selectCandidate(item));
      list.append(button);
      const bounds = (item.node.dataset.mapBounds || "").split(",").map(Number);
      if (bounds.length !== 4 || !bounds.every(Number.isFinite)) return;
      const x = (bounds[0] + bounds[2]) / 2;
      const y = (bounds[1] + bounds[3]) / 2;
      const marker = document.createElementNS("http://www.w3.org/2000/svg", "g");
      marker.setAttribute("class", "candidate-marker");
      marker.setAttribute("tabindex", "0");
      marker.setAttribute("role", "button");
      marker.dataset.x = String(x);
      marker.dataset.y = String(y);
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("r", "14");
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("y", "4");
      text.textContent = String(index + 1);
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${item.district} ${item.dong} · ${item.action}`;
      marker.append(circle, text, title);
      marker.addEventListener("click", () => selectCandidate(item));
      marker.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") selectCandidate(item);
      });
      candidateLayer.append(marker);
      candidateMarkers.push(marker);
    });
    if (!ranked.length) {
      const empty = document.createElement("li");
      empty.textContent = "현재 필터에서 확인 가능한 후보가 없습니다.";
      list.append(empty);
    }
  }

  function apply() {
    filterable.forEach((node) => node.classList.toggle("is-filtered", !visible(node)));
    document.getElementById("visible-grid-count").textContent = grids.filter(visible).length;
    document.getElementById("visible-facility-count").textContent = facilities.filter(visible).length;
    setLayerEncoding();
    buildCandidateRanking();
    renderRegionSummary();
    renderMap();
  }

  function focusSelection(district, dong) {
    const nodes = matchingGrids(district, dong);
    if (!district && !dong) {
      setCenter(initialCenter[0], initialCenter[1], BASE_ZOOM);
      return;
    }
    fitGeographicBounds(geographicBounds(nodes), dong ? 15 : 13);
  }

  function selectRegion(district, dong) {
    selectedGridNode = null;
    filters.district.value = district || "";
    refreshDongOptions();
    filters.dong.value = dong || "";
    apply();
    focusSelection(district, dong);
  }

  function buildSelectionContext(node) {
    const safe = (key) => numeric(node, key);
    return {
      grid_id: node.dataset.key,
      district: node.dataset.district,
      dong: node.dataset.dong,
      facility_count: Math.max(0, Math.round(safe("mappedFacilityCount") || 0)),
      aged_facility_count: Math.max(0, Math.round(safe("agedCount") || 0)),
      age_known_count: Math.max(0, Math.round(safe("ageKnown") || 0)),
      room_count: Math.max(0, safe("roomCount") || 0),
      supply_gap_score: safe("tourismSupplyGap"),
      demand_score: safe("demandScore"),
      supply_score: safe("supplyScore"),
      recommendation_kind: node.dataset.recommendation || "investment_caution",
    };
  }

  async function requestRegionInsight(node = selectedGridNode) {
    const result = document.getElementById("region-ai-result");
    const district = node ? node.dataset.district : filters.district.value;
    const region = !district ? "all" : westDistricts.has(district) ? "west" : eastDistricts.has(district) ? "east" : "other";
    const selectionContext = node ? buildSelectionContext(node) : null;
    result.hidden = false;
    result.textContent = selectionContext ? "선택한 500m 지역의 발행지표로 정책 아이디어를 생성하고 있습니다." : "검증된 발행지표로 권역 해석을 생성하고 있습니다.";
    try {
      const dashboard = await fetch("/tourism/data.json", { cache: "no-store" }).then((response) => response.json());
      const response = await fetch("/tourism/api/insights", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          region,
          period: "latest",
          published_run: dashboard.publishedRun,
          selection: selectionContext,
        }),
      });
      if (!response.ok) throw new Error("insight unavailable");
      const insight = await response.json();
      result.replaceChildren();
      const title = document.createElement("strong");
      title.textContent = insight.headline;
      const summary = document.createElement("p");
      summary.textContent = insight.executive_summary;
      const heading = document.createElement("h3");
      heading.textContent = "정책 아이디어";
      const options = document.createElement("ol");
      [...insight.policy_options].sort((a, b) => a.priority_rank - b.priority_rank).forEach((option) => {
        const item = document.createElement("li");
        const action = document.createElement("b");
        action.textContent = `${option.priority_rank}. ${option.action}`;
        const rationale = document.createElement("p");
        rationale.textContent = `${option.target_area} · ${option.rationale}`;
        const caveat = document.createElement("small");
        caveat.textContent = `확인 필요: ${option.caveat}`;
        item.append(action, rationale, caveat);
        options.append(item);
      });
      const note = document.createElement("small");
      note.textContent = "데이터로 확인된 사실과 정책 아이디어를 구분한 AI 보조 해석이며 사업성·인허가의 최종 판단이 아닙니다.";
      result.append(title, summary, heading, options, note);
    } catch (_) {
      result.textContent = "AI 정책 해석을 불러오지 못했습니다. 위 500m 상세 지표는 계속 사용할 수 있습니다.";
    }
  }

  function selectCandidate(item) {
    selectedGridNode = item.node;
    filters.district.value = item.district;
    refreshDongOptions();
    filters.dong.value = item.dong;
    filterable.forEach((node) => node.classList.toggle("is-filtered", !visible(node)));
    grids.forEach((node) => node.classList.toggle("is-selected", node === item.node));
    setLayerEncoding();
    renderGridSummary(item.node);
    fitGeographicBounds((item.node.dataset.geoBounds || "").split(",").map(Number), 17);
    requestRegionInsight(item.node);
  }

  grids.forEach((node) => {
    node.addEventListener("click", () => {
      selectedGridNode = node;
      renderGridSummary(node);
      fitGeographicBounds((node.dataset.geoBounds || "").split(",").map(Number), 17);
      requestRegionInsight(node);
    });
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        selectedGridNode = node;
        renderGridSummary(node);
        requestRegionInsight(node);
      }
    });
  });
  clusters.forEach((node) => node.addEventListener("click", () => selectRegion(node.dataset.district, node.dataset.dong)));
  facilities.forEach((node) => node.addEventListener("click", (event) => {
    event.stopPropagation();
    renderFacilitySummary(node);
  }));
  filters.district.addEventListener("change", () => {
    refreshDongOptions();
    apply();
    focusSelection(filters.district.value, "");
  });
  filters.dong.addEventListener("change", () => {
    apply();
    focusSelection(filters.district.value, filters.dong.value);
  });
  filters.period.addEventListener("change", () => {
    apply();
    focusSelection(filters.district.value, filters.dong.value);
  });
  layerButtons.forEach((button) => button.addEventListener("click", () => {
    activeLayer = button.dataset.layer;
    layerButtons.forEach((node) => node.classList.toggle("is-active", node === button));
    apply();
  }));
  document.getElementById("region-ai-button").addEventListener("click", () => requestRegionInsight());

  function zoomAt(nextZoom, clientX, clientY) {
    const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(nextZoom)));
    if (zoom === mapState.zoom) return;
    const rect = slippyMap.getBoundingClientRect();
    const px = clientX === undefined ? rect.width / 2 : clientX - rect.left;
    const py = clientY === undefined ? rect.height / 2 : clientY - rect.top;
    const anchor = lonLatFromWorld(
      mapState.centerX + px - rect.width / 2,
      mapState.centerY + py - rect.height / 2,
      mapState.zoom,
    );
    const anchorAtZoom = worldPixel(anchor[0], anchor[1], zoom);
    mapState.zoom = zoom;
    mapState.centerX = anchorAtZoom[0] - px + rect.width / 2;
    mapState.centerY = anchorAtZoom[1] - py + rect.height / 2;
    renderMap();
  }

  document.getElementById("zoom-in").addEventListener("click", () => zoomAt(mapState.zoom + 1));
  document.getElementById("zoom-out").addEventListener("click", () => zoomAt(mapState.zoom - 1));
  document.getElementById("zoom-reset").addEventListener("click", () => setCenter(initialCenter[0], initialCenter[1], BASE_ZOOM));
  slippyMap.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoomAt(mapState.zoom + (event.deltaY < 0 ? 1 : -1), event.clientX, event.clientY);
  }, { passive: false });
  slippyMap.addEventListener("pointerdown", (event) => {
    dragOrigin = { x: event.clientX, y: event.clientY, centerX: mapState.centerX, centerY: mapState.centerY };
    slippyMap.setPointerCapture(event.pointerId);
  });
  slippyMap.addEventListener("pointermove", (event) => {
    if (!dragOrigin) return;
    mapState.centerX = dragOrigin.centerX - (event.clientX - dragOrigin.x);
    mapState.centerY = dragOrigin.centerY - (event.clientY - dragOrigin.y);
    renderMap();
  });
  slippyMap.addEventListener("pointerup", () => { dragOrigin = null; });
  slippyMap.addEventListener("pointercancel", () => { dragOrigin = null; });
  window.addEventListener("resize", renderMap);

  apply();
})();
