(() => {
  "use strict";
  const mapElement = document.getElementById("vacant-slippy-map");
  const candidateList = document.getElementById("candidate-list");
  const districtFilter = document.getElementById("district-filter");
  const guide = document.getElementById("zoom-guide");
  const map = L.map(mapElement, {
    minZoom: Number(mapElement.dataset.minZoom),
    maxZoom: Number(mapElement.dataset.maxZoom),
  }).setView(
    mapElement.dataset.mapCenter.split(",").reverse().map(Number),
    Number(mapElement.dataset.mapZoom),
  );
  L.tileLayer(mapElement.dataset.tileTemplate, {
    minZoom: Number(mapElement.dataset.minZoom),
    maxZoom: Number(mapElement.dataset.maxZoom),
    attribution: "국토교통부 VWorld",
  }).addTo(map);

  const data = { hubs: [], parcels: [], houses: [] };
  const layers = {
    hubs: L.layerGroup().addTo(map),
    parcels: L.layerGroup(),
    houses: L.layerGroup(),
  };
  const featureLayers = { hubs: new Map(), parcels: new Map(), houses: new Map() };
  let selectedHub = null;

  function textElement(tagName, text, className = "") {
    const element = document.createElement(tagName);
    element.className = className;
    element.textContent = text;
    return element;
  }
  function featureMatches(feature) {
    const districts = feature.properties.district_names
      || [feature.properties.district_name];
    return !districtFilter.value || districts.includes(districtFilter.value);
  }
  function hubHouses(hubId) {
    return data.houses.filter((feature) => feature.properties.hub_id === hubId);
  }
  function houseDetailNode(feature) {
    const properties = feature.properties;
    const wrapper = document.createElement("div");
    wrapper.append(
      textElement("strong", properties.exact_address || properties.road_address || "주소 미확인"),
      textElement("p", [
        properties.housing_type || "주택유형 미확인",
        properties.construction_year ? `${properties.construction_year}년` : "건축연도 미확인",
        properties.vacant_grade ? `${properties.vacant_grade}등급` : "등급 미확인",
      ].join(" · ")),
    );
    return wrapper;
  }
  function selectHub(feature) {
    const properties = feature.properties;
    selectedHub = properties.hub_id;
    candidateList.querySelectorAll("button").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.hubId === selectedHub);
    });
    featureLayers.hubs.forEach((entry, hubId) => entry.shape.setStyle({
      fillOpacity: hubId === selectedHub ? 0.38 : 0.2,
      weight: hubId === selectedHub ? 4 : 2,
    }));
    const houses = hubHouses(selectedHub);
    document.getElementById("detail-title").textContent = `${properties.district_names.join("·")} ${properties.dong_names.join("·") || "연속필지군"}`;
    document.getElementById("detail-summary").textContent = `${properties.parcel_count}개 빈집 필지가 경계를 맞댄 개발후보입니다. 확대하면 필지와 개별 빈집 위치를 확인할 수 있습니다.`;
    document.getElementById("detail-rank").textContent = `${properties.candidate_rank}위`;
    document.getElementById("detail-parcels").textContent = `${properties.parcel_count}필지`;
    document.getElementById("detail-houses").textContent = `${houses.length}개소`;
    document.getElementById("detail-area").textContent = `${Math.round(properties.union_area).toLocaleString("ko-KR")}㎡`;
    const list = document.getElementById("house-list");
    list.replaceChildren();
    houses.forEach((house) => {
      const card = document.createElement("article");
      card.append(
        textElement("strong", house.properties.exact_address || house.properties.road_address || "주소 미확인"),
        textElement("span", [
          house.properties.housing_type || "주택유형 미확인",
          house.properties.construction_year ? `${house.properties.construction_year}년` : "건축연도 미확인",
          house.properties.vacant_grade ? `${house.properties.vacant_grade}등급` : "등급 미확인",
        ].join(" · ")),
      );
      list.append(card);
    });
    featureLayers.hubs.get(selectedHub).shape.bringToFront();
    map.fitBounds(featureLayers.hubs.get(selectedHub).shape.getBounds(), {
      padding: [55, 55], maxZoom: 17,
    });
  }
  function renderCandidates() {
    candidateList.replaceChildren();
    data.hubs.filter(featureMatches).forEach((feature) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.hubId = feature.properties.hub_id;
      if (selectedHub === feature.properties.hub_id) button.classList.add("is-active");
      const label = document.createElement("span");
      label.append(
        textElement("strong", `${feature.properties.district_names.join("·")} ${feature.properties.dong_names.join("·") || "후보지"}`),
        textElement("small", `${feature.properties.parcel_count}개 연속필지 · ${Math.round(feature.properties.union_area).toLocaleString("ko-KR")}㎡`),
      );
      button.append(textElement("b", String(feature.properties.candidate_rank)), label);
      button.addEventListener("click", () => selectHub(feature));
      item.append(button);
      candidateList.append(item);
    });
  }
  function refreshLayers() {
    Object.values(layers).forEach((layer) => layer.clearLayers());
    featureLayers.hubs.clear(); featureLayers.parcels.clear(); featureLayers.houses.clear();
    data.hubs.filter(featureMatches).forEach((feature) => {
      const shape = L.geoJSON(feature, { style: {
        color: "#105bd1", weight: feature.properties.hub_id === selectedHub ? 4 : 2,
        fillColor: "#146cff", fillOpacity: feature.properties.hub_id === selectedHub ? 0.38 : 0.2,
      } }).on("click", () => selectHub(feature));
      const marker = L.marker(shape.getBounds().getCenter(), { icon: L.divIcon({
        className: "candidate-marker",
        html: `<span>${Number(feature.properties.candidate_rank)}</span>`,
        iconSize: [36, 36], iconAnchor: [18, 18],
      }) }).on("click", () => selectHub(feature));
      shape.addTo(layers.hubs); marker.addTo(layers.hubs);
      featureLayers.hubs.set(feature.properties.hub_id, { shape, marker });
    });
    data.parcels.filter(featureMatches).forEach((feature) => {
      const shape = L.geoJSON(feature, { style: {
        color: "#a64613", weight: 2, fillColor: "#ed7d31", fillOpacity: 0.5,
      } });
      shape.on("click", () => {
        const hub = data.hubs.find((item) => item.properties.hub_id === feature.properties.hub_id);
        if (hub) selectHub(hub);
      });
      shape.addTo(layers.parcels); featureLayers.parcels.set(feature.properties.pnu, shape);
    });
    data.houses.filter(featureMatches).forEach((feature) => {
      const marker = L.circleMarker(
        [feature.geometry.coordinates[1], feature.geometry.coordinates[0]],
        { radius: 7, color: "#fff", weight: 2, fillColor: "#d9475a", fillOpacity: 1 },
      ).bindPopup(houseDetailNode(feature));
      marker.addTo(layers.houses); featureLayers.houses.set(feature.properties.record_id, marker);
    });
    updateLayerVisibility(); renderCandidates();
  }
  function updateLayerVisibility() {
    const zoom = map.getZoom();
    if (!map.hasLayer(layers.hubs)) layers.hubs.addTo(map);
    if (zoom >= 14 && !map.hasLayer(layers.parcels)) layers.parcels.addTo(map);
    if (zoom < 14 && map.hasLayer(layers.parcels)) map.removeLayer(layers.parcels);
    if (zoom >= 17 && !map.hasLayer(layers.houses)) layers.houses.addTo(map);
    if (zoom < 17 && map.hasLayer(layers.houses)) map.removeLayer(layers.houses);
    mapElement.classList.toggle("parcel-detail-mode", zoom >= 14);
    mapElement.classList.toggle("street-detail-mode", zoom >= 17);
    featureLayers.hubs.forEach((entry) => {
      if (zoom >= 17) layers.hubs.removeLayer(entry.marker);
      else if (!layers.hubs.hasLayer(entry.marker)) layers.hubs.addLayer(entry.marker);
    });
    guide.textContent = zoom >= 17
      ? "개별 빈집 점을 누르면 정확주소·주택유형·건축연도를 확인합니다."
      : zoom >= 14
        ? "연속된 지적필지 경계가 표시됩니다. 더 확대하면 개별 빈집 위치가 보입니다."
        : "후보 번호를 선택하면 연속필지군으로 확대됩니다.";
  }
  map.on("zoomend", updateLayerVisibility);
  districtFilter.addEventListener("change", () => {
    selectedHub = null; refreshLayers();
    const visible = data.hubs.filter(featureMatches);
    if (visible.length) {
      const bounds = L.latLngBounds(visible.flatMap((feature) => {
        const featureBounds = L.geoJSON(feature).getBounds();
        return [featureBounds.getSouthWest(), featureBounds.getNorthEast()];
      }));
      map.fitBounds(bounds, { padding: [35, 35], maxZoom: 13 });
    }
  });
  document.getElementById("address-analysis-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = document.getElementById("address-analysis-result");
    result.hidden = false; result.textContent = "현재 게시 빈집·연속필지군을 확인하고 있습니다.";
    try {
      const response = await fetch("/tourism/api/vacant/address-analysis", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: document.getElementById("address-input").value }),
      });
      if (!response.ok) throw new Error("address_analysis_failed");
      const analysis = await response.json();
      result.textContent = `${analysis.interpretation}${analysis.hub_rank ? ` 후보 ${analysis.hub_rank}위, ${analysis.hub_parcel_count}개 연속필지군입니다.` : ""} ${analysis.limitation}`;
    } catch (_) {
      result.textContent = "주소 분석을 불러오지 못했습니다. 주소 형식과 서버 상태를 확인해 주세요.";
    }
  });
  Promise.all(
    ["hubs.geojson", "parcels.geojson", "vacant-houses.geojson"].map((url) => fetch(url).then((response) => {
      if (!response.ok) throw new Error("map_data_failed");
      return response.json();
    })),
  ).then(([hubs, parcels, houses]) => {
    data.hubs = hubs.features; data.parcels = parcels.features; data.houses = houses.features;
    [...new Set(data.hubs.flatMap((feature) => feature.properties.district_names))]
      .sort().forEach((district) => districtFilter.add(new Option(district, district)));
    refreshLayers();
    const allBounds = L.latLngBounds(data.hubs.flatMap((feature) => {
      const featureBounds = L.geoJSON(feature).getBounds();
      return [featureBounds.getSouthWest(), featureBounds.getNorthEast()];
    }));
    if (allBounds.isValid()) map.fitBounds(allBounds, { padding: [35, 35], maxZoom: 12 });
  }).catch(() => { candidateList.textContent = "게시 지도자료를 불러오지 못했습니다."; });
})();
