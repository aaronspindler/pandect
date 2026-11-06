(() => {
  const section = document.querySelector("[data-case-map]");
  if (!section) return;
  const fitButton = section.querySelector("[data-map-fit]");
  const status = section.querySelector("[data-map-status]");
  const canvas = document.getElementById("investigation-map");
  if (!window.L) {
    status.textContent = "The interactive map could not start. Reload the page to try again, or use the location list below.";
    return;
  }
  let points = JSON.parse(document.getElementById("case-map-points").textContent);
  const markers = new Map();
  canvas.hidden = false;
  status.textContent = "Loading map…";
  const map = L.map(canvas, { center: [20, 0], zoom: 2, scrollWheelZoom: false, zoomAnimation: false, fadeAnimation: false, markerZoomAnimation: false, worldCopyJump: true });
  const fit = () => {
    if (points.length) {
      map.fitBounds(points.map((point) => [point.latitude, point.longitude]), { padding: [32, 32], maxZoom: 16, animate: false });
    } else {
      map.setView([20, 0], 2, { animate: false });
    }
  };
  map.attributionControl.setPrefix(false);
  const markerLayer = L.layerGroup().addTo(map);
  map.on("resize", () => markerLayer.eachLayer((marker) => {
    const popup = marker.getPopup();
    popup.options.maxHeight = Math.min(224, canvas.clientHeight - 180);
    if (marker.isPopupOpen()) popup.update();
  }));
  const attributions = new Set();
  const renderPoints = (updated) => {
    let focusedNumber;
    let openPopup;
    for (const [number, selected] of markers) {
      if (selected.some((marker) => marker.getElement() === document.activeElement)) focusedNumber = number;
      const marker = selected.find((entry) => entry.isPopupOpen());
      if (marker && !openPopup) {
        const hasFocus = marker.getPopup().getElement().contains(document.activeElement);
        openPopup = {
          number: hasFocus && document.activeElement.dataset.mapNumber ? document.activeElement.dataset.mapNumber : number,
          position: marker.getLatLng(),
          hasFocus,
          href: hasFocus ? document.activeElement.getAttribute("href") : null,
        };
      }
    }
    points = updated;
    document.querySelector("[data-map-estimate-key]").hidden = !points.some((point) => point.derived);
    markerLayer.clearLayers();
    markers.clear();
    const groups = new Map();
    for (const point of points) {
      const key = `${point.latitude},${point.longitude}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(point);
      if (point.attribution && !attributions.has(point.attribution)) {
        const attribution = document.createElement("span");
        attribution.textContent = point.attribution;
        map.attributionControl.addAttribution(attribution.outerHTML);
        attributions.add(point.attribution);
      }
    }
    for (const group of groups.values()) {
      const point = group[0];
      const estimated = group.every((entry) => entry.derived);
      const poi = group.every((entry) => entry.poi);
      const label = group.length > 1 ? `${group.length} ${estimated ? "address estimates" : poi ? "POIs" : group.some((entry) => entry.poi) ? "locations and POIs" : "observations"} at this location` : `${point.number}. ${point.title}${point.derived ? ` · ${point.label}` : ""}`;
      const content = document.createElement("div");
      const heading = document.createElement("strong");
      heading.textContent = group.length > 1 ? label : (point.derived ? point.label : point.poi ? "Investigator POI · Person-linked place" : "Reported location");
      content.append(heading);
      const list = document.createElement("ul");
      for (const entry of group) {
        const row = document.createElement("li");
        const link = document.createElement("a");
        link.href = entry.url;
        link.dataset.mapNumber = entry.number;
        link.textContent = `${entry.number}. ${entry.title}`;
        row.append(link);
        if (entry.derived) {
          const review = document.createElement("a");
          review.href = entry.review_url;
          review.dataset.mapNumber = entry.number;
          review.textContent = entry.label + " · Review lookup";
          row.append(document.createElement("br"), review);
        }
        list.append(row);
      }
      content.append(list);
      const marker = L.marker([point.latitude, point.longitude], {
        title: label,
        alt: label,
        icon: L.divIcon({ className: `case-map-marker${estimated ? " case-map-estimate" : poi ? " case-map-poi" : ""}`, html: `<span><b>${group.length > 1 ? `×${group.length}` : point.number}</b></span>`, iconSize: [44, 44], iconAnchor: [22, 22] }),
      }).addTo(markerLayer).bindPopup(content, {
        maxWidth: 300,
        maxHeight: Math.min(224, canvas.clientHeight - 180),
        autoPanPaddingTopLeft: [16, 112],
        autoPanPaddingBottomRight: [16, 16],
        keepInView: true,
      });
      marker.getElement().setAttribute("aria-label", label);
      for (const entry of group) {
        const number = String(entry.number);
        if (!markers.has(number)) markers.set(number, []);
        markers.get(number).push(marker);
      }
    }
    document.querySelectorAll("[data-map-show]").forEach((button) => {
      button.hidden = !markers.has(button.dataset.mapShow);
    });
    if (openPopup) {
      const selected = markers.get(openPopup.number) || [];
      const marker = selected.find((entry) => entry.getLatLng().equals(openPopup.position));
      marker?.openPopup();
      if (openPopup.hasFocus) {
        const link = Array.from(marker?.getPopup().getElement().querySelectorAll("a[href]") || []).find((entry) => entry.getAttribute("href") === openPopup.href && (!entry.dataset.mapNumber || entry.dataset.mapNumber === openPopup.number));
        (link || marker?.getElement() || selected[0]?.getElement() || canvas).focus({ preventScroll: true });
      }
    }
    if (focusedNumber !== undefined) {
      (markers.get(focusedNumber)?.[0].getElement() || canvas).focus({ preventScroll: true });
    }
  };
  renderPoints(points);
  fit();
  if (section.dataset.tileUrl) {
    let tileFailed = false;
    const tiles = L.tileLayer(section.dataset.tileUrl, {
      maxZoom: 19,
      referrerPolicy: "origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" rel="noreferrer">OpenStreetMap contributors</a>',
      updateWhenIdle: true,
      keepBuffer: 0,
    });
    if (section.dataset.tileAttribution) {
      const attribution = document.createElement("span");
      attribution.textContent = section.dataset.tileAttribution;
      map.attributionControl.addAttribution(attribution.outerHTML);
    }
    tiles.on("loading", () => {
      tileFailed = false;
      status.textContent = "Loading map tiles…";
      status.classList.remove("sr-only");
    });
    tiles.on("tileerror", () => {
      tileFailed = true;
      status.textContent = "Some map tiles could not load. Source markers and the location list are still available. Pan or zoom to retry, or reload the page.";
      status.classList.remove("sr-only");
    });
    tiles.on("load", () => {
      if (!tileFailed) {
        status.textContent = "Map loaded.";
        status.classList.add("sr-only");
      }
    });
    tiles.addTo(map);
  } else {
    status.textContent = "No background map configured. Source locations are shown below.";
  }
  fitButton.hidden = false;
  document.querySelectorAll("[data-map-show]").forEach((button) => {
    button.addEventListener("click", () => {
      const selected = markers.get(button.dataset.mapShow);
      if (!selected) return;
      map.fitBounds(selected.map((marker) => marker.getLatLng()), { padding: [32, 32], maxZoom: 16, animate: false });
      selected[0].openPopup();
      canvas.scrollIntoView({ block: "center" });
      canvas.focus({ preventScroll: true });
    });
  });
  fitButton.addEventListener("click", fit);
  if (section.dataset.poiCreateUrl) {
    map.on("click", (event) => {
      const position = event.latlng.wrap();
      const link = document.createElement("a");
      const url = new URL(section.dataset.poiCreateUrl, window.location.origin);
      url.searchParams.set("latitude", position.lat.toFixed(6));
      url.searchParams.set("longitude", position.lng.toFixed(6));
      link.href = url.pathname + url.search;
      link.textContent = "Add POI here";
      L.popup({ maxWidth: 300 }).setLatLng(event.latlng).setContent(link).openOn(map);
    });
  }

  const form = document.querySelector("[data-map-geocode]");
  const lookupStatus = document.querySelector("[data-geocode-status]");
  const lookupButton = form?.querySelector("button");
  let timer;
  let busy = false;
  let checks = 0;
  let batches = 0;
  let pageChanged = false;
  const changed = () => {
    pageChanged = true;
    if (lookupStatus) lookupStatus.textContent = "Locations changed. Reload the page to update the map.";
    if (lookupButton) lookupButton.disabled = true;
  };
  const stopped = () => {
    if (lookupStatus) lookupStatus.textContent = "Local address lookup stopped. Reload the page or use Look up addresses locally to try again.";
  };
  const poll = async () => {
    clearTimeout(timer);
    if (++checks > 200) { stopped(); return; }
    try {
      const response = await fetch(window.location.href, { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) throw new Error("Map unavailable");
      const data = await response.json();
      if (data.page_key !== section.dataset.pageKey || data.poi_key !== section.dataset.poiKey) {
        changed();
        return;
      }
      if (JSON.stringify(points) !== JSON.stringify(data.points)) {
        const initiallyEmpty = points.length === 0;
        renderPoints(data.points);
        if (initiallyEmpty) fit();
      }
      document.querySelector("[data-map-count]").textContent = `${data.mapped_count} of ${data.locations.length} locations mapped`;
      for (const location of data.locations) {
        const row = document.querySelector(`[data-location-row="${location.number}"]`);
        const label = row?.querySelector("[data-geocode-label]");
        const review = row?.querySelector("[data-geocode-review]");
        if (label) label.textContent = location.label;
        if (review) { review.href = location.url; review.hidden = !location.url; }
      }
      if (data.can_queue && batches < 4) {
        await queue(false);
      } else if (data.pending) {
        if (lookupStatus) lookupStatus.textContent = "Locating addresses locally…";
        timer = setTimeout(poll, 3000);
      } else if (lookupStatus) {
        lookupStatus.textContent = data.can_queue ? "More addresses remain. Use Look up addresses locally to continue." : "Address lookup finished. Review the estimates and lookup results below.";
      }
    } catch { stopped(); }
  };
  const queue = async (retry) => {
    if (!form || busy || pageChanged) return;
    clearTimeout(timer);
    busy = true;
    batches += 1;
    if (lookupButton) lookupButton.disabled = true;
    if (lookupStatus) lookupStatus.textContent = "Locating addresses locally…";
    try {
      const body = new FormData(form);
      body.set("retry", retry ? "1" : "0");
      const response = await fetch(form.action, { method: "POST", body, headers: { Accept: "application/json" } });
      if (response.status === 409) { changed(); return; }
      if (!response.ok) throw new Error("Local lookup unavailable");
      busy = false;
      await poll();
    } catch { stopped(); }
    finally {
      busy = false;
      if (lookupButton) lookupButton.disabled = pageChanged;
    }
  };
  form?.addEventListener("submit", (event) => { event.preventDefault(); checks = 0; batches = 0; queue(true); });
  if (form?.hasAttribute("data-auto-locate")) queue(false);
  else if (document.querySelector("[data-map-pending]")) poll();
})();
