(() => {
  "use strict";
  document.querySelectorAll("[data-image-preview]").forEach((figure) => {
    const image = figure.querySelector("img");
    const caption = figure.querySelector("figcaption");
    const update = () => {
      image.hidden = !image.naturalWidth;
      caption.textContent = image.naturalWidth
        ? "Resized preview. Download the original for full detail."
        : "Image preview unavailable. Download the original to inspect it.";
    };
    image.addEventListener("load", update);
    image.addEventListener("error", update);
    if (image.complete) update();
    else caption.textContent = "Loading image preview…";
  });
})();
