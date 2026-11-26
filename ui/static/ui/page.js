// Page-bound enhancements release document listeners and outstanding work before a soft visit.
window.PandectPage = function (mount) {
  let controller;
  function start() {
    controller = new AbortController();
    mount(controller.signal);
  }
  document.addEventListener("pandect:before-navigate", () => controller?.abort());
  document.addEventListener("pandect:navigated", start);
  start();
};

window.PandectPage.isPlainClick = function (event) {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
};
