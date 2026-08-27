(() => {
  "use strict";

  const TILE_SIZE = 256;
  const BASE_ZOOM = 10;
  const MIN_ZOOM = 7;
  const MAX_ZOOM = 19;
  const slippyMap = document.getElementById("slippy-map");
  const bundle = JSON.parse(document.getElementById("bundle-data").textContent);
  const tileLayer = document.getElementById("vworld-tile-layer");
  const svg = document.getElementById("spatial-map");
  const grids = [...document.querySelectorAll(".grid-feature")];
  const facilities = [...document.querySelectorAll(".facility-feature")];
  const clusters = [...document.querySelectorAll(".facility-cluster")];
  const policyLabels = [...document.querySelectorAll(".district-policy-label")];
  const tourismPois = [...document.querySelectorAll(".tourism-poi-marker")];
  const tourismPoiOverlay = document.getElementById("tourism-poi-overlay");
  const tourismPoiPopup = document.getElementById("tourism-poi-popup");
  const poiFilterButtons = [...document.querySelectorAll("[data-poi-filter]")];
  const poiFilterStatus = document.getElementById("spatial-poi-filter-status");
  const filterable = [...grids, ...facilities, ...clusters, ...policyLabels, ...tourismPois];
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
  let activePoiFilter = "all";
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
    tourismPois.forEach((node) => node.setAttribute("r", String(Number(node.dataset.baseRadius || 5) * inverse)));
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
    const isTourismPoi = node.classList.contains("tourism-poi-marker");
    if (filters.district.value && node.dataset.district !== filters.district.value) return false;
    if (isTourismPoi && activePoiFilter !== "all" && node.dataset.poiGroup !== activePoiFilter) return false;
    // POI 읍면동 값은 도로명주소 정규화 결과와 분석격자의 법정동이 다를 수 있다.
    // 후보 선택 시에는 해당 자치구 POI를 유지하고 실제 거리는 선택지점에서 계산한다.
    if (!isTourismPoi && filters.dong.value && node.dataset.dong !== filters.dong.value) return false;
    if (filters.period.value && node.dataset.period && node.dataset.period !== filters.period.value) return false;
    return true;
  }

  function updatePoiFilterStatus() {
    const labels = { all: "전체", festival: "축제·행사", food: "식당·음식", tourism_culture: "관광·문화시설", leisure_course: "레포츠·여행코스", lodging_shopping: "숙박·쇼핑", other: "기타 관광정보" };
    const districtPois = tourismPois.filter((node) => !filters.district.value || node.dataset.district === filters.district.value);
    const visibleCount = districtPois.filter((node) => activePoiFilter === "all" || node.dataset.poiGroup === activePoiFilter).length;
    const scope = filters.district.value || "부산 전체";
    poiFilterStatus.textContent = `${scope} · ${labels[activePoiFilter] || labels.all} ${visibleCount.toLocaleString("ko-KR")}개 표시`;
  }

  function setPoiFilter(group) {
    const allowed = new Set(["all", "festival", "food", "tourism_culture", "leisure_course", "lodging_shopping", "other"]);
    activePoiFilter = allowed.has(group) ? group : "all";
    poiFilterButtons.forEach((button) => {
      const selected = button.dataset.poiFilter === activePoiFilter;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    hideTourismPoiPopup();
    apply();
  }

  function numeric(node, key) {
    const value = Number(node.dataset[key]);
    return Number.isFinite(value) ? value : null;
  }

  function formatNumber(value, digits = 0) {
    return Number.isFinite(value) ? value.toLocaleString("ko-KR", { maximumFractionDigits: digits }) : "자료 없음";
  }

  function hideTourismPoiPopup() {
    tourismPoiPopup.hidden = true;
  }

  function showTourismPoiPopup(node, event) {
    document.getElementById("tourism-poi-popup-title").textContent = node.dataset.title || "관광정보";
    const popupType = document.getElementById("tourism-poi-popup-type");
    const group = node.dataset.poiGroup || "other";
    const groupLabels = { festival: "축제·행사", food: "식당·음식", tourism_culture: "관광·문화시설", leisure_course: "레포츠·여행코스", lodging_shopping: "숙박·쇼핑", other: "기타 관광정보" };
    popupType.className = `poi-group-${group}`;
    popupType.textContent = `${groupLabels[group] || groupLabels.other} · ${node.dataset.tourismType || "세부유형 미확인"}`;
    document.getElementById("tourism-poi-popup-location").textContent = [node.dataset.district, node.dataset.dong].filter(Boolean).join(" ") || "소재지역 미확인";
    const mapRect = slippyMap.getBoundingClientRect();
    const markerRect = node.getBoundingClientRect();
    const anchorX = Number.isFinite(event?.clientX) ? event.clientX : markerRect.left + markerRect.width / 2;
    const anchorY = Number.isFinite(event?.clientY) ? event.clientY : markerRect.top + markerRect.height / 2;
    tourismPoiPopup.hidden = false;
    const popupRect = tourismPoiPopup.getBoundingClientRect();
    tourismPoiPopup.style.left = `${Math.max(12, Math.min(mapRect.width - popupRect.width - 12, anchorX - mapRect.left + 12))}px`;
    tourismPoiPopup.style.top = `${Math.max(12, Math.min(mapRect.height - popupRect.height - 12, anchorY - mapRect.top + 12))}px`;
  }

  function haversineMetres(left, right) {
    const radians = (value) => value * Math.PI / 180;
    const earthRadius = 6371008.8;
    const latitudeDelta = radians(right[1] - left[1]);
    const longitudeDelta = radians(right[0] - left[0]);
    const a = Math.sin(latitudeDelta / 2) ** 2
      + Math.cos(radians(left[1])) * Math.cos(radians(right[1]))
      * Math.sin(longitudeDelta / 2) ** 2;
    return earthRadius * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function accessibilityContext(node) {
    const bounds = (node.dataset.geoBounds || "").split(",").map(Number);
    const center = bounds.length === 4 && bounds.every(Number.isFinite)
      ? [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2]
      : null;
    const features = bundle.access_context?.features || [];
    const transport = features.filter((item) => {
      const properties = item.properties || {};
      return properties.kind === "transport_dong"
        && properties.district_name === node.dataset.district
        && properties.dong_name === node.dataset.dong;
    }).sort((a, b) => String(b.properties.period || "").localeCompare(String(a.properties.period || "")))[0];
    const pois = center ? features.filter((item) => item.properties?.kind === "tourism_poi"
      && item.geometry?.type === "Point").map((item) => ({
        item,
        distance: haversineMetres(center, item.geometry.coordinates.map(Number)),
      })).sort((a, b) => a.distance - b.distance) : [];
    const nearest = pois[0] || null;
    return {
      transportInbound: transport
        ? Number(transport.properties.inbound_other_district || transport.properties.inbound_other_dong || 0)
        : null,
      transportPeriod: transport?.properties.period || null,
      nearestPoiName: nearest?.item.properties.title || null,
      nearestPoiDistance: nearest?.distance ?? null,
      poiCount1000m: pois.filter((item) => item.distance <= 1000).length,
    };
  }

  function accessibilitySummary(context) {
    const parts = [];
    const hasTourism = context.poiCount1000m > 0;
    if (context.nearestPoiName) parts.push(`최근접 관광지 ${context.nearestPoiName} ${formatNumber(Math.round(context.nearestPoiDistance))}m`);
    if (context.nearestPoiName) parts.push(`1km 내 관광지 ${formatNumber(context.poiCount1000m)}개`);
    if (context.transportInbound !== null) parts.push(`동 단위 타 자치구 대중교통 유입 ${formatNumber(context.transportInbound)}통행 (${context.transportPeriod})`);
    if (hasTourism && context.transportInbound !== null) parts.push("1km 내 관광지와 교통유입 신호가 함께 확인되어 사업성 추가검토 가치가 있음");
    else if (parts.length) parts.push("접근성 신호가 일부 확인되어 추가 자료와 함께 사업성을 검토할 필요가 있음");
    else parts.push("관광·교통 접근성 자료가 없어 사업성 신호를 판단할 수 없음");
    return parts.join(" · ");
  }

  function policyColour(kind) {
    return {
      new_supply: "#1769aa",
      remodel: "#e67e22",
      quality_upgrade: "#6b5ac6",
      content_first: "#168b89",
      investment_caution: "#68727d",
    }[kind] || "#a3adb5";
  }

  function layerValue(node) {
    let raw = node.dataset.tourismSupplyGap;
    if (activeLayer === "facility_density") raw = node.dataset.mappedFacilityCount;
    if (activeLayer === "aged_facilities") {
      if (Number(node.dataset.ageKnown || 0) <= 0) return Number.NaN;
      raw = node.dataset.agedCount;
    }
    if (activeLayer === "transport_inflow") raw = node.dataset.transportInbound;
    return raw === undefined || raw === "" ? Number.NaN : Number(raw);
  }

  function metricColour(value) {
    if (!Number.isFinite(value)) return "#8b959d";
    if (activeLayer === "facility_density") return value >= 20 ? "#6f1d91" : value >= 8 ? "#1769aa" : value >= 3 ? "#168b89" : value >= 1 ? "#d99b16" : "#eef1f3";
    if (activeLayer === "aged_facilities") return value >= 10 ? "#7f0000" : value >= 5 ? "#d1495b" : value >= 2 ? "#e67e22" : value >= 1 ? "#e5b839" : "#eef1f3";
    return value >= 75 ? "#8e0152" : value >= 50 ? "#d95f02" : value >= 25 ? "#e6b800" : "#2b83ba";
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
    document.body.classList.toggle(
      "tourism-poi-layer",
      activeLayer === "tourism_poi" || tourismPoiOverlay.checked,
    );
    document.body.classList.toggle("candidate-layer", !["facility_locations", "tourism_poi"].includes(activeLayer));
    document.body.dataset.activeLayer = activeLayer;
    grids.forEach((node) => {
      if (activeLayer === "policy_priority") {
        node.style.fill = policyColour(node.dataset.recommendation);
        node.style.fillOpacity = node.dataset.recommendation ? ".78" : ".12";
        node.style.stroke = node.dataset.recommendation === "new_supply" ? "#0b3c5d" : node.dataset.recommendation === "remodel" ? "#7a3e00" : "#4f5963";
        node.style.strokeWidth = node.dataset.recommendation ? "1.2" : ".4";
        node.style.strokeDasharray = node.dataset.recommendation === "remodel" ? "4 2" : node.dataset.recommendation ? "" : "2 3";
      } else if (["facility_locations", "tourism_poi"].includes(activeLayer)) {
        node.style.fill = "#ffffff";
        node.style.fillOpacity = ".012";
        node.style.stroke = "";
        node.style.strokeWidth = "";
        node.style.strokeDasharray = "";
      } else {
        const value = layerValue(node);
        node.style.fill = metricColour(value);
        node.style.fillOpacity = Number.isFinite(value) && value > 0 ? ".8" : ".02";
        node.style.stroke = Number.isFinite(value) && value > 0 ? "#ffffff" : "";
        node.style.strokeWidth = Number.isFinite(value) && value > 0 ? ".65" : "";
        node.style.strokeDasharray = "";
      }
    });
    const explanations = {
      policy_priority: "서부산 500m 단위 수요·공급·노후 신호로 후보지역을 좁힙니다. 번호를 누르면 해당 생활권의 근거와 AI 정책 아이디어가 열립니다.",
      tourism_supply_gap: "공급부족도 = 구별 방문수요 점수 − 해당 500m 객실공급 점수입니다. 값이 클수록 수요에 비해 확인 객실이 적습니다.",
      facility_density: "500m 안의 주소 확인 숙박시설 수입니다. 진한 지역을 클릭하면 정확한 개수와 객실·노후 표본을 확인합니다.",
      aged_facilities: "500m 안의 20년 이상 숙박시설 수입니다. 색상 영역을 클릭하면 건물연수 확인 표본을 함께 표시합니다.",
      facility_locations: "숙박시설 위치입니다. 15레벨 이상 확대하면 묶음 대신 개별 시설점을 표시합니다.",
      transport_inflow: "목적지가 해당 읍면동인 타 자치구 대중교통 통행량입니다. 관광객 수가 아니며 통행 목적과 중복 이용자를 구분하지 못합니다.",
      tourism_poi: "공식 관광정보 API에서 검토된 관광지 위치입니다. 관광지 점을 선택하면 명칭과 행정구역을 확인합니다.",
    };
    document.getElementById("layer-explainer").textContent = explanations[activeLayer];
    const legends = {
      policy_priority: [["#1769aa", "신규공급 후보 · 실선"], ["#e67e22", "노후시설 개선·전환 · 점선"], ["#6b5ac6", "품질개선 후보"], ["#168b89", "콘텐츠 선행 검토"], ["#68727d", "추가 근거 확인"]],
      tourism_supply_gap: [["#8e0152", "75 이상 · 매우 높음"], ["#d95f02", "50–74 · 높음"], ["#e6b800", "25–49 · 검토"], ["#2b83ba", "0–24 · 낮음"]],
      facility_density: [["#6f1d91", "20개 이상"], ["#1769aa", "8–19개"], ["#168b89", "3–7개"], ["#d99b16", "1–2개"]],
      aged_facilities: [["#7f0000", "20년 이상 10개+"], ["#d1495b", "5–9개"], ["#e67e22", "2–4개"], ["#e5b839", "1개"]],
      facility_locations: [["#0d3b59", "저배율 · 읍면동 묶음"], ["#496173", "15레벨+ · 개별 시설"]],
      transport_inflow: [["#8e0152", "유입량 상위"], ["#d95f02", "유입량 중상위"], ["#e6b800", "유입량 중하위"], ["#2b83ba", "유입량 하위"]],
      tourism_poi: [["#d1495b", "축제·행사"], ["#e67e22", "식당·음식"], ["#1769aa", "관광·문화시설"], ["#6b5ac6", "레포츠·여행코스"], ["#168b89", "숙박·쇼핑"], ["#68727d", "기타 관광정보"]],
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
    setRegionMetricLabels({
      first: ["숙박시설 수", "주소 좌표 확인 기준"],
      second: ["20년 이상 시설", `건축연령 확인 ${formatNumber(ageKnown)}개 시설 기준`],
      third: ["확인 객실", "객실 자료 확인분 합계"],
      fourth: ["공급부족도", "방문수요 점수 − 객실공급 점수"],
    });
    document.getElementById("region-summary-title").textContent = `${name} · 선택 지역 상세`;
    document.getElementById("region-facility-count").textContent = `${formatNumber(facilityCount)}개`;
    document.getElementById("region-aged-count").textContent = `${formatNumber(agedCount)}개`;
    document.getElementById("region-room-count").textContent = `${formatNumber(roomCount)}실`;
    document.getElementById("region-gap-score").textContent = gap === null ? "자료 없음" : `${formatNumber(gap, 1)}점`;
    document.getElementById("region-summary-text").textContent = `${name} 집계입니다. 색상 영역이나 후보 번호를 선택하면 500m 단위로 좁혀 정확한 공급·노후·수요 신호를 확인할 수 있습니다.`;
    document.getElementById("region-ai-result").hidden = true;
  }

  function renderGridSummary(node) {
    const name = `${node.dataset.district} ${node.dataset.dong}`;
    const dongNodes = matchingGrids(node.dataset.district, node.dataset.dong);
    const dongSum = (key) => dongNodes.reduce(
      (total, item) => total + (numeric(item, key) || 0), 0,
    );
    const facilityCount = numeric(node, "mappedFacilityCount") || 0;
    const aged = numeric(node, "agedCount") || 0;
    const known = numeric(node, "ageKnown") || 0;
    const rooms = numeric(node, "roomCount") || 0;
    const dongFacilityCount = dongSum("mappedFacilityCount");
    const dongAgedCount = dongSum("agedCount");
    const dongRoomCount = dongSum("roomCount");
    const gap = numeric(node, "tourismSupplyGap");
    setRegionMetricLabels({
      first: ["500m 격자 숙박시설", `${node.dataset.dong} 전체 ${formatNumber(dongFacilityCount)}개`],
      second: ["500m 격자 노후시설", `${node.dataset.dong} 전체 ${formatNumber(dongAgedCount)}개 · 격자 표본 ${formatNumber(known)}개`],
      third: ["500m 격자 확인객실", `${node.dataset.dong} 전체 ${formatNumber(dongRoomCount)}실`],
      fourth: ["공급부족도", "방문수요 점수 − 객실공급 점수"],
    });
    document.getElementById("region-summary-title").textContent = `${name} · 선택 500m 격자 상세`;
    document.getElementById("region-facility-count").textContent = `${formatNumber(facilityCount)}개`;
    document.getElementById("region-aged-count").textContent = `${formatNumber(aged)}개`;
    document.getElementById("region-room-count").textContent = `${formatNumber(rooms)}실`;
    document.getElementById("region-gap-score").textContent = gap === null ? "자료 없음" : `${formatNumber(gap, 1)}점`;
    const action = {
      new_supply: "신규 관광숙박 공급",
      remodel: "노후시설 개선·전환",
      quality_upgrade: "소규모 숙박 품질개선",
      content_first: "관광콘텐츠 선행",
      investment_caution: "공급 확대 신중 검토",
    }[node.dataset.recommendation] || "수요·노후 근거 보완";
    const fundingTracks = fundingTrackLabel(node);
    const access = accessibilityContext(node);
    document.getElementById("region-summary-text").textContent = `선택한 500m 격자 내 숙박시설은 ${formatNumber(facilityCount)}개이며, ${node.dataset.dong} 전체는 ${formatNumber(dongFacilityCount)}개입니다. ${accessibilitySummary(access)}. 격자 시장 신호는 ${action}이며, 지원방식은 ${fundingTracks}입니다. 사업주체·인수 가능성 자료가 없어 확정 배정이 아닌 1차 중복 검토 결과입니다.`;
  }

  function fundingTrackLabel(node) {
    const tracks = new Set((node.dataset.fundingTracks || "").split(",").filter(Boolean));
    if (tracks.has("track1") && tracks.has("track2")) {
      return "Track 1 민간투자 촉진형 · Track 2 기존시설 개선형 중복 검토";
    }
    if (tracks.has("track1")) return "Track 1 민간투자 촉진형";
    if (tracks.has("track2")) return "Track 2 기존시설 개선형";
    return "지원트랙 판정 보류";
  }

  function renderFacilitySummary(node) {
    const rooms = numeric(node, "roomCount");
    const age = numeric(node, "buildingAge");
    const siteArea = numeric(node, "siteArea");
    const totalArea = numeric(node, "totalArea");
    const coverageRatio = numeric(node, "buildingCoverageRatio");
    const floorAreaRatio = numeric(node, "floorAreaRatio");
    const parking = numeric(node, "parkingTotal");
    const profileParts = [
      node.dataset.landUseZone ? `용도지역 ${node.dataset.landUseZone}` : null,
      siteArea === null ? null : `대지 ${formatNumber(siteArea, 1)}㎡`,
      totalArea === null ? null : `연면적 ${formatNumber(totalArea, 1)}㎡`,
      coverageRatio === null ? null : `건폐율 ${formatNumber(coverageRatio, 1)}%`,
      floorAreaRatio === null ? null : `용적률 ${formatNumber(floorAreaRatio, 1)}%`,
      node.dataset.mainUse ? `주용도 ${node.dataset.mainUse}` : null,
      parking === null ? null : `주차 ${formatNumber(parking)}대`,
    ].filter(Boolean);
    setRegionMetricLabels({
      first: ["선택 시설", "지도에 표시된 개별 숙박시설"],
      second: ["건축연령", "사용승인일 기반 확인값"],
      third: ["확인 객실", "인허가 원자료 확인값"],
      fourth: ["행정구역", "주소 기준 자치구·읍면동"],
    });
    document.getElementById("region-summary-title").textContent = `${node.dataset.publicName || "숙박시설"} · 개별 시설 상세`;
    document.getElementById("region-facility-count").textContent = "1개 시설";
    document.getElementById("region-aged-count").textContent = age === null ? "자료 없음" : `${formatNumber(age, 1)}년`;
    document.getElementById("region-room-count").textContent = rooms === null ? "자료 없음" : `${formatNumber(rooms)}실`;
    document.getElementById("region-gap-score").textContent = `${node.dataset.district || "-"} ${node.dataset.dong || ""}`.trim();
    const profileText = profileParts.length
      ? ` · 건축물대장 투자검토 정보: ${profileParts.join(" · ")}`
      : " · 건축물대장 투자검토 정보는 확인되지 않았습니다.";
    document.getElementById("region-summary-text").textContent = `${node.dataset.publicAddress || "주소 자료 없음"}${profileText} · 법적 적합성·권리관계·사업성은 별도 확인이 필요합니다.`;
    document.getElementById("region-ai-result").hidden = true;
  }

  function setRegionMetricLabels(labels) {
    const entries = [labels.first, labels.second, labels.third, labels.fourth];
    entries.forEach(([label, note], index) => {
      document.getElementById(`region-metric-${index + 1}-label`).textContent = label;
      document.getElementById(`region-metric-${index + 1}-note`).textContent = note;
    });
  }

  function buildCandidateRanking() {
    const rankings = bundle.candidate_rankings?.[activeLayer];
    let activeRanks = rankings?.default || {};
    if (filters.dong.value) activeRanks = rankings?.dong?.[`${filters.district.value}|${filters.dong.value}`] || {};
    else if (filters.district.value) activeRanks = rankings?.district?.[filters.district.value] || {};
    const ranked = grids.filter((node) => westDistricts.has(node.dataset.district) && visible(node) && activeRanks[node.dataset.key])
      .map((node) => {
        const kind = node.dataset.recommendation;
        const gap = numeric(node, "tourismSupplyGap") || 0;
        const aged = numeric(node, "agedCount") || 0;
        const facilityCount = numeric(node, "mappedFacilityCount") || 0;
        const action = {
          new_supply: "신규 관광숙박 공급 검토",
          remodel: "노후시설 개선·전환 검토",
          quality_upgrade: "소규모 숙박 품질개선 검토",
          content_first: "관광콘텐츠 선행 검토",
          investment_caution: "공급 확대 신중 검토",
        }[kind] || "수요·노후 근거 보완 검토";
        const accessScore = rankings?.details?.[node.dataset.key] || null;
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
          fundingTrack: fundingTrackLabel(node),
          rank: Number(activeRanks[node.dataset.key]),
          accessScore,
        };
      }).sort((a, b) => a.rank - b.rank || a.gridKey.localeCompare(b.gridKey)).slice(0, 5);
    const panelTitle = document.getElementById("candidate-panel-title");
    const panelHelp = document.getElementById("candidate-panel-help");
    if (filters.dong.value) {
      panelTitle.textContent = `${filters.district.value} ${filters.dong.value} 500m 세부 후보`;
      panelHelp.textContent = "선택한 동 안에서 서로 다른 500m 분석지역을 비교합니다.";
    } else if (filters.district.value) {
      panelTitle.textContent = `${filters.district.value} 생활권 우선 후보`;
      panelHelp.textContent = "같은 동의 인접 격자 중복을 제거하고 서로 다른 생활권을 비교합니다.";
    } else {
      const titles = {
        policy_priority: "서부산 숙박시설 정책 우선 사업지",
        tourism_supply_gap: "관광수요 대비 공급부족 최상위 지역",
        facility_density: "숙박시설 밀집 최상위 지역",
        aged_facilities: "노후 숙박시설 밀집 최상위 지역",
      };
      panelTitle.textContent = titles[activeLayer] || "서부산 숙박시설 분석지역";
      panelHelp.textContent = "강서·사하·북·사상구별 최우선 1곳과 서부산 전체 차순위 1곳을 표시합니다.";
    }
    const list = document.getElementById("candidate-rank-list");
    list.replaceChildren();
    candidateLayer.replaceChildren();
    candidateMarkers = [];
    ranked.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      const rank = document.createElement("b");
      rank.textContent = String(item.rank || index + 1);
      const label = document.createElement("span");
      label.textContent = `${item.district} ${item.dong}`;
      const detail = document.createElement("small");
      const details = {
        policy_priority: `${item.fundingTrack} · 시장 신호 ${item.action} · 시설 ${formatNumber(item.facilities)}개 · 20년+ ${formatNumber(item.aged)}개`,
        tourism_supply_gap: `공급부족도 ${formatNumber(item.gap, 1)}점 · 수요 대비 객실공급 취약`,
        facility_density: `500m 내 숙박시설 ${formatNumber(item.facilities)}개 · 밀집지역`,
        aged_facilities: `20년 이상 ${formatNumber(item.aged)}개 · 개선·전환 검토`,
      };
      detail.textContent = details[activeLayer] || item.action;
      if (item.accessScore) {
        const score = item.accessScore;
        const weighted = score.weighted_score === null
          ? "접근자료 미결합"
          : `종합 ${Number(score.weighted_score).toFixed(1)}점`;
        detail.textContent += (
          ` · ${weighted} (정책신호 70%·교통 15%·관광 15%)`
          + `${score.transport_period ? ` · 교통 ${score.transport_period} 기준` : ""}`
        );
      }
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
      text.textContent = String(item.rank || index + 1);
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

  function updateVisibleCounts() {
    document.getElementById("visible-grid-count").textContent = grids.filter(visible).length;
    document.getElementById("visible-facility-count").textContent = facilities.filter(visible).length;
  }

  function apply() {
    filterable.forEach((node) => node.classList.toggle("is-filtered", !visible(node)));
    updateVisibleCounts();
    updatePoiFilterStatus();
    setLayerEncoding();
    buildCandidateRanking();
    if (selectedGridNode && visible(selectedGridNode)) renderGridSummary(selectedGridNode);
    else renderRegionSummary();
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
    const access = accessibilityContext(node);
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
      transport_inbound: access.transportInbound,
      transport_period: access.transportPeriod,
      nearest_tourism_poi_name: access.nearestPoiName,
      nearest_tourism_poi_distance_m: access.nearestPoiDistance,
      tourism_poi_count_1000m: access.poiCount1000m,
    };
  }

  async function requestRegionInsight(node = selectedGridNode, target = document.getElementById("region-ai-result"), lotBased = false) {
    const result = target;
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
      title.textContent = lotBased ? `입력 지번 주변 500m · ${insight.headline}` : insight.headline;
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
    updateVisibleCounts();
    grids.forEach((node) => node.classList.toggle("is-selected", node === item.node));
    setLayerEncoding();
    renderGridSummary(item.node);
    fitGeographicBounds((item.node.dataset.geoBounds || "").split(",").map(Number), 17);
    requestRegionInsight(item.node);
  }

  function pointInRing(point, ring) {
    let inside = false;
    for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index, index += 1) {
      const [x, y] = ring[index];
      const [previousX, previousY] = ring[previous];
      if (((y > point[1]) !== (previousY > point[1]))
        && point[0] < ((previousX - x) * (point[1] - y)) / (previousY - y) + x) inside = !inside;
    }
    return inside;
  }

  function pointInGeometry(point, geometry) {
    const polygons = geometry?.type === "Polygon" ? [geometry.coordinates]
      : geometry?.type === "MultiPolygon" ? geometry.coordinates : [];
    return polygons.some((polygon) => polygon.length && pointInRing(point, polygon[0])
      && !polygon.slice(1).some((hole) => pointInRing(point, hole)));
  }

  function findGridForPoint(longitude, latitude, district) {
    const point = [Number(longitude), Number(latitude)];
    const feature = (bundle.grids?.features || []).find((item) => {
      const properties = item.properties || {};
      return (!district || properties.district_name === district)
        && pointInGeometry(point, item.geometry);
    });
    if (!feature) return null;
    return grids.find((node) => node.dataset.key === feature.properties.grid_id) || null;
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
  tourismPois.forEach((node) => node.addEventListener("click", (event) => {
    event.stopPropagation();
    showTourismPoiPopup(node, event);
    document.getElementById("region-summary-title").textContent = `${node.dataset.title || "관광지"} · 관광지 상세`;
    document.getElementById("region-summary-text").textContent = `${node.dataset.tourismType || "관광정보"} · ${node.dataset.district || "-"} ${node.dataset.dong || ""} · 숙박 투자 검토 시 인근 관광수요 유발시설로 참고하며 실제 방문량은 별도 확인이 필요합니다.`;
  }));
  tourismPois.forEach((node) => node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      showTourismPoiPopup(node, event);
    }
  }));
  document.getElementById("tourism-poi-popup-close").addEventListener("click", hideTourismPoiPopup);
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
  tourismPoiOverlay.addEventListener("change", apply);
  poiFilterButtons.forEach((button) => button.addEventListener("click", () => setPoiFilter(button.dataset.poiFilter)));
  document.getElementById("region-ai-button").addEventListener("click", () => requestRegionInsight());
  document.getElementById("lot-investment-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const address = document.getElementById("lot-address").value.trim();
    const result = document.getElementById("lot-investment-result");
    result.hidden = false;
    result.textContent = "지번을 확인하고 주변 500m 발행지표를 찾고 있습니다.";
    try {
      const response = await fetch("/tourism/api/vworld/geocode", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ address }),
      });
      if (!response.ok) throw new Error("geocode unavailable");
      const geocode = await response.json();
      if (geocode.status !== "matched") {
        result.textContent = "입력한 부산 지번을 확인하지 못했습니다. 시·구·동과 번지를 포함해 다시 입력해 주세요.";
        return;
      }
      const node = findGridForPoint(geocode.longitude, geocode.latitude, geocode.district);
      if (!node) {
        result.textContent = "해당 지번과 연결되는 현재 발행 500m 분석지역이 없습니다.";
        return;
      }
      selectedGridNode = node;
      filters.district.value = node.dataset.district;
      refreshDongOptions();
      filters.dong.value = node.dataset.dong;
      apply();
      grids.forEach((item) => item.classList.toggle("is-selected", item === node));
      renderGridSummary(node);
      fitGeographicBounds((node.dataset.geoBounds || "").split(",").map(Number), 17);
      await requestRegionInsight(node, result, true);
    } catch (_) {
      result.textContent = "지번 기반 투자검토를 불러오지 못했습니다. 주소를 확인한 뒤 다시 시도해 주세요.";
    }
  });

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
