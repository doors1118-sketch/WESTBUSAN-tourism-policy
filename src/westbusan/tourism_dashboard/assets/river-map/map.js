(() => {
  "use strict";

  const TILE_SIZE = 256;
  const MIN_ZOOM = 10;
  const MAX_ZOOM = 18;
  const INITIAL = { lon: 128.977, lat: 35.178, zoom: 12 };
  const map = document.getElementById("river-map");
  const tileLayer = document.getElementById("tile-layer");
  const parkBoundaryOverlay = document.getElementById("park-boundary-overlay");
  const overlay = document.getElementById("river-overlay");
  const externalOverlay = document.getElementById("external-regulation-overlay");
  const labelsLayer = document.getElementById("park-labels");
  const clickMarker = document.getElementById("click-marker");
  const activitySelect = document.getElementById("activity-select");
  const structureHeight = document.getElementById("structure-height");
  const roofType = document.getElementById("roof-type");
  const parcelAddress = document.getElementById("parcel-address");
  const parcelSearch = document.getElementById("parcel-search");
  const parcelSearchState = document.getElementById("parcel-search-state");
  const policyInsightButton = document.getElementById("policy-insight-button");
  const policyInsightPanel = document.getElementById("policy-insight-panel");
  const layerFocusStatus = document.getElementById("layer-focus-status");
  const clearAllLayersButton = document.getElementById("clear-all-layers");
  const tileNodes = new Map();
  const parkBoundaryPathNodes = [];
  const pathNodes = [];
  const externalPathNodes = [];
  let features = [];
  let parkBoundaryFeatures = [];
  let selectedPoint = null;
  let selectedPnu = null;
  let dragOrigin = null;
  let regulationController = null;
  let regulationSequence = 0;
  let policyInsightController = null;
  let latestRegulationReview = null;
  let focusedLayer = null;
  let focusMessageOverride = "";

  const parks = {
    hwamyeong: { id: "hwamyeong", name: "화명생태공원", color: "#2563EB", lon: 129.00547, lat: 35.23847, zoom: 14 },
    daejeo: { id: "daejeo", name: "대저생태공원", color: "#D97706", lon: 128.98894, lat: 35.21074, zoom: 14 },
    samrak: { id: "samrak", name: "삼락생태공원", color: "#059669", lon: 128.9763, lat: 35.1711, zoom: 14 },
    maekdo: { id: "maekdo", name: "맥도생태공원", color: "#7C3AED", lon: 128.95709, lat: 35.15138, zoom: 14 },
    eulsukdo: { id: "eulsukdo", name: "을숙도생태공원", color: "#DB2777", lon: 128.9523, lat: 35.1172, zoom: 14 },
  };
  const activityLabels = {
    walking: "산책·탐방", ecology: "생태관찰·복원", festival: "축제·행사",
    sports: "체육·레저", camping: "야영·캠핑", food: "판매·음식시설",
    culture: "공연·문화시설", lodging: "숙박시설", parking: "주차장·진입도로",
  };
  const zoneLabels = {
    waterfront: "하천공간관리 근린친수지구", general_conservation: "하천공간관리 일반보전지구",
    restoration: "하천공간관리 복원지구", river_area_unclassified: "하천구역·세부지구 미확인",
    outside_river_area: "조회 기준 하천구역 외",
  };
  const gradeLabels = {
    conditional: "관리청 협의 전제 검토",
    principally_restricted: "원칙적 제한 우세·예외 확인 필요",
    outside_scope: "하천구역 외·별도 법령 검토",
  };
  const regulationLabels = {
    wetland: "습지보호구역",
    heritage: "국가유산",
    urban_park: "도시공원",
    land_use: "용도지역",
  };
  const layerDisplayLabels = {
    river_area: "하천구역",
    general_conservation: "하천공간관리 일반보전지구",
    waterfront: "하천공간관리 근린친수지구",
    restoration: "하천공간관리 복원지구",
    ...regulationLabels,
  };
  const regulationStatusLabels = {
    no_overlap: "선택 지점 중첩 없음",
    provider_error: "공간서비스 조회 실패·미판정",
    invalid_response: "공간서비스 응답 오류·미판정",
  };
  const planningCategoryLabels = {
    development_restriction: "개발행위허가 제한",
    district_unit_plan: "지구단위계획",
    urban_planning_facility: "도시계획시설",
    land_use_zone: "용도지역",
    land_use_district: "용도지구",
    land_use_area: "용도구역",
    other_law_restriction: "개별법 규제",
    unclassified: "기타 지정",
  };

  function worldPixel(lon, lat, zoom) {
    const size = TILE_SIZE * (2 ** zoom);
    const clipped = Math.max(-85.05112878, Math.min(85.05112878, lat));
    const sin = Math.sin(clipped * Math.PI / 180);
    return [(lon + 180) / 360 * size, (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size];
  }

  function lonLatFromWorld(x, y, zoom) {
    const size = TILE_SIZE * (2 ** zoom);
    const lon = x / size * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y / size;
    return [lon, 180 / Math.PI * Math.atan(Math.sinh(n))];
  }

  const initialWorld = worldPixel(INITIAL.lon, INITIAL.lat, INITIAL.zoom);
  const state = { zoom: INITIAL.zoom, centerX: initialWorld[0], centerY: initialWorld[1] };

  function screenPoint(lon, lat) {
    const [x, y] = worldPixel(lon, lat, state.zoom);
    return [x - state.centerX + map.clientWidth / 2, y - state.centerY + map.clientHeight / 2];
  }

  function pathForRing(ring) {
    return ring.map((point, index) => {
      const [x, y] = screenPoint(point[0], point[1]);
      return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ") + " Z";
  }

  function pathForGeometry(geometry) {
    if (geometry.type === "Polygon") return geometry.coordinates.map(pathForRing).join(" ");
    if (geometry.type === "MultiPolygon") return geometry.coordinates.flatMap((polygon) => polygon.map(pathForRing)).join(" ");
    if (geometry.type === "GeometryCollection") return (geometry.geometries || []).map(pathForGeometry).filter(Boolean).join(" ");
    return "";
  }

  function visitGeometryPoints(geometry, visitor) {
    if (!geometry) return;
    if (geometry.type === "GeometryCollection") {
      (geometry.geometries || []).forEach((member) => visitGeometryPoints(member, visitor));
      return;
    }
    const walk = (value) => {
      if (!Array.isArray(value)) return;
      if (value.length >= 2 && Number.isFinite(value[0]) && Number.isFinite(value[1])) {
        visitor(value[0], value[1]);
        return;
      }
      value.forEach(walk);
    };
    walk(geometry.coordinates);
  }

  function featureBounds(items) {
    const bounds = { minLon: Infinity, minLat: Infinity, maxLon: -Infinity, maxLat: -Infinity };
    items.forEach(({ feature }) => visitGeometryPoints(feature.geometry, (lon, lat) => {
      bounds.minLon = Math.min(bounds.minLon, lon); bounds.minLat = Math.min(bounds.minLat, lat);
      bounds.maxLon = Math.max(bounds.maxLon, lon); bounds.maxLat = Math.max(bounds.maxLat, lat);
    }));
    return Number.isFinite(bounds.minLon) ? bounds : null;
  }

  function featureCenter(feature) {
    const bounds = featureBounds([{ feature }]);
    return bounds ? [(bounds.minLon + bounds.maxLon) / 2, (bounds.minLat + bounds.maxLat) / 2] : null;
  }

  function focusItemsForLayer(layer) {
    return pathNodes.filter((item) => item.feature.properties.zone_type === layer)
      .concat(externalPathNodes.filter((item) => item.feature.properties.category === layer));
  }

  function fitFocusedFeatures(layer) {
    const items = focusItemsForLayer(layer);
    const bounds = featureBounds(items);
    if (!bounds || map.clientWidth <= 0 || map.clientHeight <= 0) return false;
    const usableWidth = Math.max(120, map.clientWidth - 100);
    const usableHeight = Math.max(120, map.clientHeight - 100);
    let targetZoom = MIN_ZOOM;
    for (let zoom = MAX_ZOOM; zoom >= MIN_ZOOM; zoom -= 1) {
      const topLeft = worldPixel(bounds.minLon, bounds.maxLat, zoom);
      const bottomRight = worldPixel(bounds.maxLon, bounds.minLat, zoom);
      if (Math.abs(bottomRight[0] - topLeft[0]) <= usableWidth && Math.abs(bottomRight[1] - topLeft[1]) <= usableHeight) {
        targetZoom = zoom;
        break;
      }
    }
    const northWest = worldPixel(bounds.minLon, bounds.maxLat, targetZoom);
    const southEast = worldPixel(bounds.maxLon, bounds.minLat, targetZoom);
    state.zoom = targetZoom;
    state.centerX = (northWest[0] + southEast[0]) / 2;
    state.centerY = (northWest[1] + southEast[1]) / 2;
    render();
    return true;
  }

  function renderTiles() {
    const width = map.clientWidth;
    const height = map.clientHeight;
    const left = state.centerX - width / 2;
    const top = state.centerY - height / 2;
    const limit = 2 ** state.zoom;
    const visible = new Set();
    for (let ty = Math.max(0, Math.floor(top / TILE_SIZE)); ty <= Math.min(limit - 1, Math.floor((top + height) / TILE_SIZE)); ty += 1) {
      for (let tx = Math.floor(left / TILE_SIZE); tx <= Math.floor((left + width) / TILE_SIZE); tx += 1) {
        const wx = ((tx % limit) + limit) % limit;
        const key = `${state.zoom}/${wx}/${ty}`;
        visible.add(key);
        let image = tileNodes.get(key);
        if (!image) {
          image = new Image(); image.alt = ""; image.decoding = "async"; image.draggable = false;
          image.addEventListener("error", () => { image.style.visibility = "hidden"; });
          image.addEventListener("load", () => { image.style.visibility = "visible"; });
          image.src = map.dataset.tileTemplate.replace("{z}", state.zoom).replace("{x}", wx).replace("{y}", ty);
          tileNodes.set(key, image); tileLayer.append(image);
        }
        image.style.left = `${tx * TILE_SIZE - left}px`; image.style.top = `${ty * TILE_SIZE - top}px`;
      }
    }
    tileNodes.forEach((image, key) => { if (!visible.has(key)) { image.remove(); tileNodes.delete(key); } });
  }

  function renderOverlay() {
    parkBoundaryOverlay.setAttribute("viewBox", `0 0 ${map.clientWidth} ${map.clientHeight}`);
    overlay.setAttribute("viewBox", `0 0 ${map.clientWidth} ${map.clientHeight}`);
    externalOverlay.setAttribute("viewBox", `0 0 ${map.clientWidth} ${map.clientHeight}`);
    parkBoundaryPathNodes.forEach(({ node, feature }) => node.setAttribute("d", pathForGeometry(feature.geometry)));
    pathNodes.forEach(({ node, feature }) => node.setAttribute("d", pathForGeometry(feature.geometry)));
    externalPathNodes.forEach(({ node, feature }) => node.setAttribute("d", pathForGeometry(feature.geometry)));
    labelsLayer.replaceChildren();
    if (parkBoundaryLayerEnabled()) {
      Object.values(parks).forEach((park) => {
        const [x, y] = screenPoint(park.lon, park.lat);
        if (x < -80 || x > map.clientWidth + 80 || y < -30 || y > map.clientHeight + 30) return;
        const label = document.createElement("span"); label.className = "park-label"; label.textContent = park.name;
        label.style.backgroundColor = park.color;
        label.style.left = `${x}px`; label.style.top = `${y}px`; labelsLayer.append(label);
      });
    }
    if (focusedLayer) {
      const focusedItems = focusItemsForLayer(focusedLayer);
      const labelTargets = focusedItems.length > 6
        ? [{ center: (() => {
          const bounds = featureBounds(focusedItems);
          return bounds ? [(bounds.minLon + bounds.maxLon) / 2, (bounds.minLat + bounds.maxLat) / 2] : null;
        })(), text: `${layerDisplayLabels[focusedLayer]} · ${focusedItems.length}개 도형` }]
        : focusedItems.map(({ feature }, index) => ({
          center: featureCenter(feature),
          text: `${layerDisplayLabels[focusedLayer]} · ${index + 1}/${focusedItems.length}`,
        }));
      labelTargets.forEach(({ center, text: labelText }) => {
        if (!center) return;
        const [x, y] = screenPoint(center[0], center[1]);
        if (x < -120 || x > map.clientWidth + 120 || y < -50 || y > map.clientHeight + 50) return;
        const label = document.createElement("span");
        label.className = "focus-feature-label";
        label.textContent = labelText;
        label.style.left = `${x}px`; label.style.top = `${y}px`; labelsLayer.append(label);
      });
    }
    if (selectedPoint) {
      const [x, y] = screenPoint(selectedPoint[0], selectedPoint[1]);
      clickMarker.hidden = false; clickMarker.style.left = `${x}px`; clickMarker.style.top = `${y}px`;
    }
  }

  function render() { renderTiles(); renderOverlay(); }

  function pointInRing(point, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
      const [xi, yi] = ring[i]; const [xj, yj] = ring[j];
      const crosses = ((yi > point[1]) !== (yj > point[1])) &&
        (point[0] < (xj - xi) * (point[1] - yi) / ((yj - yi) || Number.EPSILON) + xi);
      if (crosses) inside = !inside;
    }
    return inside;
  }

  function pointInPolygon(point, polygon) {
    if (!pointInRing(point, polygon[0])) return false;
    return !polygon.slice(1).some((hole) => pointInRing(point, hole));
  }

  function pointInFeature(point, feature) {
    const geometry = feature.geometry;
    if (geometry.type === "Polygon") return pointInPolygon(point, geometry.coordinates);
    if (geometry.type === "MultiPolygon") return geometry.coordinates.some((polygon) => pointInPolygon(point, polygon));
    if (geometry.type === "GeometryCollection") return (geometry.geometries || []).some((member) => pointInFeature(point, { geometry: member }));
    return false;
  }

  function parkAt(point) {
    return parkBoundaryFeatures.find((feature) => pointInFeature(point, feature)) || null;
  }

  function setActiveParkBoundary(parkId) {
    parkBoundaryPathNodes.forEach(({ node, feature }) => {
      const matches = feature.properties.park_id === parkId;
      node.classList.toggle("is-active", Boolean(parkId) && matches);
      node.classList.toggle("is-muted", Boolean(parkId) && !matches);
    });
  }

  function parkBoundaryLayerEnabled() {
    const input = document.querySelector("[data-park-boundary-layer]");
    return !input || input.checked;
  }

  function setParkBoundaryLayerVisible(visible) {
    focusMessageOverride = "";
    document.querySelectorAll("[data-park-boundary-layer]").forEach((input) => {
      input.checked = visible;
    });
    parkBoundaryPathNodes.forEach(({ node }) => node.classList.toggle("is-hidden", !visible));
    if (!visible) {
      setActiveParkBoundary(null);
      document.querySelectorAll("[data-park]").forEach((button) => {
        button.classList.remove("is-active");
        button.setAttribute("aria-pressed", "false");
      });
    }
    applyLayerReadability();
    renderOverlay();
  }

  function inputForLayer(layer) {
    return document.querySelector(`[data-layer="${layer}"], [data-regulation-layer="${layer}"]`);
  }

  function allLayersCleared() {
    const inputs = document.querySelectorAll("[data-park-boundary-layer], [data-layer], [data-regulation-layer]");
    return inputs.length > 0 && Array.from(inputs).every((input) => !input.checked);
  }

  function updateFocusButtons() {
    document.querySelectorAll("[data-focus-layer]").forEach((button) => {
      const layer = button.dataset.focusLayer;
      const count = focusItemsForLayer(layer).length;
      button.disabled = count === 0;
      button.title = count
        ? `${layerDisplayLabels[layer]} ${count}개 도형 단독 표시`
        : button.dataset.pointLayer === "true"
          ? "지도 지점을 선택한 뒤 중첩 도형이 반환되면 사용할 수 있습니다."
          : "발행된 도형이 없습니다.";
      if (!button.classList.contains("is-active")) {
        button.textContent = count ? "강조" : button.dataset.pointLayer === "true" ? "지점조회 후" : "도형 없음";
      }
    });
  }

  function applyLayerReadability() {
    const hasFocus = Boolean(focusedLayer);
    map.classList.toggle("has-focused-layer", hasFocus);
    pathNodes.forEach(({ node, feature }) => {
      const layer = feature.properties.zone_type;
      node.classList.toggle("is-focus-layer", hasFocus && layer === focusedLayer);
      node.classList.toggle("is-context-layer", hasFocus && layer !== focusedLayer);
    });
    externalPathNodes.forEach(({ node, feature }) => {
      const layer = feature.properties.category;
      node.classList.toggle("is-focus-layer", hasFocus && layer === focusedLayer);
      node.classList.toggle("is-context-layer", hasFocus && layer !== focusedLayer);
    });
    parkBoundaryPathNodes.forEach(({ node }) => node.classList.toggle("is-regulation-context", hasFocus));
    document.querySelectorAll("[data-focus-layer]").forEach((button) => {
      const active = hasFocus && button.dataset.focusLayer === focusedLayer;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      if (active) button.textContent = "강조 중";
    });
    clearAllLayersButton.disabled = false;
    clearAllLayersButton.textContent = allLayersCleared()
      ? "전체 레이어 켜기"
      : "전체 레이어 해제";
    layerFocusStatus.classList.toggle("is-active", hasFocus);
    const count = hasFocus ? focusItemsForLayer(focusedLayer).length : 0;
    layerFocusStatus.textContent = focusMessageOverride || (hasFocus
      ? `${layerDisplayLabels[focusedLayer]} ${count}개 도형으로 자동 이동했습니다. 다른 규제 레이어는 숨기고 공원 참고경계는 유지합니다.`
      : "전체 레이어를 함께 표시합니다.");
    updateFocusButtons();
  }

  function setFocusedLayer(layer) {
    focusMessageOverride = "";
    if (!layer || focusedLayer === layer) {
      focusedLayer = null;
    } else if (!focusItemsForLayer(layer).length) {
      focusedLayer = null;
      focusMessageOverride = `${layerDisplayLabels[layer]} 도형이 현재 지도에 없습니다. 지점조회 결과 없음과 규제 없음은 같은 뜻이 아닙니다.`;
    } else {
      focusedLayer = layer;
    }
    if (focusedLayer) {
      const input = inputForLayer(focusedLayer);
      if (input) input.checked = true;
      pathNodes.filter((item) => item.feature.properties.zone_type === focusedLayer)
        .forEach((item) => item.node.classList.remove("is-hidden"));
      externalPathNodes.filter((item) => item.feature.properties.category === focusedLayer)
        .forEach((item) => item.node.classList.remove("is-hidden"));
    }
    applyLayerReadability();
    if (focusedLayer) fitFocusedFeatures(focusedLayer);
  }

  function clearAllLayers() {
    const layerInputs = [...document.querySelectorAll("[data-park-boundary-layer], [data-layer], [data-regulation-layer]")];
    const showAll = layerInputs.every((input) => !input.checked);
    focusedLayer = null;
    focusMessageOverride = showAll ? "전체 레이어를 다시 표시합니다."
      : "전체 레이어를 해제했습니다. 체크박스 또는 강조 버튼에서 필요한 레이어를 선택하십시오.";
    layerInputs.forEach((input) => {
      input.checked = showAll;
    });
    pathNodes.forEach(({ node }) => node.classList.toggle("is-hidden", !showAll));
    externalPathNodes.forEach(({ node }) => node.classList.toggle("is-hidden", !showAll));
    parkBoundaryPathNodes.forEach(({ node }) => node.classList.toggle("is-hidden", !showAll));
    setActiveParkBoundary(null);
    document.querySelectorAll("[data-park]").forEach((button) => {
      button.classList.remove("is-active");
      button.setAttribute("aria-pressed", "false");
    });
    applyLayerReadability();
    setView(INITIAL.lon, INITIAL.lat, INITIAL.zoom);
  }

  function riverOverlapItems(point) {
    if (!point) return [];
    const found = new Set();
    return features.filter((feature) => pointInFeature(point, feature)).reduce((items, feature) => {
      const layer = feature.properties.zone_type;
      if (!layerDisplayLabels[layer] || found.has(layer)) return items;
      found.add(layer);
      items.push({ layer, label: layerDisplayLabels[layer] });
      return items;
    }, []);
  }

  function renderOverlapSummary(review = null, pending = false) {
    const root = document.getElementById("overlap-badges");
    const count = document.getElementById("overlap-count");
    const note = document.getElementById("overlap-summary-note");
    root.replaceChildren();
    if (!selectedPoint) {
      const empty = document.createElement("span");
      empty.className = "overlap-empty";
      empty.textContent = "지도에서 지점을 선택하십시오.";
      root.append(empty);
      count.textContent = "위치 선택 전";
      note.textContent = "색상 합성 대신 실제 중첩 레이어를 명칭으로 확인합니다.";
      return;
    }
    const items = riverOverlapItems(selectedPoint);
    const matches = review && Array.isArray(review.matches) ? review.matches : [];
    const statuses = review && Array.isArray(review.layer_statuses) ? review.layer_statuses : [];
    Object.keys(regulationLabels).forEach((category) => {
      const categoryMatches = matches.filter((match) => match.category === category);
      const status = statuses.find((item) => item.category === category);
      if (!categoryMatches.length && (!status || status.status !== "matched")) return;
      const names = [...new Set(categoryMatches.map((match) => match.label).filter(Boolean))];
      items.push({
        layer: category,
        label: names.length ? `${regulationLabels[category]} · ${names.join(" · ")}` : regulationLabels[category],
      });
    });
    const byLayer = new Map();
    items.forEach((item) => { if (!byLayer.has(item.layer)) byLayer.set(item.layer, item); });
    byLayer.forEach((item) => {
      const badge = document.createElement("button");
      badge.type = "button";
      badge.className = `overlap-badge overlap-${item.layer}`;
      badge.dataset.overlapLayer = item.layer;
      badge.textContent = item.label;
      badge.title = `${item.label} 레이어 강조`;
      badge.addEventListener("click", () => setFocusedLayer(item.layer));
      root.append(badge);
    });
    count.textContent = `${byLayer.size}개 레이어 중첩`;
    const missing = statuses.filter((status) => ["provider_error", "invalid_response"].includes(status.status))
      .map((status) => regulationLabels[status.category]).filter(Boolean);
    if (pending) {
      note.textContent = "하천 관리구역을 먼저 표시했습니다. 외부 규제 레이어는 조회 중입니다.";
    } else if (missing.length) {
      note.textContent = `미판정: ${missing.join("·")} · 조회 실패는 중첩 없음으로 해석하지 않습니다.`;
    } else {
      note.textContent = "배지를 누르면 해당 레이어만 강조하고 나머지는 윤곽으로 전환합니다.";
    }
  }

  function uniqueText(values) {
    return [...new Set(values.filter((value) => typeof value === "string" && value.trim()).map((value) => value.trim()))];
  }

  function replaceSupportList(elementId, values) {
    const root = document.getElementById(elementId);
    root.replaceChildren();
    uniqueText(values).slice(0, 6).forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      root.append(item);
    });
  }

  function renderDecisionBasis(review = null, pending = false) {
    if (!selectedPoint) {
      replaceSupportList("assessment-basis-list", ["지도에서 검토 위치와 행위를 선택하십시오."]);
      replaceSupportList("assessment-check-list", ["주소·지번을 입력하면 PNU 기준 도시계획 정보를 추가 확인합니다."]);
      return;
    }
    const zone = zoneAt(selectedPoint);
    const activity = activityLabels[activitySelect.value];
    const basis = [`하천공간관리 중첩: ${zoneLabels[zone]} · 검토행위: ${activity}`];
    const matches = review && Array.isArray(review.matches) ? review.matches : [];
    const groupedMatches = new Map();
    matches.forEach((match) => {
      if (!match || !regulationLabels[match.category]) return;
      const labels = groupedMatches.get(match.category) || [];
      if (match.label) labels.push(match.label);
      groupedMatches.set(match.category, labels);
    });
    groupedMatches.forEach((labels, category) => {
      const names = uniqueText(labels);
      basis.push(`공간중첩: ${regulationLabels[category]}${names.length ? ` · ${names.join(" · ")}` : ""}`);
    });
    const planning = review && review.parcel_planning;
    if (planning && planning.status === "matched") {
      const designationNames = (planning.designations || []).map((item) => item && item.name).filter(Boolean);
      basis.push(`필지 도시계획: ${designationNames.length ? uniqueText(designationNames).join(" · ") : "공식 지정명 원문 확인 필요"}`);
    } else if (!selectedPnu) {
      basis.push("필지 도시계획: PNU 미확정으로 용도지구·용도구역 상세판정 전");
    }
    if (pending) basis.push("습지·국가유산·도시공원·용도지역 공간서비스 조회 중");
    replaceSupportList("assessment-basis-list", basis);

    const checks = [];
    if (review && review.next_check) checks.push(review.next_check);
    if (!selectedPnu) checks.push("주소·지번 검색 또는 지도 클릭 PNU 자동연결로 필지를 확정한 뒤 토지이용계획확인서를 대조");
    const statuses = review && Array.isArray(review.layer_statuses) ? review.layer_statuses : [];
    const unavailable = statuses
      .filter((status) => ["provider_error", "invalid_response"].includes(status.status))
      .map((status) => regulationLabels[status.category]).filter(Boolean);
    if (unavailable.length) checks.push(`${uniqueText(unavailable).join("·")} 조회 실패: 중첩 없음으로 해석하지 말고 공간서비스 재조회`);
    const heritage = review && review.heritage_criteria;
    if (heritage && ["individual_review_required", "exceeds_published_criteria", "project_input_required"].includes(heritage.code)) {
      checks.push("국가유산별 역사문화환경 보존지역 허용기준과 건축물 최고높이·지붕형태를 사업계획서로 재확인");
    }
    checks.push("최신 고시도면·공원조성계획과 소관 관리청의 사업계획 기반 사전협의 결과를 최종 판단자료로 사용");
    replaceSupportList("assessment-check-list", checks);
  }

  function zoneAt(point) {
    const hits = features.filter((feature) => pointInFeature(point, feature));
    const specific = hits.filter((feature) => feature.properties.zone_type !== "river_area")
      .sort((a, b) => b.properties.priority - a.properties.priority);
    if (specific[0]) return specific[0].properties.zone_type;
    return hits.some((feature) => feature.properties.zone_type === "river_area") ? "river_area_unclassified" : "outside_river_area";
  }

  function distanceKm(a, b) {
    const rad = Math.PI / 180; const dLat = (b.lat - a[1]) * rad; const dLon = (b.lon - a[0]) * rad;
    const q = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 6371 * 2 * Math.atan2(Math.sqrt(q), Math.sqrt(1 - q));
  }

  function nearestPark(point) {
    return Object.values(parks).map((park) => ({ ...park, distance: distanceKm(point, park) }))
      .sort((a, b) => a.distance - b.distance)[0];
  }

  function assessSelection(zone, activity) {
    if (zone === "outside_river_area") return { grade: "outside_scope", reason: "선택지가 조회 기준 하천구역 도형 밖입니다. 이 결과는 행위 가능 판정이 아니며 도시계획·공원·문화유산·습지 규제가 별도로 적용될 수 있습니다.", next: "토지이용계획확인서와 해당 공원·보호구역 관리청의 공식 도면을 확인하십시오." };
    if (zone === "waterfront") {
      if (activity === "lodging") return { grade: "principally_restricted", reason: "친수지구라도 숙박시설은 일상적 친수활동 범위를 넘는 영구적 건축·점용으로 판단될 가능성이 높습니다.", next: "하천정비기본계획, 하천점용허가, 홍수소통·치수안전, 공원조성계획 반영 여부를 사전협의하십시오." };
      return { grade: "conditional", reason: "친수지구는 이용 활동을 검토할 수 있지만 규모·영구성·홍수 영향에 따라 허가 가능성이 달라집니다.", next: "임시·가설 여부, 점용면적, 홍수기 철거계획, 차량진입, 하천관리청 사전협의를 확인하십시오." };
    }
    if (zone === "general_conservation") {
      if (["walking", "ecology"].includes(activity)) return { grade: "conditional", reason: "보전 기능을 훼손하지 않는 저강도 탐방·생태활동은 검토할 수 있지만 서식지·출입제한·점용 요건을 확인해야 합니다.", next: "습지·철새·문화유산 중첩, 탐방로 기조성 여부, 관리청 시기별 제한을 확인하십시오." };
      return { grade: "principally_restricted", reason: "일반보전지구에서 구조물·차량진입·영업·집객을 수반하는 행위는 보전목적과 충돌할 가능성이 높습니다.", next: "사업지를 친수지구로 조정하거나 비구조물·최소 점용 대안으로 변경한 후 관리청과 사전협의하십시오." };
    }
    if (zone === "restoration") {
      if (activity === "ecology") return { grade: "conditional", reason: "생태복원·관찰 목적이 관리방향과 일치하더라도 시설·동선은 복원계획을 훼손하지 않아야 합니다.", next: "복원 목표종·서식처, 출입 시기, 데크·안내시설 설치범위를 관리청과 협의하십시오." };
      return { grade: "principally_restricted", reason: "복원지구의 생태·지형 회복 목적과 충돌할 가능성이 높은 행위입니다.", next: "복원계획상 위치와 목표를 확인하고 사업지 변경 또는 비구조물·최소개입 대안을 검토하십시오." };
    }
    return { grade: "conditional", reason: "하천구역 내이지만 현재 스냅샷에서 세부 관리지구가 중첩되지 않은 위치입니다. 이를 행위 허용으로 해석하면 안 됩니다.", next: "최신 하천정비기본계획 원도면과 관리청 공식 의견으로 세부 지구를 확인하십시오." };
  }

  function setRegulationResultsLoading() {
    clearExternalRegulations();
    applyLayerReadability();
    document.querySelectorAll("[data-regulation-result]").forEach((card) => {
      card.className = "is-loading";
      card.querySelector("strong").textContent = "조회 중";
      card.querySelector("small").textContent = "VWorld 규제 레이어 교차조회";
    });
    const planningStatus = document.getElementById("parcel-planning-status");
    planningStatus.className = "";
    planningStatus.textContent = selectedPnu ? "필지 규제 조회 중" : "주소·지번 입력 필요";
  }

  function clearExternalRegulations() {
    const preserved = [];
    externalPathNodes.splice(0).forEach((item) => {
      if (item.feature && item.feature.properties && item.feature.properties.delivery === "full_extent_snapshot") {
        preserved.push(item);
      } else {
        item.node.remove();
      }
    });
    externalPathNodes.push(...preserved);
    if (focusedLayer && regulationLabels[focusedLayer] && !focusItemsForLayer(focusedLayer).length) focusedLayer = null;
    updateFocusButtons();
  }

  function regulationLayerEnabled(category) {
    const input = document.querySelector(`[data-regulation-layer="${category}"]`);
    return !input || input.checked;
  }

  function renderRegulationCards(review) {
    const matches = Array.isArray(review.matches) ? review.matches : [];
    const statuses = Array.isArray(review.layer_statuses) ? review.layer_statuses : [];
    Object.keys(regulationLabels).forEach((category) => {
      const card = document.querySelector(`[data-regulation-result="${category}"]`);
      if (!card) return;
      const categoryMatches = matches.filter((match) => match.category === category);
      const status = statuses.find((item) => item.category === category);
      const statusCode = status ? status.status : "";
      card.className = statusCode === "matched" ? "is-matched" : statusCode === "no_overlap" ? "is-clear" : "is-missing";
      card.querySelector("strong").textContent = categoryMatches.length
        ? categoryMatches.map((match) => match.label).join(" · ")
        : regulationStatusLabels[statusCode] || "응답 없음·미판정";
      card.querySelector("small").textContent = statusCode === "matched"
        ? `${categoryMatches.length}개 도형 중첩`
        : category === "wetland" && statusCode === "no_overlap"
          ? "선택 지점 비중첩 · 전수경계 지도 표시"
          : "조회 결과와 법정 고시도면은 다를 수 있음";
    });
    const heritage = review.heritage_criteria;
    const heritageCard = document.querySelector('[data-regulation-result="heritage"]');
    if (heritage && heritageCard) {
      const restricted = ["direct_designation_overlap", "individual_review_required", "exceeds_published_criteria"].includes(heritage.code);
      const clear = ["within_published_criteria", "no_snapshot_overlap"].includes(heritage.code);
      heritageCard.className = restricted ? "is-matched" : clear ? "is-clear" : "is-missing";
      heritageCard.querySelector("strong").textContent = heritage.label || "국가유산 기준 미판정";
      const checked = heritage.source_checked_at ? heritage.source_checked_at.slice(0, 10) : "기준일 미확인";
      const zone = heritage.zone_name ? ` · ${heritage.zone_name}` : "";
      heritageCard.querySelector("small").textContent = `${checked} 승인 스냅샷${zone}`;
    }
    renderOverlapSummary(review);
    renderDecisionBasis(review);
  }

  function appendExternalRegulations(collection, { staticSnapshot = false } = {}) {
    const externalFeatures = collection && Array.isArray(collection.features) ? collection.features : [];
    const staticCategories = new Set(
      externalPathNodes
        .filter((item) => item.feature && item.feature.properties && item.feature.properties.delivery === "full_extent_snapshot")
        .map((item) => item.feature.properties.category),
    );
    externalFeatures.forEach((feature) => {
      const category = feature && feature.properties ? feature.properties.category : "";
      if (!regulationLabels[category] || !feature.geometry) return;
      if (!staticSnapshot && staticCategories.has(category)) return;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", `external-feature external-${category}`);
      path.setAttribute("fill-rule", "evenodd");
      path.classList.toggle("is-hidden", !regulationLayerEnabled(category));
      externalOverlay.append(path);
      externalPathNodes.push({ node: path, feature });
    });
    updateFocusButtons();
    applyLayerReadability();
    renderOverlay();
  }

  function renderExternalRegulations(collection) {
    clearExternalRegulations();
    appendExternalRegulations(collection);
  }

  function renderParcelPlanning(review) {
    const status = document.getElementById("parcel-planning-status");
    const root = document.getElementById("parcel-planning-results");
    root.replaceChildren();
    status.className = review && review.grade === "principally_restricted"
      ? "is-restricted"
      : review && review.grade === "conditional" ? "is-conditional" : "";
    status.textContent = review && review.label ? review.label : "필지 규제 미판정";
    if (!review || review.status !== "matched") {
      const message = document.createElement("p");
      message.textContent = review && review.reason
        ? review.reason
        : "승인된 필지 규제 결과를 확인하지 못했습니다.";
      root.append(message);
      return;
    }
    const designations = document.createElement("div");
    designations.className = "planning-designations";
    (review.designations || []).forEach((item) => {
      const badge = document.createElement("span");
      badge.textContent = `${planningCategoryLabels[item.category] || "지정"} · ${item.name}`;
      designations.append(badge);
    });
    if (!designations.childElementCount) {
      const badge = document.createElement("span");
      badge.textContent = "공식 지정명 없음·원문 재확인";
      designations.append(badge);
    }
    root.append(designations);
    const characteristics = review.characteristics || {};
    const facts = [
      ["PNU", review.pnu || "-"],
      ["지목", characteristics.land_category || "미확인"],
      ["필지면적", Number.isFinite(characteristics.parcel_area) ? `${characteristics.parcel_area.toLocaleString("ko-KR")}㎡` : "미확인"],
      ["도로접면", characteristics.road_side || "미확인"],
      ["이용상황", characteristics.land_use_situation || "미확인"],
      ["자료기준", review.source_date || (review.checked_at || "").slice(0, 10) || "미확인"],
    ];
    const grid = document.createElement("div");
    grid.className = "planning-grid";
    facts.forEach(([label, value]) => {
      const card = document.createElement("div");
      const caption = document.createElement("span"); caption.textContent = label;
      const strong = document.createElement("strong"); strong.textContent = value;
      card.append(caption, strong); grid.append(card);
    });
    root.append(grid);
    const copy = document.createElement("div");
    copy.className = "planning-copy";
    const reason = document.createElement("p"); reason.textContent = `판정 근거: ${review.reason}`;
    const next = document.createElement("p"); next.textContent = `선행 확인: ${review.next_check}`;
    copy.append(reason, next); root.append(copy);
  }

  function markRegulationsUnavailable(message) {
    latestRegulationReview = null;
    clearExternalRegulations();
    applyLayerReadability();
    document.querySelectorAll("[data-regulation-result]").forEach((card) => {
      card.className = "is-missing";
      card.querySelector("strong").textContent = "조회 실패·미판정";
      card.querySelector("small").textContent = message;
    });
    renderParcelPlanning({
      status: "request_failed",
      grade: "unreviewed",
      label: "필지 규제 조회 실패·미판정",
      reason: message,
    });
    renderOverlapSummary({
      layer_statuses: Object.keys(regulationLabels).map((category) => ({ category, status: "provider_error" })),
    });
    renderDecisionBasis({
      next_check: message,
      layer_statuses: Object.keys(regulationLabels).map((category) => ({ category, status: "provider_error" })),
    });
  }

  async function queryRegulations(point, zone, activity, sequence) {
    if (regulationController) regulationController.abort();
    regulationController = new AbortController();
    const query = new URLSearchParams({
      longitude: String(point[0]),
      latitude: String(point[1]),
      activity,
      river_zone: zone,
    });
    const height = Number(structureHeight.value);
    if (structureHeight.value !== "" && Number.isFinite(height)) query.set("height_m", String(height));
    query.set("roof_type", roofType.value);
    if (selectedPnu) query.set("pnu", selectedPnu);
    try {
      const response = await fetch(`/tourism/api/regulations/point?${query}`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: regulationController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const review = await response.json();
      if (sequence !== regulationSequence) return;
      latestRegulationReview = review;
      const resolution = review.parcel_resolution || {};
      if (!selectedPnu && resolution.status === "matched" && resolution.pnu) {
        selectedPnu = resolution.pnu;
        parcelSearchState.textContent = `지도 클릭 좌표 · PNU 자동연결 완료 · 도형 ${resolution.matched_count}/${resolution.target_count}개 적재`;
      } else if (!selectedPnu && resolution.status === "boundary_ambiguous") {
        parcelSearchState.textContent = "필지 경계선 좌표입니다. 후보 PNU가 둘 이상이므로 주소·지번으로 확정하십시오.";
      } else if (!selectedPnu && resolution.status === "scope_not_published") {
        parcelSearchState.textContent = "현재 발행된 검토대상 필지도형 밖입니다. 주소·지번 검색으로 별도 확인하십시오.";
      }
      renderRegulationCards(review);
      renderParcelPlanning(review.parcel_planning);
      renderExternalRegulations(review.feature_collection);
      const grade = document.getElementById("assessment-grade");
      grade.className = `grade ${review.grade}`;
      grade.textContent = review.label || gradeLabels[review.grade] || "추가 확인 필요";
      document.getElementById("assessment-reason").textContent = review.reason;
      document.getElementById("assessment-next").textContent = review.next_check;
      policyInsightButton.disabled = false;
    } catch (error) {
      if (error.name === "AbortError" || sequence !== regulationSequence) return;
      markRegulationsUnavailable("하천구역 판정만 유지·외부 규제 재조회 필요");
      policyInsightButton.disabled = true;
    }
  }

  function fillList(element, values) {
    element.replaceChildren();
    (Array.isArray(values) ? values : []).forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      element.append(item);
    });
  }

  function appendPolicyFact(root, label, value, { wide = false } = {}) {
    const card = document.createElement("div");
    if (wide) card.className = "is-wide";
    const caption = document.createElement("span"); caption.textContent = label;
    const strong = document.createElement("strong"); strong.textContent = value;
    card.append(caption, strong); root.append(card);
  }

  function renderPolicyEvidenceCounts(review) {
    const root = document.getElementById("policy-evidence-counts");
    root.replaceChildren();
    const riverCount = selectedPoint ? riverOverlapItems(selectedPoint).length : 0;
    const matches = review && Array.isArray(review.matches) ? review.matches : [];
    const externalCount = new Set(matches.map((match) => match && match.category).filter(Boolean)).size;
    const planning = review && review.parcel_planning;
    const planningCount = planning && planning.status === "matched"
      ? uniqueText((planning.designations || []).map((item) => item && item.name)).length
      : 0;
    [
      ["하천·공간관리", riverCount, selectedPoint ? "지도 도형" : "미선택"],
      ["외부 공간규제", externalCount, review ? "지점 교차조회" : "조회 전"],
      ["필지 도시계획 지정", planningCount, planning && planning.status === "matched" ? "PNU 기준" : "필지 미확정"],
    ].forEach(([label, count, source]) => {
      const card = document.createElement("div");
      const caption = document.createElement("span"); caption.textContent = label;
      const strong = document.createElement("strong"); strong.textContent = `${count}개`;
      const small = document.createElement("small"); small.textContent = source;
      card.append(caption, strong, small); root.append(card);
    });
  }

  function renderPolicyParcelFacts(review) {
    const root = document.getElementById("policy-parcel-facts");
    const status = document.getElementById("policy-parcel-facts-status");
    root.replaceChildren();
    const planning = review && review.parcel_planning;
    if (!planning || planning.status !== "matched") {
      status.className = "is-unconfirmed";
      status.textContent = selectedPnu ? "필지 자료 미확인" : "PNU 미확정";
      const message = document.createElement("p");
      message.textContent = planning && planning.reason
        ? planning.reason
        : "주소·지번 검색 또는 지도 클릭 PNU 자동연결 후 필지 사실을 확인합니다.";
      root.append(message);
      return;
    }
    const sourceDate = planning.source_date || (planning.checked_at || "").slice(0, 10) || "기준일 미확인";
    status.className = "is-confirmed";
    status.textContent = `확인됨 · ${sourceDate}`;
    const characteristics = planning.characteristics || {};
    const coordinates = selectedPoint
      ? `${selectedPoint[1].toFixed(6)}, ${selectedPoint[0].toFixed(6)}`
      : "미확인";
    const area = Number.isFinite(characteristics.parcel_area)
      ? `${characteristics.parcel_area.toLocaleString("ko-KR")}㎡`
      : "미확인";
    const designations = uniqueText((planning.designations || []).map((item) => item && item.name));
    appendPolicyFact(root, "PNU", planning.pnu || selectedPnu || "미확인");
    appendPolicyFact(root, "좌표", coordinates);
    appendPolicyFact(root, "지목", characteristics.land_category || "미확인");
    appendPolicyFact(root, "필지면적", area);
    appendPolicyFact(root, "도로접면", characteristics.road_side || "미확인");
    appendPolicyFact(root, "이용상황", characteristics.land_use_situation || "미확인");
    appendPolicyFact(root, "도시계획 지정", designations.length ? designations.join(" · ") : "공식 지정명 미확인", { wide: true });
  }

  async function queryPolicyInsight() {
    if (!selectedPoint || policyInsightButton.disabled) return;
    if (policyInsightController) policyInsightController.abort();
    policyInsightController = new AbortController();
    policyInsightPanel.hidden = false;
    policyInsightButton.disabled = true;
    document.getElementById("policy-insight-status").textContent = "법령 근거·정책대안 생성 중";
    document.getElementById("policy-insight-title").textContent = "선택지 정책인사이트";
    document.getElementById("policy-insight-copy").textContent = "공간판정과 내부 법령근거 캐시를 확인하고 있습니다.";
    document.getElementById("policy-evidence-summary").textContent = "선택지 공간중첩, 검토 행위, PNU 도시계획 판정과 근거법령을 연결하고 있습니다.";
    renderPolicyEvidenceCounts(latestRegulationReview);
    renderPolicyParcelFacts(latestRegulationReview);
    fillList(document.getElementById("policy-options"), []);
    fillList(document.getElementById("required-consultations"), []);
    document.getElementById("legal-source-links").replaceChildren();
    document.getElementById("policy-insight-limit").textContent = "";
    document.getElementById("policy-insight-meta").textContent = "생성 상태와 근거자료 버전을 확인하고 있습니다.";
    const zone = zoneAt(selectedPoint);
    const body = {
      longitude: selectedPoint[0],
      latitude: selectedPoint[1],
      activity: activitySelect.value,
      river_zone: zone,
      roof_type: roofType.value,
    };
    const height = Number(structureHeight.value);
    if (structureHeight.value !== "" && Number.isFinite(height)) body.height_m = height;
    if (selectedPnu) body.pnu = selectedPnu;
    try {
      const response = await fetch("/tourism/api/regulations/insight", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
        signal: policyInsightController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const insight = await response.json();
      document.getElementById("policy-insight-title").textContent = insight.headline;
      document.getElementById("policy-insight-copy").textContent = insight.policy_insight;
      const legalCount = Array.isArray(insight.legal_bases) ? insight.legal_bases.length : 0;
      document.getElementById("policy-evidence-summary").textContent = `현재 단계: ${insight.deterministic_label} · 근거법령 ${legalCount}건 연결. AI는 공간판정 등급을 변경하지 않고 원안·조정안·대체입지의 검토경로만 설명합니다.`;
      const evidenceLabel = insight.legal_evidence_source === "curated_registry_and_mcp"
        ? "공식 근거법령 + MCP 보조검색"
        : insight.legal_evidence_source === "curated_registry"
          ? "공식 근거법령 연결"
          : "법령근거 미연결";
      const explanationLabel = insight.source === "openai"
        ? `${insight.cached ? "AI 해설 캐시" : "AI 정책해설"}`
        : "기본 정책해설";
      document.getElementById("policy-insight-status").textContent = `${explanationLabel} · ${evidenceLabel}`;
      renderPolicyEvidenceCounts(latestRegulationReview);
      renderPolicyParcelFacts(latestRegulationReview);
      fillList(document.getElementById("policy-options"), insight.policy_options);
      fillList(document.getElementById("required-consultations"), insight.required_consultations);
      const sourceRoot = document.getElementById("legal-source-links");
      sourceRoot.replaceChildren();
      (insight.legal_bases || []).forEach((basis) => {
        const link = document.createElement("a");
        link.href = basis.official_url; link.target = "_blank"; link.rel = "noopener";
        link.textContent = `${basis.law_name} ${basis.articles} ↗`;
        link.title = `${basis.rationale} ${basis.review_effect}`;
        sourceRoot.append(link);
      });
      if (!sourceRoot.children.length) {
        (insight.legal_source_urls || []).forEach((url, index) => {
          const link = document.createElement("a");
          link.href = url; link.target = "_blank"; link.rel = "noopener";
          link.textContent = `공식 법령근거 ${index + 1} ↗`;
          sourceRoot.append(link);
        });
      }
      document.getElementById("policy-insight-limit").textContent = insight.limitations;
      const generatedAt = insight.generated_at
        ? new Date(insight.generated_at).toLocaleString("ko-KR", { hour12: false })
        : "생성시각 미확인";
      document.getElementById("policy-insight-meta").textContent = `${insight.cached ? "캐시 재사용" : "신규 생성"} · ${generatedAt} · 법령 ${insight.legal_basis_version || "버전 미확인"} · 모델 ${insight.model || "미확인"}`;
    } catch (error) {
      if (error.name === "AbortError") return;
      document.getElementById("policy-insight-status").textContent = "생성 실패";
      document.getElementById("policy-insight-copy").textContent = "정책해설 서비스에 연결하지 못했습니다. 위 공간판정과 선행 확인사항만 사용하십시오.";
      document.getElementById("policy-evidence-summary").textContent = "AI 해설은 실패했지만 서버 결정규칙의 공간중첩·1차 판정근거는 그대로 유지됩니다.";
      document.getElementById("policy-insight-meta").textContent = "AI 생성 실패 · 위의 결정규칙 기반 판정과 필지 사실만 사용하십시오.";
    } finally {
      policyInsightButton.disabled = false;
    }
  }

  function updateAssessment(point) {
    const zone = zoneAt(point); const activity = activitySelect.value; const result = assessSelection(zone, activity); const boundary = parkAt(point); const park = boundary ? parks[boundary.properties.park_id] : nearestPark(point);
    document.getElementById("assessment-title").textContent = `${park.name} 일원·${activityLabels[activity]} 1차 검토`;
    const grade = document.getElementById("assessment-grade"); grade.className = `grade ${result.grade}`; grade.textContent = gradeLabels[result.grade];
    document.getElementById("selected-park").textContent = boundary ? `${park.name} 참고경계 내` : `${park.name} 인근 · 약 ${park.distance.toFixed(1)}km`;
    setActiveParkBoundary(boundary ? boundary.properties.park_id : null);
    document.getElementById("selected-zone").textContent = zoneLabels[zone];
    document.getElementById("selected-activity").textContent = activityLabels[activity];
    document.getElementById("selected-coordinate").textContent = `${point[1].toFixed(6)}, ${point[0].toFixed(6)}`;
    document.getElementById("assessment-reason").textContent = result.reason;
    document.getElementById("assessment-next").textContent = result.next;
    policyInsightButton.disabled = true;
    policyInsightPanel.hidden = true;
    latestRegulationReview = null;
    setRegulationResultsLoading();
    renderOverlapSummary(null, true);
    renderDecisionBasis(null, true);
    regulationSequence += 1;
    void queryRegulations(point, zone, activity, regulationSequence);
  }

  function setView(lon, lat, zoom) {
    state.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
    [state.centerX, state.centerY] = worldPixel(lon, lat, state.zoom); render();
  }

  map.addEventListener("pointerdown", (event) => { map.setPointerCapture(event.pointerId); dragOrigin = { x: event.clientX, y: event.clientY, cx: state.centerX, cy: state.centerY, moved: false }; });
  map.addEventListener("pointermove", (event) => { if (!dragOrigin) return; const dx = event.clientX - dragOrigin.x; const dy = event.clientY - dragOrigin.y; if (Math.abs(dx) + Math.abs(dy) > 4) dragOrigin.moved = true; state.centerX = dragOrigin.cx - dx; state.centerY = dragOrigin.cy - dy; render(); });
  map.addEventListener("pointerup", (event) => {
    if (!dragOrigin) return; const moved = dragOrigin.moved; dragOrigin = null;
    if (moved) return;
    const rect = map.getBoundingClientRect(); const worldX = state.centerX - map.clientWidth / 2 + event.clientX - rect.left; const worldY = state.centerY - map.clientHeight / 2 + event.clientY - rect.top;
    selectedPnu = null;
    parcelSearchState.textContent = "지도 클릭 위치입니다. 필지 도시계획 상세는 주소·지번 검색이 필요합니다.";
    selectedPoint = lonLatFromWorld(worldX, worldY, state.zoom); updateAssessment(selectedPoint); renderOverlay();
  });
  map.addEventListener("wheel", (event) => { event.preventDefault(); const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom + (event.deltaY < 0 ? 1 : -1)); }, { passive: false });
  document.getElementById("zoom-in").addEventListener("click", () => { const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom + 1); });
  document.getElementById("zoom-out").addEventListener("click", () => { const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom - 1); });
  document.getElementById("zoom-reset").addEventListener("click", () => setView(INITIAL.lon, INITIAL.lat, INITIAL.zoom));
  document.querySelectorAll("[data-park]").forEach((button) => button.addEventListener("click", () => {
    setParkBoundaryLayerVisible(true);
    document.querySelectorAll("[data-park]").forEach((item) => {
      const active = item === button;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-pressed", String(active));
    });
    const park = parks[button.dataset.park]; setActiveParkBoundary(park.id); setView(park.lon, park.lat, park.zoom);
  }));
  document.querySelectorAll("[data-park-boundary-layer]").forEach((input) => {
    input.addEventListener("change", () => setParkBoundaryLayerVisible(input.checked));
  });
  document.querySelectorAll("[data-layer]").forEach((input) => input.addEventListener("change", () => {
    focusMessageOverride = "";
    pathNodes.filter((item) => item.feature.properties.zone_type === input.dataset.layer).forEach((item) => item.node.classList.toggle("is-hidden", !input.checked));
    if (!input.checked && focusedLayer === input.dataset.layer) focusedLayer = null;
    applyLayerReadability();
  }));
  document.querySelectorAll("[data-regulation-layer]").forEach((input) => input.addEventListener("change", () => {
    focusMessageOverride = "";
    externalPathNodes.filter((item) => item.feature.properties.category === input.dataset.regulationLayer)
      .forEach((item) => item.node.classList.toggle("is-hidden", !input.checked));
    if (!input.checked && focusedLayer === input.dataset.regulationLayer) focusedLayer = null;
    applyLayerReadability();
  }));
  document.querySelectorAll("[data-focus-layer]").forEach((button) => button.addEventListener("click", () => {
    setFocusedLayer(button.dataset.focusLayer);
  }));
  clearAllLayersButton.addEventListener("click", clearAllLayers);
  updateFocusButtons();
  applyLayerReadability();
  activitySelect.addEventListener("change", () => { document.getElementById("selected-activity").textContent = activityLabels[activitySelect.value]; if (selectedPoint) updateAssessment(selectedPoint); });
  structureHeight.addEventListener("change", () => { if (selectedPoint) updateAssessment(selectedPoint); });
  roofType.addEventListener("change", () => { if (selectedPoint) updateAssessment(selectedPoint); });
  async function searchParcel() {
    const address = parcelAddress.value.trim();
    if (address.length < 8 || !address.includes("부산")) {
      parcelSearchState.textContent = "부산광역시와 자치구를 포함한 주소·지번을 입력하십시오.";
      return;
    }
    parcelSearch.disabled = true;
    parcelSearchState.textContent = "주소와 PNU를 확인하는 중입니다.";
    try {
      const response = await fetch("/tourism/api/vworld/geocode", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ address }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const geocode = await response.json();
      if (geocode.status !== "matched" || !geocode.pnu || !Number.isFinite(geocode.longitude) || !Number.isFinite(geocode.latitude)) {
        selectedPnu = null;
        parcelSearchState.textContent = "주소 좌표 또는 19자리 PNU를 확인하지 못했습니다.";
        return;
      }
      selectedPnu = geocode.pnu;
      selectedPoint = [geocode.longitude, geocode.latitude];
      parcelSearchState.textContent = `${geocode.district || "부산"} · PNU 확인 완료`;
      setView(selectedPoint[0], selectedPoint[1], Math.max(state.zoom, 16));
      updateAssessment(selectedPoint);
      renderOverlay();
    } catch (_error) {
      selectedPnu = null;
      parcelSearchState.textContent = "주소 검색 서비스에 연결하지 못했습니다. 잠시 후 다시 확인하십시오.";
    } finally {
      parcelSearch.disabled = false;
    }
  }
  parcelSearch.addEventListener("click", () => { void searchParcel(); });
  policyInsightButton.addEventListener("click", () => { void queryPolicyInsight(); });
  parcelAddress.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); void searchParcel(); }
  });
  window.addEventListener("resize", render);

  Promise.all([
    fetch("river_layers.geojson", { cache: "no-store" }).then((response) => response.json()),
    fetch("source_metadata.json", { cache: "no-store" }).then((response) => response.json()),
  ]).then(([collection, metadata]) => {
    features = collection.features;
    document.getElementById("source-date").textContent = `${metadata.retrieved_at.slice(0, 10)} 스냅샷·${metadata.feature_count}개 도형`;
    features.slice().sort((a, b) => a.properties.priority - b.properties.priority).forEach((feature) => {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", `zone-feature zone-${feature.properties.zone_type}`); path.setAttribute("fill-rule", "evenodd");
      const input = inputForLayer(feature.properties.zone_type);
      path.classList.toggle("is-hidden", input ? !input.checked : false);
      overlay.append(path); pathNodes.push({ node: path, feature });
    });
    updateFocusButtons();
    applyLayerReadability();
    render();
  }).catch(() => {
    document.querySelector(".map-instruction").textContent = "규제도형을 불러오지 못했습니다. 발행파일 경로를 확인하십시오.";
    renderTiles();
  });

  Promise.all([
    fetch("wetland_boundary.geojson", { cache: "no-store" }).then((response) => response.json()),
    fetch("wetland_boundary_source_metadata.json", { cache: "no-store" }).then((response) => response.json()),
  ]).then(([collection, metadata]) => {
    appendExternalRegulations(collection, { staticSnapshot: true });
    const button = document.querySelector('[data-focus-layer="wetland"]');
    if (button) button.title = `${metadata.feature_count}개 중복제거 도형 · ${metadata.notice.number}`;
  }).catch(() => {
    const button = document.querySelector('[data-focus-layer="wetland"]');
    if (button) button.title = "습지보호구역 전수경계를 불러오지 못했습니다. 지점조회 결과만 사용할 수 있습니다.";
  });

  Promise.all([
    fetch("park_boundaries.geojson", { cache: "no-store" }).then((response) => response.json()),
    fetch("park_boundary_source_metadata.json", { cache: "no-store" }).then((response) => response.json()),
  ]).then(([collection, metadata]) => {
    parkBoundaryFeatures = collection.features;
    parkBoundaryFeatures.forEach((feature) => {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", "park-boundary");
      path.setAttribute("fill", feature.properties.color);
      path.setAttribute("stroke", feature.properties.color);
      path.setAttribute("fill-rule", "evenodd");
      path.dataset.parkId = feature.properties.park_id;
      path.classList.toggle("is-hidden", !parkBoundaryLayerEnabled());
      parkBoundaryOverlay.append(path);
      parkBoundaryPathNodes.push({ node: path, feature });
    });
    document.getElementById("park-boundary-legend").title = `${metadata.display_label} · 법적 효력 없음`;
    renderOverlay();
  }).catch(() => {
    const help = document.querySelector(".park-boundary-help");
    help.textContent = "공원 참고경계를 불러오지 못했습니다. 공원 빠른 이동과 규제도형만 사용할 수 있습니다.";
  });
})();
