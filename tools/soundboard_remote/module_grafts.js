(() => {
  "use strict";

  const token = __KSH_TOKEN_JSON__;

  const liveControls = new Map();

  const key = (module, surface, control) =>
    JSON.stringify([module, surface, control]);

  function applyConfirmed(surfaces) {

    for (const surface of surfaces) {

      if (
        !surface.state
        || typeof surface.state !== "object"
      ) {
        continue;
      }

      for (
        const [controlId, value]
        of Object.entries(surface.state)
      ) {
        const entry = liveControls.get(
          key(
            surface.module,
            surface.id,
            controlId
          )
        );

        if (
          !entry
          || entry.input.matches(":active")
        ) {
          continue;
        }

        if (entry.input.type === "checkbox") {
          entry.input.checked = value === true;
        } else {
          entry.input.value = String(value);
        }

        if (entry.output) {
          entry.output.textContent =
            String(value);
        }
      }
    }
  }


  let signature = "";
  let showing = false;

  const header = document.querySelector("header");

  if (!header) return;

  let nav = header.querySelector(".module-switch");

  if (!nav) {
    nav = document.createElement("nav");
    nav.className = "module-switch";
    nav.setAttribute(
      "aria-label",
      "Remote module"
    );

    header.append(nav);
  }

  const tab = document.createElement("button");
  tab.type = "button";
  tab.textContent = "◇ Modules";
  tab.hidden = true;
  nav.append(tab);

  const panel = document.createElement("section");
  panel.className = "external-module-panel";
  panel.hidden = true;
  panel.setAttribute(
    "aria-label",
    "External modules"
  );

  header.insertAdjacentElement(
    "afterend",
    panel
  );

  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "← Back";
  close.addEventListener("click", exit);

  const bodyBar = document.querySelector(
    ".bottom-bar"
  );

  const pads = document.getElementById(
    "padsModule"
  );

  const voice = document.getElementById(
    "voiceFxModule"
  );

  let previous = "pads";

  function exit() {
    if (!showing) return;

    showing = false;
    panel.hidden = true;
    tab.classList.remove("active");

    if (bodyBar) {
      bodyBar.style.removeProperty(
        "display"
      );
    }

    setRemoteMode(previous, false);
  }

  for (
    const id of [
      "modulePadsButton",
      "moduleVoiceButton"
    ]
  ) {
    const button = document.getElementById(id);

    if (button) {
      button.addEventListener(
        "click",
        () => {
          if (showing) {
            showing = false;
            panel.hidden = true;
            tab.classList.remove("active");

            if (bodyBar) {
              bodyBar.style.removeProperty(
                "display"
              );
            }
          }
        },
        true
      );
    }
  }

  tab.addEventListener("click", () => {
    if (showing) return;

    previous = (
      voice && !voice.hidden
    )
      ? "voice"
      : "pads";

    showing = true;

    if (pads) pads.hidden = true;
    if (voice) voice.hidden = true;

    panel.hidden = false;
    tab.classList.add("active");

    if (bodyBar) {
      bodyBar.style.display = "none";
    }
  });


  async function send(surface, control, value, status) {
    const serial = (status._kshSerial || 0) + 1;

    status._kshSerial = serial;

    const current = () =>
      status._kshSerial === serial;

    status.textContent = "Sending…";

    try {
      const response = await fetch(
        "/module-action?token="
        + encodeURIComponent(token),
        {
          method: "POST",
          headers: {
            "Content-Type":
              "application/x-www-form-urlencoded"
          },
          body: new URLSearchParams({
            payload: JSON.stringify({
              module: surface.module,
              surface: surface.id,
              control: control.id,
              value: value
            })
          })
        }
      );

      const sent = await response.json();

      if (!current()) return;

      if (
        !response.ok
        || !sent.queued
        || !sent.request_id
      ) {
        status.textContent =
          sent.error || "Unavailable";

        return;
      }

      status.textContent = "Waiting for module…";

      for (let attempt = 0; attempt < 44; attempt++) {
        await new Promise(
          resolve => setTimeout(resolve, 250)
        );

        if (!current()) return;

        const reply = await fetch(
          "/module-action-result?token="
          + encodeURIComponent(token)
          + "&request_id="
          + encodeURIComponent(sent.request_id),
          {
            cache: "no-store"
          }
        );

        if (!current()) return;

        if (!reply.ok) {
          status.textContent = "Reply unavailable";
          return;
        }

        const receipt = await reply.json();

        if (
          receipt.state === "accepted"
          || receipt.state === "refused"
        ) {
          status.textContent = (
            receipt.state === "accepted"
              ? "✓ "
              : "⚠ "
          ) + (
            receipt.detail
            || receipt.state
          );

          return;
        }
      }

      if (current()) {
        status.textContent =
          "Module reply timed out";
      }

    } catch (_) {
      if (current()) {
        status.textContent = "Disconnected";
      }
    }
  }

  function render(surfaces) {
    if (!Array.isArray(surfaces)) {
      surfaces = [];
    }

    tab.hidden = surfaces.length === 0;
    if (!surfaces.length) {signature="";liveControls.clear();liveControls.clear();panel.replaceChildren();}

    if (!surfaces.length) {
      exit();
      return;
    }

    const next = JSON.stringify(surfaces.map(s => ({...s, state:undefined})));

    if (next === signature) {applyConfirmed(surfaces);return;}

    signature = next;
    panel.replaceChildren();
    panel.append(close);

    for (const surface of surfaces) {
      const card = document.createElement(
        "section"
      );

      card.className = "external-module-card";

      const heading = document.createElement(
        "h2"
      );

      heading.textContent = (
        surface.icon + " " + surface.label
      );

      card.append(heading);

      for (
        const control of (
          surface.controls || []
        )
      ) {
        const row = document.createElement(
          "label"
        );

        const caption = document.createElement(
          "span"
        );

        caption.textContent = control.label;
        row.append(caption);

        if (control.type === "label") {
          card.append(row);
          continue;
        }

        const status = document.createElement(
          "small"
        );

        status.setAttribute(
          "role",
          "status"
        );

        let input;

        let output = null;

        if (control.type === "button") {
          caption.remove();

          input = document.createElement(
            "button"
          );

          input.type = "button";
          input.textContent = control.label;

          input.addEventListener(
            "click",
            () => send(
              surface,
              control,
              true,
              status
            )
          );

        } else if (control.type === "slider") {
          input = document.createElement(
            "input"
          );

          input.type = "range";
          input.min = control.min;
          input.max = control.max;
          input.value = control.default;

          output = document.createElement(
            "output"
          );

          output.textContent = input.value;

          input.addEventListener(
            "input",
            () => {
              output.textContent = input.value;
            }
          );

          input.addEventListener(
            "change",
            () => send(
              surface,
              control,
              Number(input.value),
              status
            )
          );

          row.append(output);

        } else if (control.type === "toggle") {
          input = document.createElement(
            "input"
          );

          input.type = "checkbox";
          input.checked = (
            control.default === true
          );

          input.addEventListener(
            "change",
            () => send(
              surface,
              control,
              input.checked,
              status
            )
          );

        } else if (control.type === "select") {
          input = document.createElement(
            "select"
          );

          for (
            const option of (
              control.options || []
            )
          ) {
            const element = document.createElement(
              "option"
            );

            element.value = option.value;
            element.textContent = option.label;

            input.append(element);
          }

          input.value = control.default;

          input.addEventListener(
            "change",
            () => send(
              surface,
              control,
              input.value,
              status
            )
          );

        } else {
          continue;
        }

        row.append(input, status);liveControls.set(key(surface.module,surface.id,control.id),{input,output});
        card.append(row);
      }

      panel.append(card);
    }
    applyConfirmed(surfaces);
  }

  async function poll() {
    if (document.hidden) return;

    try {
      const response = await fetch(
        "/module-extensions?token="
        + encodeURIComponent(token),
        {
          cache: "no-store"
        }
      );

      if (!response.ok) {
        throw new Error(
          "Module service unavailable"
        );
      }

      const result = await response.json();

      render(result.surfaces);

    } catch (_error) {
      render([]);
    }
  }

  poll();

  window.setInterval(
    poll,
    2200
  );
})();
