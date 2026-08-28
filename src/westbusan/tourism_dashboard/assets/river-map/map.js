(() => {
  "use strict";

  const TILE_SIZE = 256;
  const MIN_ZOOM = 10;
  const MAX_ZOOM = 18;
  const INITIAL = { lon: 128.977, lat: 35.178, zoom: 12 };
  const map = document.getElementById("river-map");
  const tileLayer = document.getElementById("tile-layer");
  const overlay = document.getElementById("river-overlay");
  const labelsLayer = document.getElementById("park-labels");
  const clickMarker = document.getElementById("click-marker");
  const activitySelect = document.getElementById("activity-select");
  const tileNodes = new Map();
  const pathNodes = [];
  let features = [];
  let selectedPoint = null;
  let dragOrigin = null;

  const parks = {
    hwamyeong: { name: "화명생태공원", lon: 129.00547, lat: 35.23847, zoom: 14 },
    daejeo: { name: "대저생태공원", lon: 128.98894, lat: 35.21074, zoom: 14 },
    samrak: { name: "삼락생태공원", lon: 128.9763, lat: 35.1711, zoom: 14 },
    maekdo: { name: "맥도생태공원", lon: 128.95709, lat: 35.15138, zoom: 14 },
    eulsukdo: { name: "을숙도생태공원", lon: 128.9523, lat: 35.1172, zoom: 14 },
  };
  const activityLabels = {
    walking: "산책·탐방", ecology: "생태관찰·복원", festival: "축제·행사",
    sports: "체육·레저", camping: "야영·캠핑", food: "판매·음식시설",
    culture: "공연·문화시설", lodging: "숙박시설", parking: "주차장·진입도로",
  };
  const zoneLabels = {
    waterfront: "근린친수지구", general_conservation: "일반보전지구",
    restoration: "복원지구", river_area_unclassified: "하천구역·세부지구 미확인",
    outside_river_area: "조회 기준 하천구역 외",
  };
  const gradeLabels = {
    conditional: "관리청 협의 전제 검토",
    principally_restricted: "원칙적 불가 가능성 높음",
    outside_scope: "하천구역 외·별도 법령 검토",
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
    return "";
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
    overlay.setAttribute("viewBox", `0 0 ${map.clientWidth} ${map.clientHeight}`);
    pathNodes.forEach(({ node, feature }) => node.setAttribute("d", pathForGeometry(feature.geometry)));
    labelsLayer.replaceChildren();
    Object.values(parks).forEach((park) => {
      const [x, y] = screenPoint(park.lon, park.lat);
      if (x < -80 || x > map.clientWidth + 80 || y < -30 || y > map.clientHeight + 30) return;
      const label = document.createElement("span"); label.className = "park-label"; label.textContent = park.name;
      label.style.left = `${x}px`; label.style.top = `${y}px`; labelsLayer.append(label);
    });
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
    return false;
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

  function updateAssessment(point) {
    const zone = zoneAt(point); const activity = activitySelect.value; const result = assessSelection(zone, activity); const park = nearestPark(point);
    document.getElementById("assessment-title").textContent = `${park.name} 일원·${activityLabels[activity]} 1차 검토`;
    const grade = document.getElementById("assessment-grade"); grade.className = `grade ${result.grade}`; grade.textContent = gradeLabels[result.grade];
    document.getElementById("selected-park").textContent = `${park.name} 약 ${park.distance.toFixed(1)}km`;
    document.getElementById("selected-zone").textContent = zoneLabels[zone];
    document.getElementById("selected-activity").textContent = activityLabels[activity];
    document.getElementById("selected-coordinate").textContent = `${point[1].toFixed(6)}, ${point[0].toFixed(6)}`;
    document.getElementById("assessment-reason").textContent = result.reason;
    document.getElementById("assessment-next").textContent = result.next;
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
    selectedPoint = lonLatFromWorld(worldX, worldY, state.zoom); updateAssessment(selectedPoint); renderOverlay();
  });
  map.addEventListener("wheel", (event) => { event.preventDefault(); const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom + (event.deltaY < 0 ? 1 : -1)); }, { passive: false });
  document.getElementById("zoom-in").addEventListener("click", () => { const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom + 1); });
  document.getElementById("zoom-out").addEventListener("click", () => { const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom - 1); });
  document.getElementById("zoom-reset").addEventListener("click", () => setView(INITIAL.lon, INITIAL.lat, INITIAL.zoom));
  document.querySelectorAll("[data-park]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-park]").forEach((item) => item.classList.toggle("is-active", item === button));
    const park = parks[button.dataset.park]; setView(park.lon, park.lat, park.zoom);
  }));
  document.querySelectorAll("[data-layer]").forEach((input) => input.addEventListener("change", () => {
    pathNodes.filter((item) => item.feature.properties.zone_type === input.dataset.layer).forEach((item) => item.node.classList.toggle("is-hidden", !input.checked));
  }));
  activitySelect.addEventListener("change", () => { document.getElementById("selected-activity").textContent = activityLabels[activitySelect.value]; if (selectedPoint) updateAssessment(selectedPoint); });
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
      overlay.append(path); pathNodes.push({ node: path, feature });
    });
    render();
  }).catch(() => {
    document.querySelector(".map-instruction").textContent = "규제도형을 불러오지 못했습니다. 발행파일 경로를 확인하십시오.";
    renderTiles();
  });
})();
