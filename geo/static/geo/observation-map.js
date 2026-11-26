(() => {
  "use strict";

  const result = document.getElementById("analysis-result");
  if (!result) return;

  function createMap(section) {
    const canvas = section.querySelector("[data-observation-map-canvas]");
    const status = section.querySelector("[data-map-status]");
    section.querySelector("[data-observation-map-info]").hidden = false;
    if (!window.L || !window.PandectMapTiles) {
      status.textContent = "The background map could not start. Reload the page to try again. Coordinates and sources are still available.";
      status.classList.remove("sr-only");
      return;
    }

    const groups = new Map();
    for (const row of section.querySelectorAll("[data-position-kind]")) {
      const {latitude, longitude, positionKind} = row.dataset;
      const coordinates = [Number(latitude), Number(longitude)];
      const key = coordinates.join(",");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push({
        coordinates,
        kind: positionKind,
        label: row.querySelector("[data-position-label]").textContent.trim(),
        link: row.querySelector("a"),
      });
    }

    canvas.hidden = false;
    const map = L.map(canvas, {
      scrollWheelZoom: false, zoomAnimation: false, fadeAnimation: false,
      markerZoomAnimation: false, worldCopyJump: true, trackResize: false,
    });
    map.attributionControl.setPrefix(false);
    const bounds = L.latLngBounds([...groups.values()].map(([point]) => point.coordinates));
    map.fitBounds(bounds, {padding: [32, 32], maxZoom: 16, animate: false});
    const resize = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width && entry.contentRect.height) {
        map.invalidateSize({debounceMoveend: true});
      }
    });
    resize.observe(canvas);
    map.once("unload", () => resize.disconnect());
    for (const group of groups.values()) {
      const [first] = group;
      const labels = [...new Set(group.map(point => point.label))];
      const name = group.length === 1
        ? `${first.label} · ${first.link.textContent.trim()}`
        : `${group.length} positions · ${labels.join(", ")}`;
      const style = labels.length === 1 ? first.kind : "group";
      const shape = document.createElement("span");
      if (group.length > 1) {
        const count = document.createElement("b");
        count.textContent = group.length;
        shape.append(count);
      }
      shape.setAttribute("aria-hidden", "true");
      const sources = document.createElement("ul");
      sources.setAttribute("role", "list");
      for (const point of group) {
        const source = document.createElement("li");
        source.append(`${point.label} · `, point.link.cloneNode(true));
        sources.append(source);
      }
      const marker = L.marker(first.coordinates, {
        title: name,
        icon: L.divIcon({
          className: `case-map-marker observation-marker-${style}`,
          html: shape,
          iconSize: [44, 44],
          iconAnchor: [22, 22],
        }),
      }).addTo(map).bindPopup(sources, {maxWidth: 260, maxHeight: 190});
      marker.getElement().setAttribute("aria-label", name);
    }

    window.PandectMapTiles(map, section.dataset, status,
      "Some map tiles are unavailable. Coordinates and sources are still available. Pan or zoom to retry.");
    const plot = section.querySelector("[data-position-plot]");
    if (plot) plot.hidden = true;
    return map;
  }

  const maps = new Map();
  function syncMaps() {
    for (const [section, map] of maps) {
      if (result.contains(section)) continue;
      map?.remove();
      maps.delete(section);
    }
    for (const section of result.querySelectorAll("[data-observation-map][open]")) {
      if (!maps.has(section)) maps.set(section, createMap(section));
    }
  }

  syncMaps();
  // Live results replace the contents of this region after publication.
  const observer = new MutationObserver(syncMaps);
  observer.observe(result, {childList: true});
  result.addEventListener("toggle", ({target}) => {
    if (target.matches("[data-observation-map][open]")) syncMaps();
  }, true);
  document.addEventListener("pandect:access-lost", () => {
    observer.disconnect();
    for (const map of maps.values()) map?.remove();
    maps.clear();
  }, {once: true});
})();
