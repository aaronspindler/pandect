(() => {
  "use strict";

  window.PandectMapTiles = (map, {tileUrl, tileAttribution}, status, failureMessage) => {
    let failed = false;
    const tiles = L.tileLayer(tileUrl, {
      maxZoom: 19, referrerPolicy: "origin", updateWhenIdle: true, keepBuffer: 0,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" rel="noreferrer">OpenStreetMap contributors</a>',
    });
    if (tileAttribution) {
      const label = document.createElement("span");
      label.textContent = tileAttribution;
      map.attributionControl.addAttribution(label.outerHTML);
    }
    tiles.on("loading", () => {
      failed = false;
      status.textContent = "Loading map tiles…";
      status.classList.remove("sr-only");
    });
    tiles.on("tileerror", () => {
      failed = true;
      status.textContent = failureMessage;
      status.classList.remove("sr-only");
    });
    tiles.on("load", () => {
      if (failed) return;
      status.textContent = "Map loaded.";
      status.classList.add("sr-only");
    });
    tiles.addTo(map);
  };
})();
