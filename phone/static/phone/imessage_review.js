document.addEventListener("change", (event) => {
  const person = event.target;
  if (!person.matches("select[data-imessage-create]")) return;
  person.closest("fieldset").querySelector("[data-imessage-new-person]").open =
    person.value === person.dataset.imessageCreate;
});
