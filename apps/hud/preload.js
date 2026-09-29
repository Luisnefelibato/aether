const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("adamHud", {
  onPttToggle: (cb) => ipcRenderer.on("ptt-toggle", () => cb()),
});
