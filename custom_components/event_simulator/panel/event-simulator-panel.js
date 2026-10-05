/* Event Simulator sidebar panel.
 * Plain web component: no build step, no framework. Uses Home Assistant's CSS
 * variables so it matches the active theme.
 */

class EventSimulatorPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this.shadowRoot) this._render();
  }

  set narrow(narrow) {
    this._narrow = narrow;
  }

  set panel(panel) {
    this._panel = panel;
  }

  _render() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host {
          display: block;
          padding: 16px;
          color: var(--primary-text-color);
          background: var(--primary-background-color);
          min-height: 100vh;
          box-sizing: border-box;
        }
        h1 {
          font-size: 24px;
          font-weight: 400;
          margin: 0 0 8px;
        }
        p {
          color: var(--secondary-text-color);
          margin: 0;
        }
      </style>
      <h1>Event Simulator</h1>
      <p>Work in progress.</p>
    `;
  }
}

if (!customElements.get("event-simulator-panel")) {
  customElements.define("event-simulator-panel", EventSimulatorPanel);
}
